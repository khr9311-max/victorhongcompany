"""검증 3: 동시 전략 주문·재시도·프로세스 중복으로 자금 한도 초과나 중복 주문이 발생하지 않는가."""

import asyncio
import threading
from decimal import Decimal

import pytest

from aifund.core.lock import LockHeldError, ProcessLock, is_locked
from aifund.data.store import MarketStore
from aifund.db.database import Database
from aifund.domain.models import Side
from aifund.execution.executor import OrderExecutor, ReservationDenied
from aifund.ledger.ledger import Ledger
from helpers import make_env, trade


def test_concurrent_async_reservations_respect_cap(tmp_path):
    env = make_env(tmp_path)
    intents = [env.intent("9") for _ in range(8)]  # 각 약 9만원
    results = asyncio.run(_gather([env.executor.execute(i) for i in intents]))
    created = [r for r in results if r[0]]
    total = sum(Decimal(r["amount_initial"]) for r in env.db.query("SELECT amount_initial FROM reservations"))
    assert total <= Decimal(300000)
    assert len(created) == 3


async def _gather(coros):
    return await asyncio.gather(*coros)


def test_threads_with_separate_connections(tmp_path):
    """서로 다른 커넥션(다른 스레드·프로세스와 동일 조건)에서 동시에 예약해도 한도를 넘지 않는다."""
    env = make_env(tmp_path)
    path = env.db.path
    errors, ok = [], []
    barrier = threading.Barrier(6)

    def worker() -> None:
        db = Database(path)
        ex = OrderExecutor(db=db, ledger=Ledger(db, env.clock), broker=env.broker, store=MarketStore(db, env.clock),
                           fx=env.fx, flags=env.flags, incidents=env.incidents, clock=env.clock, settings_fn=lambda: env.settings,
                           mode="internal_paper")
        it = env.intent("9")
        barrier.wait()
        try:
            ok.append(ex.reserve_and_create(it))
        except ReservationDenied as exc:
            errors.append(str(exc))
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ok) == 3 and len(errors) == 3
    total = sum(Decimal(r["amount_initial"]) for r in env.db.query("SELECT amount_initial FROM reservations"))
    assert total <= Decimal(300000)


def test_retry_same_intent_is_idempotent(tmp_path):
    env = make_env(tmp_path)
    it = env.intent("2")
    a = asyncio.run(env.executor.execute(it))
    b = asyncio.run(env.executor.execute(it))
    assert a[0] == b[0]
    assert env.db.scalar("SELECT COUNT(*) FROM orders") == 1
    assert env.broker.submit_calls == 1


def test_process_lock_prevents_second_service(tmp_path):
    p = tmp_path / "run" / "service.lock"
    with ProcessLock(p):
        assert is_locked(p)
        with pytest.raises(LockHeldError):
            ProcessLock(p).acquire()
    assert not is_locked(p)


def test_sell_reservation_prevents_double_sell(tmp_path):
    env = make_env(tmp_path)
    env.broker.default_script = [[trade("x1", "5")]]
    asyncio.run(env.executor.execute(env.intent("5")))
    asyncio.run(env.executor.poll())
    assert env.ledger.book_qty("b", env.inst.instrument_id) == Decimal(5)
    env.broker.default_script = None
    s1 = env.intent("4", side=Side.SELL)
    s2 = env.intent("4", side=Side.SELL)
    r1 = asyncio.run(env.executor.execute(s1))
    r2 = asyncio.run(env.executor.execute(s2))
    assert r1[0] is not None and r2[0] is None and "매도 가능 수량 부족" in r2[1]
