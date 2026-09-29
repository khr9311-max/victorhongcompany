"""LIVE 활성화 사전 점검(실행무결성 + 계좌 연결 확인). 몇 주의 모의운용·수익성 입증은 요구하지 않는다."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import timedelta

from aifund.brokers.base import BrokerError
from aifund.control.live import confirm_phrase
from aifund.core.ids import client_order_id
from aifund.core.money import D, ceil_step, floor_step
from aifund.core.timeutil import parse_iso, to_iso
from aifund.db.database import dumps
from aifund.domain.models import OrderRequest, Side
from aifund.execution.reconcile import Reconciler
from aifund.service.context import OPERATING, AppContext


@dataclass
class CheckItem:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


def reconciler_for(ctx: AppContext, market: str) -> Reconciler | None:
    mr = ctx.markets.get(market)
    if mr is None or mr.operating_executor is None or mr.operating_broker is None:
        return None
    insts = [i for i in (ctx.market_store.instrument(f"{market}:{s}") for s in ctx.settings.markets[market].instruments) if i]  # type: ignore[index]
    if mr.reconciler is None or {i.instrument_id for i in mr.reconciler.instruments} != {i.instrument_id for i in insts}:
        from aifund.markets.rules import market_currency

        ccy = market_currency(market)
        accounts = {ctx.markets[m].operating_executor.account_id for m, ms in ctx.settings.markets.items()  # type: ignore[union-attr]
                    if ms.enabled and m in ctx.markets and ctx.markets[m].operating_executor is not None
                    and market_currency(m) == ccy}
        mr.reconciler = Reconciler(db=ctx.db, ledger=ctx.ledger, executor=mr.operating_executor, broker=mr.operating_broker,
                                   flags=ctx.flags, incidents=ctx.incidents, clock=ctx.clock, book_ids=[OPERATING], instruments=insts,
                                   cash_check=len(accounts) <= 1)
    return mr.reconciler


async def update_broker_health(ctx: AppContext, market: str) -> CheckItem:
    mr = ctx.markets.get(market)
    if mr is None or mr.operating_broker is None:
        return CheckItem("계좌 연결", False, mr.broker_reason if mr else "시장 비활성")
    ac = await mr.operating_broker.check_account()
    now = to_iso(ctx.clock.now())
    ctx.db.execute(
        "INSERT INTO broker_health(account_id, broker, last_ok_at, last_error_at, last_error, auth_ok, clock_skew_sec, detail_json, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
        "last_ok_at=COALESCE(excluded.last_ok_at, broker_health.last_ok_at), last_error_at=COALESCE(excluded.last_error_at, broker_health.last_error_at), "
        "last_error=excluded.last_error, auth_ok=excluded.auth_ok, clock_skew_sec=excluded.clock_skew_sec, detail_json=excluded.detail_json, "
        "updated_at=excluded.updated_at",
        (mr.operating_account if ctx.mode in ("offline_demo", "internal_paper") else mr.operating_broker.account_id,
         mr.operating_broker.name, now if ac.ok else None, None if ac.ok else now, ac.error, int(ac.auth_ok),
         ac.clock_skew_sec, dumps(ac.detail), now),
    )
    key = f"auth_error:{mr.operating_broker.account_id}"
    if ac.ok and ctx.flags.get(key):
        ctx.flags.clear(key, "system", "계좌 확인 성공")
    return CheckItem("계좌 연결·인증", ac.ok, ac.error or json.dumps(ac.detail, ensure_ascii=False, default=str))


async def live_readiness(ctx: AppContext, market: str, *, ack_no_withdraw: bool = False, run_order_test: bool = True) -> list[CheckItem]:
    s = ctx.settings
    items: list[CheckItem] = []
    items.append(CheckItem("프로세스 모드 = live", ctx.mode == "live", f"현재 {ctx.mode}"))
    ms = s.markets.get(market)  # type: ignore[call-overload]
    items.append(CheckItem("시장 활성화(설정)", bool(ms and ms.enabled), market))
    mr = ctx.markets.get(market)
    broker = mr.operating_broker if mr else None
    items.append(CheckItem("실계좌 어댑터", broker is not None and broker.is_live_money, mr.broker_reason if mr else "-"))
    items.append(CheckItem("출금 권한 없는 키 사용(사용자 확인)", ack_no_withdraw,
                           "API 키에 출금 권한을 주지 마세요(업비트는 서브포켓 전용 키 권장). 코드가 권한을 확인할 수 없어 사용자 확인 필요."))
    last = ctx.db.query_one("SELECT ts, ok FROM selftest_runs ORDER BY id DESC LIMIT 1")
    fresh = bool(last and last["ok"] and ctx.clock.now() - parse_iso(last["ts"]) < timedelta(hours=24))  # type: ignore[operator]
    items.append(CheckItem("실행무결성 자체검증(24시간 이내 통과)", fresh, "`aifund selftest` 실행" if not fresh else f"통과 {last['ts']}"))
    if broker is None or ms is None or mr is None or mr.collector is None:
        _record(ctx, market, items)
        return items
    items.append(await update_broker_health(ctx, market))
    try:
        insts = await mr.collector.refresh_instruments(market, ms.instruments, force=True)
        for inst in insts:
            ok = inst.status == "active"
            items.append(CheckItem(f"상품 {inst.instrument_id}", ok, f"상태 {inst.status}, 최소주문 {inst.min_notional} {inst.quote_ccy}"))
            v, why = ctx.fx.to_krw(inst.min_notional, inst.quote_ccy, s.risk.max_fx_age_hours)
            if v is not None and v > s.risk.max_order_notional_krw:
                items.append(CheckItem(f"{inst.instrument_id} 예산 내 주문 가능", False, "최소 주문 금액이 1회 주문 한도보다 큼 → 제외 필요"))
        quotes = ctx.market_store.save_quotes(await mr.collector.source.quotes(insts))
        skew = None
        from aifund.brokers.upbit import UpbitMarketData

        if isinstance(mr.data, UpbitMarketData):
            skew = mr.data.http.last_clock_skew
        if skew is not None:
            items.append(CheckItem("시계 오차", abs(skew) <= s.risk.max_clock_skew_sec, f"{skew:+.2f}초"))
        if insts and quotes:
            inst, q = insts[0], quotes[0]
            price = q.ask or q.last or D(0)
            cash = await broker.orderable_cash(inst, price)
            items.append(CheckItem("주문 가능 금액", cash >= inst.min_notional, f"{cash} {inst.quote_ccy} (봇 원금 한도 {s.risk.principal_cap_krw:,.0f}원과 별개로 실제 가능 금액 안에서만 주문)"))
            if run_order_test and broker.capabilities.order_test and q.bid:
                px = floor_step(q.bid * D("0.8"), inst.tick_for(q.bid * D("0.8")))
                qty = ceil_step(inst.min_notional * D("1.2") / px, inst.qty_step)
                r = await broker.test_order(OrderRequest(client_order_id(), inst, Side.BUY, qty, px))
                items.append(CheckItem("주문 형식 검증(거래소 테스트 API, 실제 주문 없음)", r.outcome == "accepted",
                                       r.error_message or "통과"))
            elif not broker.capabilities.order_test:
                items.append(CheckItem("주문 형식 검증", True, "이 거래소는 테스트 API가 없어 생략(계약 테스트로 대체)", blocking=False))
    except BrokerError as exc:
        items.append(CheckItem("거래소 조회", False, str(exc)))
    rec = reconciler_for(ctx, market)
    if rec is not None:
        has_baseline = ctx.db.scalar("SELECT COUNT(*) FROM account_baselines WHERE account_id=?", (broker.account_id,)) > 0
        res = await rec.run("live_readiness", record_flags=False)
        foreign = [m for m in res.mismatches if "모르는 미체결" in m]
        qty_mm = [m for m in res.mismatches if "수량 불일치" in m]
        other = [m for m in res.mismatches if m not in foreign and m not in qty_mm]
        items.append(CheckItem("외부 미체결 주문 없음(소유분 구분 가능)", not foreign, "; ".join(foreign) or "없음"))
        if has_baseline:
            items.append(CheckItem("보유 수량 대사", not qty_mm, "; ".join(qty_mm) or "일치"))
        else:
            prefix = ("; ".join(qty_mm) + " → ") if qty_mm else ""
            items.append(CheckItem("기존 보유분 처리", True,
                                   prefix + "활성화 시 현재 보유분을 '기존 보유분'으로 기록하며 봇은 이를 매도하지 않음", blocking=False))
        items.append(CheckItem("현금·조회 대사", not other, "; ".join(other) or "정상"))
        unknown = mr.operating_executor.unknown_count() if mr.operating_executor else 0
        items.append(CheckItem("상태 불명 주문 없음", unknown == 0, f"{unknown}건"))
    _record(ctx, market, items)
    return items


def _record(ctx: AppContext, market: str, items: list[CheckItem]) -> None:
    ms = ctx.settings.markets.get(market)  # type: ignore[call-overload]
    ok = all(i.ok for i in items if i.blocking)
    ctx.db.execute("INSERT INTO live_checks(market, account_id, ts, ok, items_json) VALUES (?,?,?,?,?)",
                   (market, ms.account_id if ms else "-", to_iso(ctx.clock.now()), int(ok), dumps([asdict(i) for i in items])))


async def enable_live(ctx: AppContext, market: str, phrase: str, actor: str, *, ack_no_withdraw: bool) -> tuple[bool, list[CheckItem], str]:
    ms = ctx.settings.markets[market]  # type: ignore[index]
    expected = confirm_phrase(market, ms.account_id)
    if phrase.strip() != expected:
        return False, [], f"확인 문구 불일치. 정확히 입력: {expected}"
    items = await live_readiness(ctx, market, ack_no_withdraw=ack_no_withdraw)
    failed = [i for i in items if i.blocking and not i.ok]
    if failed:
        return False, items, "사전 점검 실패: " + "; ".join(f"{i.name}({i.detail})" for i in failed)
    rec = reconciler_for(ctx, market)
    if rec is None:
        return False, items, "계좌 연결이 없어 활성화할 수 없습니다"
    try:
        allocated = await allocate_live_principal(ctx, market, actor)
    except RuntimeError as exc:
        return False, items, str(exc)
    await rec.capture_baseline(actor, "LIVE 활성화 시점 기존 보유분(봇이 매도하지 않음)")
    res = await rec.run("live_enable")
    if not res.ok:
        return False, items, "기준 보유분 기록 후 대사 실패: " + "; ".join(res.mismatches)
    ctx.startup_reconciled[rec.broker.account_id] = True
    ctx.activations.activate(market, ctx.settings, ctx.settings_version, actor, phrase)
    return True, items, (f"{market} LIVE 활성화 완료(계좌 {ms.account_id}, 종목 {', '.join(ms.instruments)}, "
                         f"봇 배정 {allocated})")


async def allocate_live_principal(ctx: AppContext, market: str, actor: str) -> str:
    """LIVE 활성화 시 봇 원금 배정: min(시장 배정액, 실제 주문가능금액). 이미 배정된 만큼은 다시 배정하지 않는다.

    입금이 있어도 자동으로 늘지 않으며, 늘리려면 설정(allocation_krw)을 바꾸고 다시 활성화해야 한다.
    외화(USD) 배정은 계좌에 이미 있는 USD를 봇 몫으로 기록하는 회계 처리일 뿐 실제 환전이 아니다.
    """
    from aifund.core.money import ZERO, D
    from aifund.core.timeutil import to_iso as _iso

    s = ctx.settings
    ms = s.markets[market]  # type: ignore[index]
    mr = ctx.markets[market]
    broker = mr.operating_broker
    assert broker is not None and mr.collector is not None
    insts = await mr.collector.refresh_instruments(market, ms.instruments)
    if not insts:
        raise RuntimeError("허용 종목 정보를 불러오지 못했습니다")
    inst = insts[0]
    q = ctx.market_store.latest_quote(inst.instrument_id)
    price = (q.ask or q.last) if q else None
    orderable = await broker.orderable_cash(inst, price or D(1))
    rows = ctx.db.query("SELECT delta FROM ledger_entries WHERE book_id=? AND kind='principal' AND ref_type='live_alloc' AND ref_id=?",
                        (OPERATING, market))
    already_krw = sum((D(r["delta"]) for r in rows), ZERO)
    target_krw = ms.allocation_krw - already_krw
    if target_krw <= 0:
        return f"기존 배정 유지({already_krw:,.0f}원)"
    now = _iso(ctx.clock.now())
    if inst.quote_ccy == "KRW":
        amount_krw = min(target_krw, orderable)
        if amount_krw < inst.min_notional:
            raise RuntimeError(f"주문 가능 금액({orderable})이 최소 주문 금액보다 작아 배정할 수 없습니다")
        with ctx.db.tx() as c:
            c.execute("INSERT INTO ledger_entries(book_id, ts, kind, strategy_id, asset, delta, ref_type, ref_id, note) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (OPERATING, now, "principal", "_book", "KRW", str(amount_krw), "live_alloc", market,
                       f"LIVE 활성화 배정({actor}): min(배정 {ms.allocation_krw}, 주문가능 {orderable})"))
        text = f"{amount_krw:,.0f}원"
    else:
        st = ctx.fx.status(s.risk.max_fx_age_hours)
        if not st.fresh or st.rate is None:
            raise RuntimeError(f"환율이 없거나 오래되어 외화 배정 불가: {st.reason}")
        usd = min(target_krw / st.rate.rate, orderable)
        amount_krw = usd * st.rate.rate
        with ctx.db.tx() as c:
            c.execute("INSERT INTO ledger_entries(book_id, ts, kind, strategy_id, asset, delta, ref_type, ref_id, note) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (OPERATING, now, "principal", "_book", "KRW", str(amount_krw), "live_alloc", market,
                       f"LIVE 활성화 배정(원화 환산, 환율 {st.rate.rate} {st.rate.source})"))
            c.execute("INSERT INTO ledger_entries(book_id, ts, kind, strategy_id, asset, delta, ref_type, ref_id, note) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (OPERATING, now, "fx_alloc", "_book", "KRW", str(-amount_krw), "live_alloc", market, "회계상 원화→USD 배정"))
            c.execute("INSERT INTO ledger_entries(book_id, ts, kind, strategy_id, asset, delta, ref_type, ref_id, note) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (OPERATING, now, "fx_alloc", "_book", "USD", str(usd), "live_alloc", market,
                       "계좌 보유 USD를 봇 몫으로 기록(실제 환전 아님)"))
        text = f"{usd:,.2f} USD(≈{amount_krw:,.0f}원)"
    ctx.equity.adjust_for_flow(OPERATING, amount_krw, f"{market} LIVE 배정")
    return text


def disable_live(ctx: AppContext, market: str, actor: str, reason: str) -> str:
    return "LIVE 비활성화" if ctx.activations.deactivate(market, actor, reason or "사용자 요청") else "이미 비활성 상태"

