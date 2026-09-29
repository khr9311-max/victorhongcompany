"""짧은 실행무결성 자체검증(LIVE 활성화 전제 조건).

실제 주문 실행기·원장·가드 코드를 임시 DB와 가짜 거래소로 돌려 핵심 장애 시나리오를 확인한다.
수익성 검증이 아니다. 몇 초 안에 끝나며 실거래소에는 아무 요청도 보내지 않는다.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from aifund.brokers.fake import FakeBroker
from aifund.config.settings import Settings
from aifund.control.flags import Flags, Incidents
from aifund.control.live import LiveActivations, LiveGuardedBroker
from aifund.core.ids import new_id
from aifund.core.money import D
from aifund.core.timeutil import UTC, ManualClock, to_iso
from aifund.data.fx import FxService
from aifund.data.store import MarketStore
from aifund.db.database import Database, dumps
from aifund.domain.models import Instrument, OrderStatus, Quote, Side, TradeFill
from aifund.execution.executor import OrderExecutor, OrderIntent
from aifund.ledger.ledger import Ledger
from aifund.markets.rules import UPBIT_VOLUME_STEP


@dataclass
class SelfTestResult:
    name: str
    ok: bool
    detail: str


_OPEN: list[Database] = []


class _Env:
    def __init__(self, tmp: Path, broker: FakeBroker | None = None) -> None:
        self.db = Database(tmp / f"{new_id('st')}.sqlite3")
        _OPEN.append(self.db)
        self.db.migrate()
        self.clock = ManualClock(datetime(2026, 1, 5, 1, 0, tzinfo=UTC))
        self.settings = Settings()
        self.store = MarketStore(self.db, self.clock)
        self.fx = FxService(self.db, clock=self.clock, provider="none")
        self.ledger = Ledger(self.db, self.clock)
        self.ledger.create_book("b", kind="operating", setting="A", principal_krw=D(300000), virtual=True, account_id="fake",
                                description="selftest")
        self.inst = Instrument("crypto:KRW-TEST", "crypto", "KRW-TEST", "FAKE", "TEST", "KRW", "TEST", "upbit_krw", None,
                               UPBIT_VOLUME_STEP, D(5000), None, "active", "selftest")
        self.store.save_instruments([self.inst])
        self.store.save_quotes([Quote(self.inst.instrument_id, D(9990), D(10000), D(100), D(100), D(9995), D(10**10),
                                      self.clock.now(), self.clock.now(), "selftest")])
        self.broker = broker or FakeBroker("fake")
        self.flags = Flags(self.db, self.clock)
        self.executor = OrderExecutor(db=self.db, ledger=self.ledger, broker=self.broker, store=self.store, fx=self.fx,
                                      flags=self.flags, incidents=Incidents(self.db, self.clock), clock=self.clock,
                                      settings_fn=lambda: self.settings, mode="internal_paper")

    def intent(self, qty: str = "5", side: Side = Side.BUY, legs: list[tuple[str, Decimal]] | None = None) -> OrderIntent:
        iid = new_id("int")
        self.db.execute("INSERT INTO intents(intent_id, book_id, instrument_id, side, qty, ref_price, notional_krw, risk_increasing, "
                        "purpose, status, allocations_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (iid, "b", self.inst.instrument_id, side.value, qty, "10000", "50000", int(side == Side.BUY), "rebalance",
                         "approved", "[]", to_iso(self.clock.now())))
        return OrderIntent(iid, "b", "fake", "crypto", self.inst, side, D(qty), D(10000), D(10000),
                           legs or [("trend_sma", D(qty))], side == Side.BUY)


def _t(i: str, q: str, p: str = "10000") -> TradeFill:
    return TradeFill(i, D(q), D(p), D("0"), None)


async def _normal(tmp: Path) -> str:
    e = _Env(tmp)
    e.broker.default_script = [[_t("a1", "5")]]
    oid, st = await e.executor.execute(e.intent())
    await e.executor.poll()
    row = e.executor.order(oid)  # type: ignore[arg-type]
    assert row["status"] == "filled", row["status"]
    assert e.ledger.book_qty("b", e.inst.instrument_id) == D(5)
    assert not e.ledger.verify("b")
    return "정상 체결·원장 반영"


async def _lost_response(tmp: Path) -> str:
    e = _Env(tmp)
    e.broker.lose_next_response = True
    e.broker.default_script = [[_t("b1", "5")]]
    it = e.intent()
    oid, st = await e.executor.execute(it)
    assert st == "unknown", st
    oid2, _ = await e.executor.execute(it)  # 같은 의도 재시도
    assert oid2 == oid and e.broker.submit_calls == 1, "재주문 발생"
    await e.executor.poll()
    row = e.executor.order(oid)  # type: ignore[arg-type]
    assert row["status"] == "filled" and row["broker_order_id"], dict(row)
    assert e.broker.submit_calls == 1
    return "응답 유실 → 조회로 연결, 재주문 없음"


async def _partial_cancel(tmp: Path) -> str:
    e = _Env(tmp)
    e.broker.default_script = [[_t("c1", "2")], [_t("c2", "1")], []]
    oid, _ = await e.executor.execute(e.intent())
    await e.executor.poll()
    assert e.executor.order(oid)["status"] == "partially_filled"  # type: ignore[index]
    msg = await e.executor.request_cancel(oid, "테스트")  # type: ignore[arg-type]
    assert e.executor.order(oid)["status"] == "cancel_pending", msg  # type: ignore[index]
    await e.executor.poll()  # 취소 대기 중 추가 체결(c2)
    await e.executor.poll()  # 거래소 취소 확정
    row = e.executor.order(oid)  # type: ignore[arg-type]
    assert row["status"] == "canceled", row["status"]
    assert D(row["filled_qty"]) == D(3)
    rsv = e.db.query_one("SELECT status, amount_remaining FROM reservations WHERE order_id=?", (oid,))
    assert rsv["status"] == "released" and D(rsv["amount_remaining"]) == 0
    return "부분체결 → 취소요청(대기) 중 추가체결 → 취소 확정 후 예약 해제"


async def _dup_events(tmp: Path) -> str:
    e = _Env(tmp)
    e.broker.default_script = [[_t("d1", "2")], [_t("d1", "2"), _t("d2", "3")]]
    oid, _ = await e.executor.execute(e.intent())
    await e.executor.poll()
    await e.executor.poll()
    row = e.executor.order(oid)  # type: ignore[arg-type]
    assert D(row["filled_qty"]) == D(5) and row["status"] == "filled", dict(row)
    n = e.db.scalar("SELECT COUNT(*) FROM fills WHERE order_id=?", (oid,))
    assert n == 2, n
    # 누적값만 주는 거래소에서 순서 역전(누적 감소) 무시
    from aifund.domain.models import BrokerOrderState

    e2 = _Env(tmp)
    oid2, _ = await e2.executor.execute(e2.intent())
    st_hi = BrokerOrderState(None, oid2, OrderStatus.PARTIALLY_FILLED, D(3), D(30000), None, None, "wait")
    st_lo = BrokerOrderState(None, oid2, OrderStatus.PARTIALLY_FILLED, D(1), D(10000), None, None, "wait")
    e2.executor.apply_state(oid2, st_hi)  # type: ignore[arg-type]
    e2.executor.apply_state(oid2, st_lo)  # type: ignore[arg-type]
    assert D(e2.executor.order(oid2)["filled_qty"]) == D(3)  # type: ignore[index]
    return "중복 체결 이벤트 제거·누적 역전 무시"


async def _concurrency(tmp: Path) -> str:
    e = _Env(tmp)
    e.settings.risk.gross_exposure_cap_krw = D(300000)
    intents = [e.intent("9") for _ in range(6)]  # 각 약 9만원 × 6 > 30만원
    results = await asyncio.gather(*(e.executor.execute(i) for i in intents))
    created = [r for r in results if r[0] is not None]
    total = sum((D(r["amount_initial"]) for r in e.db.query("SELECT amount_initial FROM reservations")), D(0))
    assert total <= D(300000), total
    assert len(created) == 3, len(created)
    return f"동시 6건 중 {len(created)}건만 예약(합계 {total:,.0f}원 ≤ 한도)"


async def _restart(tmp: Path) -> str:
    e = _Env(tmp)
    oid_a = e.executor.reserve_and_create(e.intent())
    oid_b = e.executor.reserve_and_create(e.intent())
    e.db.execute("UPDATE orders SET submit_attempted_at=? WHERE order_id=?", (to_iso(e.clock.now()), oid_b))
    out = e.executor.recover_pending()
    assert e.executor.order(oid_a)["status"] == "rejected"  # type: ignore[index]
    assert e.executor.order(oid_b)["status"] == "unknown"  # type: ignore[index]
    assert out == {"never_sent": 1, "to_unknown": 1}
    return "재시작: 미전송 확정/전송중단은 상태불명으로 전환"


async def _live_guard(tmp: Path) -> str:
    e = _Env(tmp)
    inner = FakeBroker("live-acct", live_money=True)
    guarded = LiveGuardedBroker(inner, "crypto", "live", LiveActivations(e.db, e.clock), lambda: e.settings)
    e.executor.broker = guarded
    e.executor.account_id = "live-acct"
    oid, st = await e.executor.execute(e.intent())
    assert st == "rejected", st
    assert inner.submit_calls == 0
    return "LIVE 미활성: 실주문 어댑터 호출 0회"


CHECKS: list[tuple[str, Callable[[Path], Awaitable[str]]]] = [
    ("정상 체결", _normal),
    ("응답 유실", _lost_response),
    ("부분체결·취소", _partial_cancel),
    ("중복·역전 이벤트", _dup_events),
    ("동시 예약", _concurrency),
    ("재시작 복구", _restart),
    ("LIVE 가드", _live_guard),
]


async def run_selftest(record_db: Database | None = None, code_version: str = "") -> list[SelfTestResult]:
    out: list[SelfTestResult] = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        tmp = Path(d)
        for name, fn in CHECKS:
            try:
                out.append(SelfTestResult(name, True, await fn(tmp)))
            except AssertionError as exc:
                out.append(SelfTestResult(name, False, f"실패: {exc}"))
            except Exception as exc:  # pragma: no cover - 예기치 못한 오류도 실패로 기록
                out.append(SelfTestResult(name, False, f"오류: {exc!r}"))
            finally:
                while _OPEN:
                    _OPEN.pop().close()
    if record_db is not None:
        from aifund.core.timeutil import utcnow

        record_db.execute("INSERT INTO selftest_runs(ts, ok, code_version, detail_json) VALUES (?,?,?,?)",
                          (to_iso(utcnow()), int(all(r.ok for r in out)), code_version,
                           dumps([r.__dict__ for r in out])))
    return out
