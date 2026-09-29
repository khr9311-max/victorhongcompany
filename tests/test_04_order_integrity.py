"""검증 4: 전송 후 응답 유실, 부분체결, 취소 중 추가 체결, 이벤트 중복·순서 역전 처리."""

import asyncio
from decimal import Decimal

from aifund.brokers.fake import FakeBroker
from aifund.core.money import D
from aifund.domain.models import BrokerOrderState, OrderStatus
from helpers import make_env, trade


def run(c):
    return asyncio.run(c)


def test_lost_response_resolves_without_resubmit(tmp_path):
    env = make_env(tmp_path)
    env.broker.lose_next_response = True
    env.broker.default_script = [[trade("t1", "5")]]
    it = env.intent("5")
    oid, st = run(env.executor.execute(it))
    assert st == "unknown"
    # 불명 상태에서는 같은 계좌의 위험 증가가 막혀야 한다(위험검사 입력)
    assert env.executor.unknown_count() == 1
    run(env.executor.execute(it))  # 재시도해도 재주문 없음
    assert env.broker.submit_calls == 1
    run(env.executor.poll())
    row = env.executor.order(oid)
    assert row["status"] == "filled" and row["broker_order_id"] == "fake-1"
    assert env.executor.unknown_count() == 0


def test_unknown_confirmed_absent_only_after_repeated_lookups(tmp_path):
    env = make_env(tmp_path)
    oid = env.executor.reserve_and_create(env.intent("5"))
    env.db.execute("UPDATE orders SET status='unknown', submit_attempted_at=?, unknown_since=? WHERE order_id=?",
                   (env.clock.now().isoformat(), env.clock.now().isoformat(), oid))
    assert run(env.executor.resolve_unknown(oid)) == OrderStatus.UNKNOWN  # 1회 조회: 확정 안 함
    env.clock.advance(15)
    assert run(env.executor.resolve_unknown(oid)) == OrderStatus.REJECTED  # 2회·시간 경과 후 미접수 확정
    assert env.db.query_one("SELECT status FROM reservations WHERE order_id=?", (oid,))["status"] == "released"


def test_no_client_id_broker_uses_hint_and_window(tmp_path):
    fb = FakeBroker("acct", client_ids=False)
    env = make_env(tmp_path, broker=fb)
    fb.lose_next_response = True
    fb.default_script = [[trade("k1", "5")]]
    oid, st = run(env.executor.execute(env.intent("5")))
    assert st == "unknown"
    run(env.executor.poll())  # 종목·수량·가격 단서로 유일 매칭
    assert env.executor.order(oid)["status"] == "filled"
    assert fb.submit_calls == 1


def test_partial_fill_then_cancel_with_fill_during_cancel(tmp_path):
    env = make_env(tmp_path)
    env.broker.default_script = [[trade("p1", "2")], [trade("p2", "1")], []]
    oid, _ = run(env.executor.execute(env.intent("5")))
    run(env.executor.poll())
    assert env.executor.order(oid)["status"] == "partially_filled"
    run(env.executor.request_cancel(oid, "test"))
    row = env.executor.order(oid)
    assert row["status"] == "cancel_pending"  # 취소 요청 성공 ≠ 취소 완료
    rsv = env.db.query_one("SELECT status FROM reservations WHERE order_id=?", (oid,))
    assert rsv["status"] == "active"  # 취소 확인 전에는 예약 유지
    run(env.executor.poll())
    assert env.executor.order(oid)["status"] == "cancel_pending"
    assert Decimal(env.executor.order(oid)["filled_qty"]) == 3  # 취소 대기 중 추가 체결 반영
    run(env.executor.poll())
    row = env.executor.order(oid)
    assert row["status"] == "canceled" and Decimal(row["filled_qty"]) == 3
    assert env.db.query_one("SELECT status FROM reservations WHERE order_id=?", (oid,))["status"] == "released"
    assert env.ledger.book_qty("b", env.inst.instrument_id) == 3
    assert env.ledger.verify("b") == []


def test_duplicate_and_out_of_order_events(tmp_path):
    env = make_env(tmp_path)
    oid = env.executor.reserve_and_create(env.intent("5"))
    run(env.executor.submit(oid))
    st1 = BrokerOrderState("fake-1", oid, OrderStatus.PARTIALLY_FILLED, D(2), D(20000), D(0),
                           [trade("a", "2")], "wait")
    st2 = BrokerOrderState("fake-1", oid, OrderStatus.PARTIALLY_FILLED, D(2), D(20000), D(0),
                           [trade("a", "2")], "wait")  # 중복 이벤트
    st3 = BrokerOrderState("fake-1", oid, OrderStatus.FILLED, D(5), D(50000), D(0),
                           [trade("b", "3"), trade("a", "2")], "done")  # 순서가 바뀐 목록
    for s in (st1, st2, st3):
        env.executor.apply_state(oid, s)
    assert env.db.scalar("SELECT COUNT(*) FROM fills WHERE order_id=?", (oid,)) == 2
    assert Decimal(env.executor.order(oid)["filled_qty"]) == 5
    # 종료 뒤 늦게 도착한 이전 상태는 무시
    env.executor.apply_state(oid, st1)
    assert env.executor.order(oid)["status"] == "filled"


def test_cumulative_only_decrease_ignored(tmp_path):
    env = make_env(tmp_path)
    oid = env.executor.reserve_and_create(env.intent("5"))
    run(env.executor.submit(oid))
    hi = BrokerOrderState("x", oid, OrderStatus.PARTIALLY_FILLED, D(4), D(40000), None, None, "wait")
    lo = BrokerOrderState("x", oid, OrderStatus.PARTIALLY_FILLED, D(1), D(10000), None, None, "wait")
    env.executor.apply_state(oid, hi)
    env.executor.apply_state(oid, lo)
    env.executor.apply_state(oid, hi)
    assert Decimal(env.executor.order(oid)["filled_qty"]) == 4
    f = env.db.query("SELECT fee_estimated FROM fills WHERE order_id=?", (oid,))
    assert len(f) == 1 and f[0]["fee_estimated"] == 1  # 누적값만 주는 거래소 수수료는 추정 표시


def test_definite_reject_releases_reservation(tmp_path):
    env = make_env(tmp_path)
    env.broker.reject_next = "insufficient_funds_bid"
    oid, st = run(env.executor.execute(env.intent("5")))
    assert st == "rejected"
    assert env.db.query_one("SELECT status FROM reservations WHERE order_id=?", (oid,))["status"] == "released"
