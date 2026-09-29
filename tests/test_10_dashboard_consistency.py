"""검증 10: 화면의 숫자가 원장과 일치하고 제안부터 체결까지 추적되는가."""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from aifund.core.money import fmt_krw
from aifund.core.paths import mode_paths
from aifund.core.timeutil import UTC, ManualClock
from aifund.service.context import build_context
from aifund.service.simulate import simulate
from aifund.web import views
from aifund.web.app import create_app


def test_numbers_match_ledger_and_trace(home):
    ctx = build_context(mode_paths("offline_demo", home).ensure(), clock=ManualClock(datetime.now(UTC) - timedelta(hours=60)))
    asyncio.run(simulate(ctx, 48))
    cap = views.capital(ctx)
    # 원장에서 직접 계산
    cash = ctx.ledger.cash("operating")
    pos_value = sum((p.qty * ctx.price(p.instrument_id) for p in ctx.ledger.positions("operating")), Decimal(0))
    assert cap["cash"] == cash
    assert cap["positions"] == pos_value
    assert cap["equity"] == cash + pos_value
    assert cap["pnl"] == cash + pos_value - Decimal(300000)
    fees = sum((Decimal(r["fees"]) for r in ctx.db.query("SELECT fees FROM orders WHERE book_id='operating'")), Decimal(0))
    assert abs(cap["fees"] - fees) < Decimal("0.000001")
    # 전략별 합계 = 장부 실제 손익
    by = cap["by_instrument"]
    assert sum(by.values(), Decimal(0)) == pos_value
    client = TestClient(create_app(ctx, None), base_url="http://127.0.0.1:8765")
    html = client.get("/").text
    assert fmt_krw(cap["equity"]) in html and fmt_krw(cap["cash"] - cap["reserved"]) in html
    api = client.get("/api/status").json()
    assert Decimal(api["equity_krw"]) == cap["equity"]
    # 제안 → 의도 → 주문 → 체결 → 원장 추적
    oid = ctx.db.scalar("SELECT o.order_id FROM orders o JOIN intents i ON i.intent_id=o.intent_id "
                        "WHERE o.book_id='operating' AND o.status='filled' AND i.proposal_ids_json != '[]' LIMIT 1")
    if oid is None:
        oid = ctx.db.scalar("SELECT order_id FROM orders WHERE status='filled' LIMIT 1")
    tr = views.order_trace(ctx, oid)
    assert tr["events"][0]["to_status"] == "pending"
    assert tr["fills"] and tr["ledger"]
    assert sum(Decimal(f["qty"]) for f in tr["fills"]) == Decimal(tr["order"]["filled_qty"])
    if tr["proposals"]:
        assert tr["snapshots"], "제안은 스냅샷 ID로 추적되어야 함"
        assert json.loads(tr["intent"]["proposal_ids_json"])
    page = client.get(f"/orders/{oid}").text
    assert oid in page and "원장 반영" in page
