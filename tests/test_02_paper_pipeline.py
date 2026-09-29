"""검증 2: internal_paper에서 데이터→전략→AI(명시적 비활성)→위험검사→모의주문→체결→손익이 이어지는가."""

import asyncio
from datetime import timedelta
from decimal import Decimal

from aifund.config.settings import Settings
from aifund.config.store import SettingsStore
from aifund.core.paths import mode_paths
from aifund.core.timeutil import ManualClock
from aifund.data.replay import ReplayMarketData
from aifund.db.database import Database
from aifund.service.context import build_context
from aifund.service.cycle import DecisionCycle
from helpers import T0, candles_series


def _prep_settings(home) -> None:
    s = Settings()
    s.ai.enabled = False  # AI 명시적 비활성
    s.markets["crypto"].instruments = ["KRW-TEST"]
    s.strategies.mean_reversion.enabled = False
    db = Database(mode_paths("internal_paper", home).ensure().db_path)
    db.migrate()
    db.set_meta("mode", "internal_paper")
    SettingsStore(db).save(s, "test", "파이프라인 테스트")
    db.close()


def test_paper_pipeline_replay(home):
    _prep_settings(home)
    # 80봉 하락·횡보 후 상승 → 추세 매수 신호, 이후 급락 → 추세 이탈 매도
    closes = [10000 - i * 5 for i in range(70)] + [9650 + i * 60 for i in range(40)] + [12000 - i * 150 for i in range(40)]
    series = candles_series("crypto:KRW-TEST", closes, T0)
    clock = ManualClock(T0)
    replay = ReplayMarketData({"crypto:KRW-TEST": series}, clock, top_size=Decimal("1000"))
    ctx = build_context(mode_paths("internal_paper", home), clock=clock, replay=replay)
    assert ctx.ai.availability() == (False, "AI 비활성(설정)")
    cycle = DecisionCycle(ctx)

    async def run() -> list[str]:
        mr = ctx.markets["crypto"]
        await mr.collector.refresh_instruments("crypto", ["KRW-TEST"])
        ctx.startup_reconciled[mr.operating_executor.account_id] = True
        statuses = []
        for k in range(75, 150):
            clock.set(series[k].close_time + timedelta(seconds=25))
            ctx.market_store.save_quotes(await replay.quotes([ctx.market_store.instrument("crypto:KRW-TEST")]))
            res = await cycle.run("crypto")
            statuses.append(res.status)
            for _ in range(3):
                clock.advance(30)
                ctx.market_store.save_quotes(await replay.quotes([ctx.market_store.instrument("crypto:KRW-TEST")]))
                for ex in ctx.all_executors():
                    await ex.poll()
        return statuses

    statuses = asyncio.run(run())
    assert set(statuses) == {"done"}
    db = ctx.db
    buys = db.query("SELECT * FROM orders WHERE book_id='operating' AND side='buy' AND status='filled'")
    sells = db.query("SELECT * FROM orders WHERE book_id='operating' AND side='sell' AND status='filled'")
    assert buys and sells, "매수와 매도가 모두 체결되어야 함"
    # 추적: 제안 → 의도(위험검사 통과) → 주문 → 체결 → 원장
    o = buys[0]
    intent = db.query_one("SELECT * FROM intents WHERE intent_id=?", (o["intent_id"],))
    assert intent["status"] == "ordered"
    fills = db.query("SELECT * FROM fills WHERE order_id=?", (o["order_id"],))
    assert fills
    # 미래정보 방지: 체결은 주문 생성 이후 시각
    assert all(f["ts"] > o["created_at"] for f in fills)
    # 신호에 쓴 종가가 아니라 이후 호가 기준 불리한 가격으로 체결
    signal_bar = max((c for c in series if c.close_time.isoformat() <= o["created_at"]), key=lambda c: c.close_time)
    assert Decimal(fills[0]["price"]) >= signal_bar.close
    assert ctx.ledger.verify("operating") == []
    # 손익: 수수료가 반영되고, 원금 대비 평가 결과가 원장 합과 일치
    fees = -sum(Decimal(r["delta"]) for r in db.query("SELECT delta FROM ledger_entries WHERE book_id='operating' AND kind='fee'"))
    assert fees > 0
    pos = ctx.ledger.positions("operating", include_closed=True)
    realized = sum((p.realized_pnl for p in pos), Decimal(0))
    cash = ctx.ledger.cash("operating")
    held_cost = sum((p.cost_basis for p in pos), Decimal(0))
    assert cash - Decimal(300000) == realized - held_cost
    # 가상 비교 장부도 같은 스냅샷으로 독립 운용됨
    assert db.scalar("SELECT COUNT(*) FROM orders WHERE book_id='shadow_A'") > 0
