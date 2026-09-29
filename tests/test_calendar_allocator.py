"""거래 달력(휴장·서머타임)과 포트폴리오 조정기(상계·한도) 단위 테스트."""

from datetime import datetime
from decimal import Decimal

from aifund.core.timeutil import KST, NEW_YORK, UTC
from aifund.domain.models import Action
from aifund.ledger.ledger import PositionRow
from aifund.markets.calendar import session_info
from aifund.portfolio import allocator
from aifund.portfolio.allocator import TargetInput
from helpers import inst


def test_krx_holiday_and_session():
    assert session_info("kr_stock", datetime(2026, 9, 29, 10, 0, tzinfo=KST).astimezone(UTC)).is_open
    chuseok = session_info("kr_stock", datetime(2026, 9, 25, 10, 0, tzinfo=KST).astimezone(UTC))
    assert not chuseok.is_open and "공휴일" in chuseok.reason
    assert "주말" in session_info("kr_stock", datetime(2026, 9, 26, 10, 0, tzinfo=KST).astimezone(UTC)).reason


def test_nyse_dst_and_early_close():
    # 서머타임 시작(2026-03-08) 전후로 UTC 마감 시각이 달라진다
    before = session_info("us_stock", datetime(2026, 3, 6, 10, 0, tzinfo=NEW_YORK).astimezone(UTC))
    after = session_info("us_stock", datetime(2026, 3, 9, 10, 0, tzinfo=NEW_YORK).astimezone(UTC))
    assert before.session_close.hour == 21 and after.session_close.hour == 20
    assert not session_info("us_stock", datetime(2026, 11, 26, 11, 0, tzinfo=NEW_YORK).astimezone(UTC)).is_open  # 추수감사절
    early = session_info("us_stock", datetime(2026, 11, 27, 13, 30, tzinfo=NEW_YORK).astimezone(UTC))
    assert not early.is_open  # 추수감사절 다음날 13:00 조기폐장


def test_crosses_net_conflicting_strategies():
    i = inst()
    pos = [PositionRow("b", "trend_sma", i.instrument_id, Decimal(10), Decimal(100000), Decimal(0), Decimal(0), None)]
    targets = [TargetInput("trend_sma", i.instrument_id, Action.SELL, Decimal(0), "추세 이탈"),
               TargetInput("mean_reversion", i.instrument_id, Action.BUY, Decimal("0.5"), "과매도")]
    plan = allocator.plan(targets=targets, positions=pos, sleeve_equity_quote=lambda s, iid: Decimal(120000),
                          ref_price=lambda iid: Decimal(10000), instruments={i.instrument_id: i},
                          rebalance_threshold_quote=lambda iid: Decimal(10000), max_order_quote=lambda iid: Decimal(100000))
    assert len(plan.crosses) == 1
    cr = plan.crosses[0]
    assert cr.from_strategy == "trend_sma" and cr.to_strategy == "mean_reversion" and cr.qty == Decimal(6)
    assert len(plan.orders) == 1 and plan.orders[0].side == "sell" and plan.orders[0].qty == Decimal(4)


def test_buy_capped_by_max_order_and_blocked_reason():
    i = inst()
    targets = [TargetInput("trend_sma", i.instrument_id, Action.BUY, Decimal(1), "매수")]
    plan = allocator.plan(targets=targets, positions=[], sleeve_equity_quote=lambda s, iid: Decimal(200000),
                          ref_price=lambda iid: Decimal(10000), instruments={i.instrument_id: i},
                          rebalance_threshold_quote=lambda iid: Decimal(10000), max_order_quote=lambda iid: Decimal(98000))
    assert plan.orders[0].qty * Decimal(10000) <= Decimal(98000)
    blocked = allocator.plan(targets=targets, positions=[], sleeve_equity_quote=lambda s, iid: Decimal(200000),
                             ref_price=lambda iid: Decimal(10000), instruments={i.instrument_id: i},
                             rebalance_threshold_quote=lambda iid: Decimal(10000), max_order_quote=lambda iid: None,
                             block_buys_reason=lambda iid: "AI 사용 불가")
    assert not blocked.orders and blocked.proposals[0].status == "rejected"


def test_wait_is_valid_decision():
    i = inst()
    plan = allocator.plan(targets=[TargetInput("trend_sma", i.instrument_id, Action.WAIT, Decimal(0), "관망")], positions=[],
                          sleeve_equity_quote=lambda s, iid: Decimal(100000), ref_price=lambda iid: Decimal(10000),
                          instruments={i.instrument_id: i}, rebalance_threshold_quote=lambda iid: Decimal(10000),
                          max_order_quote=lambda iid: None)
    assert not plan.orders and plan.proposals[0].status == "adopted"
