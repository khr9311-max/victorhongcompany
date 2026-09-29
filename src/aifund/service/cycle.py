"""의사결정 사이클.

데이터 스냅샷 → 전략별 독립 제안 → (B/C) AI 검토 반영 → 포트폴리오 조정(상계) → 결정적 위험 검사 → 중앙 주문 실행 → 기록.
운용 장부와 가상 비교 장부(A/B/C)는 같은 스냅샷으로 각자 독립적으로 판단·체결한다.
오래된 봉(서비스 중단으로 놓친 신호)은 실행하지 않고 건너뛴다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from aifund.brokers.upbit import UpbitMarketData
from aifund.core.ids import new_id
from aifund.core.money import ZERO, ceil_step, dstr, floor_step
from aifund.core.timeutil import to_iso
from aifund.data.collector import Snapshot
from aifund.db.database import dumps
from aifund.domain.models import Action, Instrument, Quote, Side
from aifund.execution.executor import OrderExecutor, OrderIntent
from aifund.ledger.ledger import BOOK_STRATEGY, PositionRow
from aifund.ledger.valuation import value_book
from aifund.portfolio import allocator
from aifund.portfolio.allocator import NetOrder, TargetInput
from aifund.risk.engine import RiskInputs, evaluate
from aifund.service.context import OPERATING, AppContext
from aifund.strategies.base import PositionView, Signal, StrategyContext, build_strategies

log = logging.getLogger(__name__)

RULE_STRATEGIES = ("trend_sma", "mean_reversion")


@dataclass
class CycleResult:
    cycle_id: str
    market: str
    status: str
    snapshot_id: str | None = None
    notes: list[str] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)


def limit_price(inst: Instrument, q: Quote, side: Side, offset_bps: Decimal) -> Decimal | None:
    if side == Side.BUY:
        if q.ask is None:
            return None
        raw = q.ask * (1 + offset_bps / Decimal(10000))
        tick = inst.tick_for(raw)
        px = floor_step(raw, tick)
        return max(px, q.ask)
    if q.bid is None:
        return None
    raw = q.bid * (1 - offset_bps / Decimal(10000))
    tick = inst.tick_for(raw)
    px = ceil_step(raw, tick)
    return min(px, q.bid)


class DecisionCycle:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.last_snapshots: dict[str, Snapshot] = {}

    # ------------------------------------------------------------------ 보조
    def _positions_by_strategy(self, book_id: str, strategy_id: str) -> dict[str, PositionView]:
        out = {}
        for p in self.ctx.ledger.positions(book_id):
            if p.strategy_id == strategy_id:
                out[p.instrument_id] = PositionView(p.qty, p.cost_basis, p.opened_at)
        return out

    def _signals(self, snap: Snapshot, book_id: str) -> dict[str, list[Signal]]:
        s = self.ctx.settings
        out: dict[str, list[Signal]] = {}
        for strat in build_strategies(s.strategies.model_dump(include={"trend_sma", "mean_reversion"})):
            ctx = StrategyContext(snap, self._positions_by_strategy(book_id, strat.strategy_id), self.ctx.clock.now())
            out[strat.strategy_id] = strat.evaluate(ctx)
        return out

    def _to_quote(self, krw: Decimal, inst: Instrument) -> Decimal | None:
        if inst.quote_ccy == "KRW":
            return krw
        st = self.ctx.fx.status(self.ctx.settings.risk.max_fx_age_hours)
        if not st.fresh or st.rate is None:
            return None
        return krw / st.rate.rate

    def _clock_skew(self, market: str) -> float | None:
        mr = self.ctx.markets.get(market)
        if mr is not None and isinstance(mr.data, UpbitMarketData):
            return mr.data.http.last_clock_skew
        row = self.ctx.db.query_one("SELECT clock_skew_sec FROM broker_health WHERE account_id=?", (mr.operating_account if mr else "",))
        return row["clock_skew_sec"] if row else None

    def _broker_auth_ok(self, account_id: str) -> bool | None:
        if self.ctx.mode not in ("live", "broker_sandbox"):
            return None
        row = self.ctx.db.query_one("SELECT auth_ok, last_ok_at FROM broker_health WHERE account_id=?", (account_id,))
        if row is None:
            return False
        return bool(row["auth_ok"])

    # ------------------------------------------------------------------ 실행
    async def run(self, market: str, *, trigger: str = "candle") -> CycleResult:
        ctx = self.ctx
        s = ctx.settings
        mr = ctx.markets.get(market)
        cid = new_id("cyc")
        now = ctx.clock.now()
        ctx.db.execute("INSERT INTO cycles(cycle_id, market, started_at, status) VALUES (?,?,?,?)", (cid, market, to_iso(now), "running"))
        res = CycleResult(cid, market, "running")
        if mr is None or mr.collector is None:
            res.status = "skipped_no_data"
            res.notes.append(mr.data_reason if mr else "시장 비활성")
            self._finish(res)
            return res
        try:
            snap = await mr.collector.build(market, s.markets[market], s.risk)  # type: ignore[index]
        except Exception as exc:
            ctx.incidents.open("data_error", f"스냅샷 실패: {exc}", market=market)
            res.status = "data_error"
            res.notes.append(str(exc))
            self._finish(res)
            return res
        res.snapshot_id = snap.snapshot_id
        self.last_snapshots[market] = snap
        intraday = s.markets[market].candle != "1d"  # type: ignore[index]
        if snap.candle_close_time is not None and trigger == "candle" and intraday:
            # 일봉(주식)은 수집기가 '직전 거래일 종가 확정 여부'로 지연을 판정한다(주말·휴장 고려)
            age = (now - snap.candle_close_time).total_seconds()
            if age > s.risk.max_signal_age_sec:
                res.status = "skipped_stale"
                res.notes.append(f"최근 완성봉이 {age / 60:.0f}분 전 → 놓친 신호는 실행하지 않음")
                self._finish(res)
                return res
        op_signals = self._signals(snap, OPERATING)
        self._store_signals(cid, snap, op_signals)
        for book_id in (OPERATING, "shadow_A", "shadow_B", "shadow_C"):
            executor = ctx.executor_for(book_id, market)
            if executor is None:
                res.notes.append(f"{book_id}: 주문 실행기 없음({mr.broker_reason})")
                continue
            try:
                await self._run_book(cid, book_id, snap, executor, res)
            except Exception as exc:  # 한 장부의 실패가 다른 장부를 막지 않게
                log.exception("장부 %s 처리 실패", book_id)
                ctx.incidents.open("cycle_error", f"{book_id} 처리 실패: {exc}", market=market, book_id=book_id)
                res.notes.append(f"{book_id} 실패: {exc}")
        await self._baseline(cid, snap, res)
        res.status = "done"
        self._finish(res)
        return res

    def _finish(self, res: CycleResult) -> None:
        self.ctx.db.execute("UPDATE cycles SET finished_at=?, status=?, snapshot_id=?, detail_json=? WHERE cycle_id=?",
                            (to_iso(self.ctx.clock.now()), res.status, res.snapshot_id,
                             dumps({"notes": res.notes, "orders": res.orders}), res.cycle_id))

    def _store_signals(self, cid: str, snap: Snapshot, signals: dict[str, list[Signal]]) -> None:
        now = to_iso(self.ctx.clock.now())
        from aifund.strategies.base import REGISTRY

        with self.ctx.db.tx() as c:
            for sid, sigs in signals.items():
                for sg in sigs:
                    c.execute(
                        "INSERT INTO signals(cycle_id, snapshot_id, strategy_id, strategy_version, instrument_id, action, target_weight, "
                        "rationale, indicators_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (cid, snap.snapshot_id, sid, REGISTRY[sid].version, sg.instrument_id, sg.action.value,
                         dstr(sg.target_weight) if sg.target_weight is not None else "keep", sg.rationale, dumps(sg.indicators), now),
                    )

    def signals_payload(self, snap: Snapshot) -> list[dict[str, Any]]:
        sigs = self._signals(snap, OPERATING)
        return [{"strategy_id": sid, "instrument_id": x.instrument_id, "action": x.action.value,
                 "target_weight": str(x.target_weight) if x.target_weight is not None else "keep", "rationale": x.rationale}
                for sid, lst in sigs.items() for x in lst]

    async def _run_book(self, cid: str, book_id: str, snap: Snapshot, executor: OrderExecutor, res: CycleResult) -> None:
        ctx = self.ctx
        s = ctx.settings
        market = snap.market
        setting = ctx.book_setting(book_id)
        now = ctx.clock.now()
        signals = self._signals(snap, book_id)
        targets: list[TargetInput] = []
        ai_hold: str | None = None
        ai_positions = {p.instrument_id: p.qty for p in ctx.ledger.positions(book_id) if p.strategy_id == "ai_research"}
        view = None
        if setting in ("B", "C"):
            view = ctx.ai.view(market, setting, ai_positions)
            if not view.available and view.hold_new_risk:
                ai_hold = f"AI 사용 불가({view.reason}) + 설정: 신규 위험 보류"
            targets.extend(view.proposals)
        elif ai_positions:
            for iid, q in ai_positions.items():
                if q > 0:
                    targets.append(TargetInput("ai_research", iid, Action.SELL, ZERO, f"{setting} 설정은 AI 슬리브를 현금으로 유지", source="ai"))
        for sid, sigs in signals.items():
            for sg in sigs:
                blocked = None
                if view is not None and sg.action == Action.BUY and sg.instrument_id in view.veto:
                    blocked = view.veto[sg.instrument_id]
                targets.append(TargetInput(sid, sg.instrument_id, sg.action, sg.target_weight, sg.rationale, "rule",
                                           invalidation=sg.invalidation, blocked_reason=blocked))
        # 같은 종목에 미체결(또는 상태 불명) 주문이 있으면 이번 주기엔 새 주문을 내지 않는다(주문 중복·과다 노출 방지)
        busy = {r[0] for r in ctx.db.query(
            "SELECT DISTINCT instrument_id FROM orders WHERE book_id=? AND status IN "
            "('pending','submitted','partially_filled','cancel_pending','unknown')", (book_id,))}
        for t in targets:
            if t.instrument_id in busy and t.blocked_reason is None and t.action in (Action.BUY, Action.SELL, Action.REDUCE):
                t.blocked_reason = "같은 종목 미체결 주문 진행 중: 이번 주기 보류"
        instruments = {iid: it.instrument for iid, it in snap.items.items()}
        val = value_book(ctx.db, ctx.ledger, book_id, ctx.price, ctx.market_store.instrument, ctx.fx, s.risk)
        capital = ctx.market_capital(book_id, market)

        def sleeve_equity(strategy_id: str, iid: str) -> Decimal | None:
            w = s.strategies.sleeves.get(strategy_id, ZERO)
            st = val.by_strategy.get(strategy_id, {})
            krw = w * capital + st.get("realized", ZERO) + st.get("unrealized", ZERO)
            return self._to_quote(krw, instruments[iid])

        def quote_amt(krw: Decimal):  # type: ignore[no-untyped-def]
            return lambda iid: self._to_quote(krw, instruments[iid]) or krw

        plan = allocator.plan(
            targets=targets, positions=ctx.ledger.positions(book_id),
            sleeve_equity_quote=sleeve_equity,
            ref_price=lambda iid: (snap.items[iid].quote.mid if snap.items.get(iid) and snap.items[iid].quote else None),
            instruments=instruments,
            rebalance_threshold_quote=quote_amt(s.strategies.rebalance_threshold_krw),
            max_order_quote=lambda iid: self._to_quote(s.risk.max_order_notional_krw * Decimal("0.98"), instruments[iid]),
            block_buys_reason=lambda iid: ai_hold,
        )
        expires = now + timedelta(seconds=s.risk.max_signal_age_sec)
        prop_ids: dict[tuple[str, str], str] = {}
        with ctx.db.tx() as c:
            for pr in plan.proposals:
                pid = new_id("prop")
                prop_ids[(pr.strategy_id, pr.instrument_id)] = pid
                c.execute(
                    "INSERT INTO proposals(proposal_id, book_id, cycle_id, strategy_id, instrument_id, market, action, target_weight, "
                    "target_notional_krw, current_notional_krw, rationale, sources_json, counterarguments_json, invalidation, created_at, "
                    "expires_at, snapshot_id, status, decision_reason, ai_report_id, settings_version, code_version, prompt_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (pid, book_id, cid, pr.strategy_id, pr.instrument_id, market, pr.action,
                     dstr(pr.target_weight) if pr.target_weight is not None else "keep", dstr(pr.target_notional),
                     dstr(pr.current_notional), pr.rationale, dumps(pr.sources), dumps(pr.counterarguments), pr.invalidation,
                     to_iso(now), to_iso(expires), snap.snapshot_id, pr.status, pr.decision_reason, pr.ai_report_id,
                     ctx.settings_version, ctx.code_version, pr.prompt_version),
                )
            for cr in plan.crosses:
                inst = instruments[cr.instrument_id]
                ctx.ledger.internal_transfer(c, book_id=book_id, iid=cr.instrument_id, qty=cr.qty, price=cr.price,
                                             from_strategy=cr.from_strategy, to_strategy=cr.to_strategy, quote_ccy=inst.quote_ccy,
                                             ts=now, ref=cid)
        for note in plan.notes:
            res.notes.append(f"{book_id}: {note}")
        for no in plan.orders:
            await self._execute_net(cid, book_id, snap, executor, no, [prop_ids.get((sid, no.instrument_id)) for sid, _ in no.legs],
                                    ai_hold, res)
        self.record_equity(book_id)

    def risk_inputs(self, book_id: str, executor: OrderExecutor, snap: Snapshot | None, inst: Instrument, side: Side,
                    qty: Decimal, price: Decimal, risk_increasing: bool, ai_hold: str | None, purpose: str) -> RiskInputs:
        ctx = self.ctx
        s = ctx.settings
        market = inst.market
        q = ctx.market_store.latest_quote(inst.instrument_id)
        notional_krw, _ = ctx.fx.to_krw(qty * price, inst.quote_ccy, s.risk.max_fx_age_hours, s.risk.fx_haircut_pct)
        fx_st = ctx.fx.status(s.risk.max_fx_age_hours)
        from aifund.markets.calendar import session_info

        sess = session_info(market, ctx.clock.now())
        it = snap.items.get(inst.instrument_id) if snap else None
        book = ctx.ledger.book(book_id)
        kind = book["kind"] if book else "shadow"
        live_auth = None
        if ctx.mode == "live" and kind == "operating":
            live_auth = ctx.activations.authorized("live", market, executor.account_id, inst.instrument_id, s)
        return RiskInputs(
            mode=ctx.mode, market=market, book_kind=kind, account_id=executor.account_id, instrument=inst, side=side, qty=qty,
            limit_price=price, notional_krw=notional_krw, risk_increasing=risk_increasing, quote=q, now=ctx.clock.now(),
            snapshot_issues=list(it.issues) if it else [], session_open=sess.is_open, session_reason=sess.reason,
            halted_reason=ctx.flags.halted(market), account_blocks=ctx.flags.account_blocks(executor.account_id),
            startup_reconciled=ctx.startup_reconciled.get(executor.account_id, kind != "operating"),
            unknown_orders_account=executor.unknown_count(), unknown_orders_instrument=executor.unknown_count(inst.instrument_id),
            risk_state=ctx.equity.state(book_id), ai_hold_reason=ai_hold, clock_skew_sec=self._clock_skew(market),
            fx_ok=fx_st.fresh, fx_reason=fx_st.reason, broker_auth_ok=self._broker_auth_ok(executor.account_id) if kind == "operating" else None,
            held_instruments=ctx.ledger.held_instruments(book_id), pending_buy_instruments=executor.pending_buy_instruments(book_id),
            sellable_qty=executor.sellable_qty(book_id, inst.instrument_id), live_authorized=live_auth, purpose=purpose,
        )

    async def _execute_net(self, cid: str, book_id: str, snap: Snapshot | None, executor: OrderExecutor, no: NetOrder,
                           proposal_ids: list[str | None], ai_hold: str | None, res: CycleResult, purpose: str = "rebalance",
                           offset_bps: Decimal | None = None) -> None:
        ctx = self.ctx
        s = ctx.settings
        inst = ctx.market_store.instrument(no.instrument_id)
        q = ctx.market_store.latest_quote(no.instrument_id)
        side = Side(no.side)
        if inst is None or q is None:
            res.notes.append(f"{book_id} {no.instrument_id}: 상품/호가 없음")
            return
        px = limit_price(inst, q, side, offset_bps if offset_bps is not None else s.execution.limit_offset_bps)
        if px is None:
            res.notes.append(f"{book_id} {no.instrument_id}: 실행 가능 호가 없음")
            return
        if side == Side.BUY:
            # 가용 현금(예약 제외) 안으로 수량을 비례 축소한다(계좌 간 자금 이동은 가정하지 않음)
            from aifund.execution.executor import est_fee_rate

            unit = px * (1 + est_fee_rate(inst, side, s)) * (1 + s.risk.fee_buffer_pct / 100)
            max_qty = floor_step(executor.available_cash(book_id, inst.quote_ccy) / unit, inst.qty_step)
            if max_qty < no.qty:
                if max_qty * px < inst.min_notional:
                    res.notes.append(f"{book_id} {no.instrument_id}: 가용 현금 부족으로 매수 생략")
                    return
                scale = max_qty / no.qty
                legs = [(sid, floor_step(q * scale, inst.qty_step)) for sid, q in no.legs]
                legs = [(sid, q) for sid, q in legs if q > 0]
                no = NetOrder(no.instrument_id, no.side, sum((q for _, q in legs), ZERO), no.ref_price, legs, no.risk_increasing,
                              no.notes + ["가용 현금 한도로 수량 축소"])
                if no.qty <= 0:
                    return
        risk = self.risk_inputs(book_id, executor, snap, inst, side, no.qty, px, no.risk_increasing, ai_hold, purpose)
        dec = evaluate(risk, s.risk)
        iid = new_id("int")
        notional_krw = risk.notional_krw if risk.notional_krw is not None else ZERO
        ctx.db.execute(
            "INSERT INTO intents(intent_id, cycle_id, book_id, instrument_id, side, qty, ref_price, notional_krw, risk_increasing, purpose, "
            "status, risk_reasons_json, allocations_json, proposal_ids_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, cid, book_id, no.instrument_id, no.side, dstr(no.qty), dstr(no.ref_price), dstr(notional_krw), int(no.risk_increasing),
             purpose, "approved" if dec.approved else "rejected", dumps(dec.reasons), dumps([[a, str(b)] for a, b in no.legs]),
             dumps([p for p in proposal_ids if p]), to_iso(ctx.clock.now())),
        )
        if not dec.approved:
            res.orders.append({"book": book_id, "instrument": no.instrument_id, "side": no.side, "qty": str(no.qty),
                               "status": "risk_rejected", "reasons": dec.reasons})
            with ctx.db.tx() as c:
                for pid in proposal_ids:
                    if pid:
                        c.execute("UPDATE proposals SET status='rejected', decision_reason=? WHERE proposal_id=?",
                                  ("위험검사 거부: " + "; ".join(dec.reasons)[:500], pid))
            return
        # 제안 만료 확인(느린 처리로 만료된 제안은 실행하지 않는다)
        oi = OrderIntent(iid, book_id, executor.account_id, inst.market, inst, side, no.qty, px, no.ref_price, no.legs,
                         no.risk_increasing, purpose, cid, [p for p in proposal_ids if p], s.execution.order_ttl_sec)
        oid, status = await executor.execute(oi)
        res.orders.append({"book": book_id, "instrument": no.instrument_id, "side": no.side, "qty": str(no.qty), "price": str(px),
                           "order_id": oid, "status": status})

    def record_equity(self, book_id: str) -> None:
        ctx = self.ctx
        val = value_book(ctx.db, ctx.ledger, book_id, ctx.price, ctx.market_store.instrument, ctx.fx, ctx.settings.risk)
        ctx.equity.record(val, ctx.ai_cost_for_setting(ctx.book_setting(book_id)), ctx.settings.risk)

    async def _baseline(self, cid: str, snap: Snapshot, res: CycleResult) -> None:
        """매수·보유 기준 장부: 처음 한 번 허용 종목을 동일비중으로 산다(이후 보유)."""
        ctx = self.ctx
        book_id = "baseline_bh"
        executor = ctx.shadow_executors.get(book_id)
        if executor is None:
            return
        s = ctx.settings
        held = ctx.ledger.held_instruments(book_id)
        pending = executor.pending_buy_instruments(book_id)
        todo = [it for iid, it in snap.items.items() if iid not in held and iid not in pending]
        ever = {r[0] for r in ctx.db.query("SELECT DISTINCT instrument_id FROM orders WHERE book_id=? AND status IN "
                                            "('filled','partially_filled')", (book_id,))}
        todo = [it for it in todo if it.instrument.instrument_id not in ever]
        if todo:
            per = ctx.market_capital(book_id, snap.market) * Decimal("0.98") / max(1, len(snap.items))
            for it in todo:
                if not (it.ok and it.quote and it.quote.mid):
                    continue
                inst = it.instrument
                px = it.quote.mid
                amt = self._to_quote(min(per, s.risk.max_order_notional_krw * Decimal("0.98")), inst)
                if amt is None:
                    continue
                qty = floor_step(amt / px, inst.qty_step)
                if qty <= 0:
                    continue
                await self._execute_net(cid, book_id, snap, executor, NetOrder(inst.instrument_id, "buy", qty, px, [(BOOK_STRATEGY, qty)], True),
                                        [], None, res, purpose="baseline")
        self.record_equity(book_id)
        # 현금 기준 장부는 거래 없음
        self.record_equity("baseline_cash")


def positions_summary(rows: list[PositionRow]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for p in rows:
        out[p.instrument_id] = out.get(p.instrument_id, ZERO) + p.qty
    return out
