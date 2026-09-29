"""대시보드 화면 데이터. 모든 숫자는 원장·DB에서 직접 계산한다(화면 전용 계산값 없음)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from aifund.brokers.upbit import UpbitMarketData
from aifund.config.settings import scope_hash
from aifund.control.actions import preview_liquidation
from aifund.control.live import confirm_phrase
from aifund.core.money import D, ZERO
from aifund.core.paths import MODE_LABELS
from aifund.core.timeutil import parse_iso
from aifund.db.database import loads
from aifund.domain.models import ACTION_LABELS, STATUS_LABELS
from aifund.evaluation.metrics import book_metrics, comparison
from aifund.ledger.valuation import value_book
from aifund.markets.calendar import session_info
from aifund.service.context import OPERATING, AppContext

BOOK_LABELS = {
    "operating": "운용 장부",
    "shadow_A": "비교 A(규칙 전략만)",
    "shadow_B": "비교 B(+연구 AI)",
    "shadow_C": "비교 C(+연구·검증 AI)",
    "baseline_bh": "기준: 매수·보유",
    "baseline_cash": "기준: 현금 유지",
}
STRATEGY_LABELS = {"trend_sma": "봇 A 추세", "mean_reversion": "봇 B 평균회귀", "ai_research": "AI 슬리브", "_book": "장부 공통"}


def market_rows(ctx: AppContext, runtime: Any) -> list[dict[str, Any]]:
    rows = []
    for market, ms in ctx.settings.markets.items():
        mr = ctx.markets.get(market)
        sess = session_info(market, ctx.clock.now()) if ms.enabled else None
        last_ok = mr.collector.last_ok_at.get(market) if mr and mr.collector else None
        if last_ok is None:
            r = ctx.db.query_one("SELECT created_at FROM snapshots WHERE market=? ORDER BY created_at DESC LIMIT 1", (market,))
            last_ok = parse_iso(r["created_at"]) if r else None
        lq = ctx.db.query_one("SELECT MAX(q.fetched_at) AS t FROM quotes q WHERE q.instrument_id LIKE ?", (market + ":%",))
        act = ctx.activations.active(market)
        live_state = "비활성"
        if act:
            ok, why = ctx.activations.authorized(ctx.mode, market, act["account_id"], None, ctx.settings)
            live_state = "LIVE 활성" if ok else f"재확인 필요: {why}"
        rows.append({
            "market": market, "enabled": ms.enabled, "account": ms.account_id, "broker": ms.broker,
            "instruments": ", ".join(ms.instruments), "data": mr.data_reason if mr else "비활성",
            "broker_status": mr.broker_reason if mr else "비활성", "session": sess.reason if sess else "-",
            "last_snapshot": last_ok, "last_quote": parse_iso(lq["t"]) if lq and lq["t"] else None, "live": live_state,
            "reconciled": ctx.startup_reconciled.get(mr.operating_executor.account_id) if mr and mr.operating_executor else None,
            "caps": mr.operating_broker.capabilities.as_rows() if mr and mr.operating_broker else [],
            "cap_notes": mr.operating_broker.capabilities.notes if mr and mr.operating_broker else (),
        })
    return rows


def capital(ctx: AppContext, book_id: str = OPERATING) -> dict[str, Any]:
    val = value_book(ctx.db, ctx.ledger, book_id, ctx.price, ctx.market_store.instrument, ctx.fx, ctx.settings.risk)
    principal = ctx.ledger.principal(book_id)
    setting = ctx.book_setting(book_id)
    ai_cost = ctx.ai_cost_for_setting(setting)
    fees = -sum((D(r["delta"]) for r in ctx.db.query("SELECT delta FROM ledger_entries WHERE book_id=? AND kind='fee'", (book_id,))), ZERO)
    risk = ctx.equity.state(book_id)
    return {"principal": principal, "cash": val.cash_krw, "cash_by_ccy": val.cash, "reserved": val.reserved_krw,
            "available": val.cash_krw - val.reserved_krw, "positions": val.positions_krw, "equity": val.equity_krw,
            "exposure": val.exposure_krw, "realized": val.realized_krw, "unrealized": val.unrealized_krw, "fees": fees,
            "pnl": val.equity_krw - principal, "ai_cost": ai_cost, "experiment_pnl": val.equity_krw - principal - ai_cost,
            "by_instrument": val.by_instrument, "by_market": val.by_market, "by_class": val.by_class, "stale": val.stale,
            "risk": risk, "setting": setting}


def overview(ctx: AppContext, runtime: Any) -> dict[str, Any]:
    incidents = [dict(r) for r in ctx.db.query("SELECT * FROM incidents ORDER BY id DESC LIMIT 12")]
    orders = [dict(r) for r in ctx.db.query("SELECT * FROM orders WHERE book_id=? ORDER BY created_at DESC LIMIT 10", (OPERATING,))]
    positions = []
    agg: dict[str, dict[str, Any]] = {}
    for p in ctx.ledger.positions(OPERATING):
        a = agg.setdefault(p.instrument_id, {"instrument_id": p.instrument_id, "qty": ZERO, "cost": ZERO, "strategies": []})
        a["qty"] += p.qty
        a["cost"] += p.cost_basis
        a["strategies"].append(f"{STRATEGY_LABELS.get(p.strategy_id, p.strategy_id)} {p.qty}")
    for iid, a in agg.items():
        px = ctx.price(iid)
        inst = ctx.market_store.instrument(iid)
        a["price"] = px
        a["value"] = a["qty"] * px if px else None
        a["ccy"] = inst.quote_ccy if inst else "KRW"
        positions.append(a)
    skew = None
    for mr in ctx.markets.values():
        if isinstance(mr.data, UpbitMarketData):
            skew = mr.data.http.last_clock_skew
    return {
        "mode": ctx.mode, "mode_label": MODE_LABELS[ctx.mode], "status": runtime.status if runtime else "대시보드 단독",
        "errors": runtime.errors if runtime else {}, "last_cycle": runtime.last_cycle if runtime else {},
        "markets": market_rows(ctx, runtime), "capital": capital(ctx), "flags": ctx.flags.all(), "incidents": incidents,
        "orders": orders, "positions": positions, "fx": ctx.fx.age_text(), "clock_skew": skew,
        "ai_status": ctx.ai.availability(), "budget": ctx.budget().month_usage(), "code_version": ctx.code_version,
        "settings_version": ctx.settings_version, "secrets": ctx.secrets.describe(),
    }


def strategies_page(ctx: AppContext) -> dict[str, Any]:
    comp = comparison(ctx)
    op = book_metrics(ctx, OPERATING)
    positions = []
    for b in ("operating", "shadow_A", "shadow_B", "shadow_C", "baseline_bh"):
        for p in ctx.ledger.positions(b):
            px = ctx.price(p.instrument_id)
            positions.append({"book": b, "strategy": p.strategy_id, "instrument": p.instrument_id, "qty": p.qty,
                              "avg": p.avg_cost, "price": px, "value": p.qty * px if px else None,
                              "unreal": (p.qty * px - p.cost_basis) if px else None, "realized": p.realized_pnl,
                              "opened_at": p.opened_at})
    from aifund.strategies import mean_reversion, trend  # noqa: F401
    from aifund.strategies.base import REGISTRY

    strategies = [{"id": sid, "title": cls.title, "hypothesis": cls.hypothesis, "version": cls.version,
                   "enabled": getattr(ctx.settings.strategies, sid).enabled,
                   "params": getattr(ctx.settings.strategies, sid).params, "sleeve": ctx.settings.strategies.sleeves.get(sid)}
                  for sid, cls in REGISTRY.items()]
    signals = [dict(r) for r in ctx.db.query(
        "SELECT * FROM signals WHERE cycle_id=(SELECT cycle_id FROM signals ORDER BY id DESC LIMIT 1) ORDER BY strategy_id, instrument_id")]
    return {"comparison": comp, "operating": op, "positions": positions, "strategies": strategies, "signals": signals,
            "sleeves": ctx.settings.strategies.sleeves}


def research_page(ctx: AppContext) -> dict[str, Any]:
    runs = [dict(r) for r in ctx.db.query("SELECT * FROM ai_runs ORDER BY started_at DESC LIMIT 30")]
    reports = []
    for r in ctx.db.query("SELECT * FROM ai_reports ORDER BY created_at DESC LIMIT 12"):
        d = dict(r)
        data = loads(r["report_json"], {})
        d["report"] = data.get("report", {})
        bundle = data.get("bundle") or {}
        d["sources"] = bundle.get("sources_UNTRUSTED_DATA", []) if isinstance(bundle, dict) else []
        d["data_status"] = bundle.get("data_status", []) if isinstance(bundle, dict) else []
        d["errors"] = loads(r["validation_errors"], [])
        reports.append(d)
    proposals = [dict(r) for r in ctx.db.query(
        "SELECT * FROM proposals WHERE book_id=? AND (action NOT IN ('wait','hold') OR status='rejected' OR strategy_id='ai_research') "
        "ORDER BY created_at DESC LIMIT 60", (OPERATING,))]
    for p in proposals:
        p["sources"] = loads(p["sources_json"], [])
        p["counter"] = loads(p["counterarguments_json"], [])
    news = [dict(r) for r in ctx.db.query("SELECT * FROM sources ORDER BY fetched_at DESC LIMIT 20")]
    return {"runs": runs, "reports": reports, "proposals": proposals, "news": news, "availability": ctx.ai.availability(),
            "budget": ctx.budget().month_usage(), "provider": ctx.settings.ai.provider, "model": ctx.settings.ai.model,
            "locked": ctx.locked_settings, "pricing": ctx.settings.ai.pricing.get(ctx.settings.ai.model),
            "feeds": ctx.news.last_results}


def orders_page(ctx: AppContext, book: str | None = None, limit: int = 100) -> dict[str, Any]:
    sql = "SELECT * FROM orders"
    params: tuple = ()
    if book:
        sql += " WHERE book_id=?"
        params = (book,)
    rows = [dict(r) for r in ctx.db.query(sql + " ORDER BY created_at DESC LIMIT ?", (*params, limit))]
    recon = [dict(r) for r in ctx.db.query("SELECT * FROM reconciliations ORDER BY id DESC LIMIT 10")]
    for r in recon:
        r["mismatches"] = loads(r["mismatches_json"], [])
        r["detail"] = loads(r["detail_json"], {})
    intents = [dict(r) for r in ctx.db.query("SELECT * FROM intents WHERE status='rejected' ORDER BY created_at DESC LIMIT 20")]
    for i in intents:
        i["reasons"] = loads(i["risk_reasons_json"], [])
    return {"orders": rows, "recon": recon, "rejected_intents": intents, "book": book}


def order_trace(ctx: AppContext, order_id: str) -> dict[str, Any] | None:
    o = ctx.db.query_one("SELECT * FROM orders WHERE order_id=?", (order_id,))
    if o is None:
        return None
    intent = ctx.db.query_one("SELECT * FROM intents WHERE intent_id=?", (o["intent_id"],)) if o["intent_id"] else None
    pids = loads(intent["proposal_ids_json"], []) if intent else []
    proposals = [dict(r) for r in ctx.db.query(
        f"SELECT * FROM proposals WHERE proposal_id IN ({','.join('?' for _ in pids)})", tuple(pids))] if pids else []
    snap_ids = {p["snapshot_id"] for p in proposals}
    snaps = [dict(r) for r in ctx.db.query(
        f"SELECT snapshot_id, created_at, candle_close_time, status_json FROM snapshots WHERE snapshot_id IN ({','.join('?' for _ in snap_ids)})",
        tuple(snap_ids))] if snap_ids else []
    events = [dict(r) | {"detail": loads(r["detail_json"], {})} for r in
              ctx.db.query("SELECT * FROM order_events WHERE order_id=? ORDER BY id", (order_id,))]
    fills = [dict(r) for r in ctx.db.query("SELECT * FROM fills WHERE order_id=? ORDER BY id", (order_id,))]
    alloc = [dict(r) for r in ctx.db.query("SELECT * FROM order_allocations WHERE order_id=?", (order_id,))]
    ledger = [dict(r) for r in ctx.db.query("SELECT * FROM ledger_entries WHERE ref_id=? ORDER BY id", (order_id,))]
    rsv = ctx.db.query_one("SELECT * FROM reservations WHERE order_id=?", (order_id,))
    return {"order": dict(o), "intent": dict(intent) if intent else None, "proposals": proposals, "snapshots": snaps,
            "events": events, "fills": fills, "alloc": alloc, "ledger": ledger, "reservation": dict(rsv) if rsv else None,
            "risk_reasons": loads(intent["risk_reasons_json"], []) if intent else []}


def control_page(ctx: AppContext) -> dict[str, Any]:
    markets = []
    for market, ms in ctx.settings.markets.items():
        if not ms.enabled:
            continue
        act = ctx.activations.active(market)
        check = ctx.db.query_one("SELECT * FROM live_checks WHERE market=? ORDER BY id DESC LIMIT 1", (market,))
        markets.append({
            "market": market, "account": ms.account_id, "instruments": ms.instruments, "allocation": ms.allocation_krw,
            "active": act, "phrase": confirm_phrase(market, ms.account_id),
            "scope_ok": (act["scope_hash"] == scope_hash(ctx.settings.live_scope(market))) if act else None,
            "check": dict(check) | {"items": loads(check["items_json"], [])} if check else None,
            "liq": preview_liquidation(ctx, market), "halted": ctx.flags.halted(market),
        })
    selftest = ctx.db.query_one("SELECT * FROM selftest_runs ORDER BY id DESC LIMIT 1")
    events = [dict(r) for r in ctx.db.query("SELECT * FROM control_events ORDER BY id DESC LIMIT 25")]
    risk = {b: ctx.equity.state(b) for b in ("operating",)}
    return {"markets": markets, "flags": ctx.flags.all(), "mode": ctx.mode, "risk": ctx.settings.risk,
            "selftest": dict(selftest) | {"items": loads(selftest["detail_json"], [])} if selftest else None,
            "events": events, "risk_state": risk}


def settings_page(ctx: AppContext) -> dict[str, Any]:
    cands = [dict(r) | {"params": loads(r["params_json"], {}), "base": loads(r["base_params_json"], {}),
                        "bt": loads(r["backtest_json"], None)}
             for r in ctx.db.query("SELECT * FROM strategy_candidates ORDER BY created_at DESC LIMIT 20")]
    return {"s": ctx.settings, "version": ctx.settings_version, "history": ctx.store_settings.history(30), "locked": ctx.locked_settings,
            "candidates": cands}


LABELS = {"status": STATUS_LABELS, "action": ACTION_LABELS, "book": BOOK_LABELS, "strategy": STRATEGY_LABELS}


def dec(v: Any) -> Decimal | None:
    if v is None or v == "" or v == "keep":
        return None
    return D(v)
