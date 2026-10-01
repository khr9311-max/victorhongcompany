"""미국주식 모의운용 자금: 기존 원화를 달러로 환전하고 양쪽 장부에 함께 기록한다."""

from decimal import Decimal

from aifund.brokers.paper import PaperBroker
from aifund.core.ids import new_id
from aifund.core.money import D, ZERO, floor_step
from aifund.core.timeutil import to_iso
from aifund.ledger.ledger import BOOK_STRATEGY


def fund_us_paper(ctx, book_id, executor) -> Decimal:
    """누적 환전 원금은 시장 배정액 이내. 재시작·손실 후에도 원금을 재입금하지 않는다."""
    broker = executor.broker
    if not isinstance(broker, PaperBroker):
        return ZERO
    ms = ctx.settings.markets.get("us_stock")
    if ms is None or not ms.enabled:
        return ZERO
    fx = ctx.fx.status(ctx.settings.risk.max_fx_age_hours)
    if not fx.fresh or fx.rate is None or not fx.rate.rate.is_finite() or fx.rate.rate <= 0:
        return ZERO
    with ctx.db.tx() as c:
        rows = c.execute("SELECT delta FROM ledger_entries WHERE book_id=? AND ref_type='paper_fx' AND asset='KRW'",
                         (book_id,)).fetchall()
        converted = -sum((D(r[0]) for r in rows), ZERO)
        remaining = max(ZERO, ctx.market_capital(book_id, "us_stock") - converted)
        free_krw, locked_krw = broker._bal(c, "KRW")
        amount = min(remaining, executor.available_cash(book_id, "KRW", c), free_krw)
        usd = floor_step(max(ZERO, amount) / fx.rate.rate, Decimal("0.01"))
        if usd <= 0:
            return ZERO
        spent = usd * fx.rate.rate
        free_usd, locked_usd = broker._bal(c, "USD")
        broker._set(c, "KRW", free_krw - spent, locked_krw)
        broker._set(c, "USD", free_usd + usd, locked_usd)
        ref = new_id("fx")
        note = f"모의 환전, 환율 {fx.rate.rate}, 출처 {fx.rate.source}, 기준 {to_iso(fx.rate.as_of)}; 환전 수수료 미반영"
        for asset, delta in (("KRW", -spent), ("USD", usd)):
            ctx.ledger._entry(c, book_id, to_iso(ctx.clock.now()), "fx", BOOK_STRATEGY, asset, delta,
                              fx.rate.rate, "paper_fx", ref, note)
        return usd
