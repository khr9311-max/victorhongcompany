"""LIVE 활성화 기록과 실주문 가드.

- LIVE는 시장·계좌·상품·허용 종목·투자 한도(scope)를 확인하고 정확한 확인 문구를 입력해야만 켜진다.
- API 키를 넣는 것만으로는 켜지지 않는다. 활성화 이후 범위 내 주문은 건별 승인 없이 실행된다.
- 설정(scope)이 바뀌면 해당 시장의 LIVE는 자동으로 '재확인 필요' 상태가 된다.
- LiveGuardedBroker: 활성화가 없거나 범위를 벗어나면 실제 주문 어댑터의 submit_order에 도달하지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal

from aifund.brokers.base import BrokerAdapter, BrokerCapabilities, OrderLookupHint
from aifund.config.settings import Settings, scope_hash
from aifund.core.timeutil import Clock, to_iso
from aifund.db.database import Database, dumps, loads
from aifund.domain.models import AccountCheck, Balance, BrokerOrderState, CancelResult, Instrument, OrderRequest, SubmitResult

log = logging.getLogger(__name__)


class LiveNotAuthorized(Exception):
    pass


def confirm_phrase(market: str, account_id: str) -> str:
    return f"LIVE {market} {account_id}"


class LiveActivations:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock

    def active(self, market: str) -> dict | None:
        r = self.db.query_one(
            "SELECT * FROM live_activations WHERE market=? AND deactivated_at IS NULL ORDER BY id DESC LIMIT 1", (market,)
        )
        return None if r is None else dict(r)

    def authorized(self, mode: str, market: str, account_id: str, instrument_id: str | None, settings: Settings) -> tuple[bool, str]:
        if mode != "live":
            return False, f"현재 모드 {mode}에서는 실주문 불가"
        act = self.active(market)
        if act is None:
            return False, f"{market} LIVE 미활성화"
        if act["account_id"] != account_id:
            return False, f"활성화 계좌({act['account_id']})와 주문 계좌({account_id}) 불일치"
        cur = scope_hash(settings.live_scope(market))
        if cur != act["scope_hash"]:
            return False, "설정(한도·종목·계좌)이 활성화 이후 변경됨 → LIVE 재확인 필요"
        scope = loads(act["scope_json"], {})
        if instrument_id is not None:
            sym = instrument_id.split(":", 1)[1]
            if sym not in scope.get("instruments", []):
                return False, f"허용 종목 아님: {instrument_id}"
        return True, "LIVE 활성"

    def activate(self, market: str, settings: Settings, settings_version: int, actor: str, phrase: str) -> int:
        ms = settings.markets[market]  # type: ignore[index]
        expected = confirm_phrase(market, ms.account_id)
        if phrase.strip() != expected:
            raise LiveNotAuthorized(f"확인 문구가 일치하지 않습니다. 정확히 입력: {expected}")
        scope = settings.live_scope(market)
        with self.db.tx() as c:
            c.execute(
                "UPDATE live_activations SET deactivated_at=?, deactivated_by=?, deactivation_reason=? WHERE market=? AND deactivated_at IS NULL",
                (to_iso(self.clock.now()), actor, "재활성화로 대체", market),
            )
            cur = c.execute(
                "INSERT INTO live_activations(market, account_id, broker, scope_json, scope_hash, settings_version, confirm_phrase, "
                "activated_at, activated_by) VALUES (?,?,?,?,?,?,?,?,?)",
                (market, ms.account_id, ms.broker, dumps(scope), scope_hash(scope), settings_version, phrase,
                 to_iso(self.clock.now()), actor),
            )
            c.execute("INSERT INTO control_events(ts, actor, action, scope, detail_json) VALUES (?,?,?,?,?)",
                      (to_iso(self.clock.now()), actor, "live_enabled", market, dumps(scope)))
        log.warning("LIVE 활성화: %s %s (%s)", market, ms.account_id, actor)
        return int(cur.lastrowid)

    def deactivate(self, market: str, actor: str, reason: str) -> bool:
        with self.db.tx() as c:
            cur = c.execute(
                "UPDATE live_activations SET deactivated_at=?, deactivated_by=?, deactivation_reason=? WHERE market=? AND deactivated_at IS NULL",
                (to_iso(self.clock.now()), actor, reason, market),
            )
            if cur.rowcount:
                c.execute("INSERT INTO control_events(ts, actor, action, scope, detail_json) VALUES (?,?,?,?,?)",
                          (to_iso(self.clock.now()), actor, "live_disabled", market, dumps({"reason": reason})))
        return cur.rowcount > 0


class LiveGuardedBroker(BrokerAdapter):
    """실거래 어댑터 래퍼. 주문 생성만 가드하고, 조회·취소(위험 감소)는 통과시킨다."""

    def __init__(self, inner: BrokerAdapter, market: str, mode: str, activations: LiveActivations,
                 settings_fn: Callable[[], Settings]) -> None:
        super().__init__(inner.account_id)
        self.inner = inner
        self.market = market
        self.mode = mode
        self.activations = activations
        self.settings_fn = settings_fn
        self.name = inner.name
        self.is_live_money = inner.is_live_money

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self.inner.capabilities

    async def submit_order(self, req: OrderRequest) -> SubmitResult:
        ok, reason = self.activations.authorized(self.mode, self.market, self.account_id, req.instrument.instrument_id,
                                                 self.settings_fn())
        if not ok:
            raise LiveNotAuthorized(reason)
        return await self.inner.submit_order(req)

    async def test_order(self, req: OrderRequest) -> SubmitResult:
        return await self.inner.test_order(req)

    async def check_account(self) -> AccountCheck:
        return await self.inner.check_account()

    async def balances(self) -> dict[str, Balance]:
        return await self.inner.balances()

    async def orderable_cash(self, instrument: Instrument, price: Decimal) -> Decimal:
        return await self.inner.orderable_cash(instrument, price)

    async def get_order(self, *, broker_order_id: str | None, client_order_id: str,
                        hint: OrderLookupHint | None = None) -> BrokerOrderState | None:
        return await self.inner.get_order(broker_order_id=broker_order_id, client_order_id=client_order_id, hint=hint)

    async def cancel_order(self, *, broker_order_id: str | None, client_order_id: str, instrument: Instrument) -> CancelResult:
        return await self.inner.cancel_order(broker_order_id=broker_order_id, client_order_id=client_order_id, instrument=instrument)

    async def open_orders(self, instruments: list[Instrument]) -> list[BrokerOrderState]:
        return await self.inner.open_orders(instruments)

    async def instrument_meta(self, instruments: list[Instrument]) -> list[Instrument]:
        return await self.inner.instrument_meta(instruments)

    async def close(self) -> None:
        await self.inner.close()
