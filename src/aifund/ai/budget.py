"""월 AI 예산. 호출 전 예상 최대 비용을 예약하고, 호출 후 실제 사용량으로 정산한다.

- 요율이 설정되지 않은 모델은 유료 자동 호출을 하지 않는다.
- 환율이 불확실하면 설정의 대체환율로 계산하고 '추정'으로 표시한다.
- AI 운영비는 투자원금과 별도 항목이며 실험 전체 손익에는 포함된다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from aifund.ai.provider import LLMUsage
from aifund.config.settings import AISettings, ModelPricing
from aifund.core.money import D, ZERO
from aifund.core.timeutil import Clock, kst_month, parse_iso, to_iso
from aifund.data.fx import FxService
from aifund.db.database import Database

log = logging.getLogger(__name__)

RESERVE_MARGIN = Decimal("1.10")
MTOK = Decimal(1_000_000)


class BudgetExceeded(Exception):
    pass


class PricingMissing(Exception):
    pass


@dataclass(frozen=True)
class MonthUsage:
    month: str
    settled_krw: Decimal
    reserved_krw: Decimal
    cap_krw: Decimal
    estimated: bool

    @property
    def remaining_krw(self) -> Decimal:
        return self.cap_krw - self.settled_krw - self.reserved_krw


class AIBudget:
    def __init__(self, db: Database, settings: AISettings, fx: FxService, clock: Clock) -> None:
        self.db = db
        self.s = settings
        self.fx = fx
        self.clock = clock

    def pricing_for(self, model: str) -> ModelPricing | None:
        return self.s.pricing.get(model)

    def max_known_rates(self) -> tuple[Decimal, Decimal]:
        if not self.s.pricing:
            raise PricingMissing("요율 미설정")
        return (max(p.input_usd_per_mtok for p in self.s.pricing.values()),
                max(p.output_usd_per_mtok for p in self.s.pricing.values()))

    def estimate_usd(self, model: str, input_tokens: int, max_output_tokens: int, fallbacks: bool) -> Decimal:
        p = self.pricing_for(model)
        if p is None:
            raise PricingMissing(f"모델 {model}의 요율이 설정되지 않았습니다")
        in_rate, out_rate = p.input_usd_per_mtok, p.output_usd_per_mtok
        if fallbacks:
            # 폴백 모델이 더 비쌀 가능성까지 보수적으로 예약
            mi, mo = self.max_known_rates()
            in_rate, out_rate = max(in_rate, mi), max(out_rate, mo)
        return (Decimal(input_tokens) * in_rate + Decimal(max_output_tokens) * out_rate) / MTOK

    def cost_usd(self, usages: list[LLMUsage]) -> tuple[Decimal, bool]:
        """(USD 비용, 추정여부). 알 수 없는 모델 요율은 최대 요율로 계산하고 추정 처리."""
        total = ZERO
        estimated = False
        for u in usages:
            p = self.pricing_for(u.model)
            if p is None:
                estimated = True
                try:
                    in_rate, out_rate = self.max_known_rates()
                except PricingMissing:
                    in_rate, out_rate = Decimal(15), Decimal(75)
            else:
                in_rate, out_rate = p.input_usd_per_mtok, p.output_usd_per_mtok
            # 캐시 읽기·쓰기는 보수적으로 기본 입력 요율(쓰기는 1.25배)로 계산
            inp = Decimal(u.input_tokens) + Decimal(u.cache_read_tokens) + Decimal(u.cache_write_tokens) * Decimal("1.25")
            total += (inp * in_rate + Decimal(u.output_tokens) * out_rate) / MTOK
        return total, estimated

    def usdkrw(self) -> tuple[Decimal, str, bool]:
        return self.fx.usdkrw_for_costs(self.s.fallback_usdkrw)

    def month_usage(self, month: str | None = None) -> MonthUsage:
        month = month or kst_month(self.clock.now())
        rows = self.db.query("SELECT reserved_krw, actual_krw, status, estimated FROM ai_budget WHERE month=?", (month,))
        settled = sum((D(r["actual_krw"]) for r in rows if r["status"] == "settled"), ZERO)
        reserved = sum((D(r["reserved_krw"]) for r in rows if r["status"] == "reserved"), ZERO)
        est = any(r["estimated"] for r in rows if r["status"] == "settled")
        return MonthUsage(month, settled, reserved, self.s.monthly_budget_krw, est)

    def reserve(self, est_usd: Decimal, run_id: str) -> int:
        rate, src, est = self.usdkrw()
        krw = (est_usd * rate * RESERVE_MARGIN).quantize(Decimal("0.01"))
        month = kst_month(self.clock.now())
        with self.db.tx() as c:
            rows = c.execute("SELECT reserved_krw, actual_krw, status FROM ai_budget WHERE month=?", (month,)).fetchall()
            used = sum((D(r["actual_krw"]) for r in rows if r["status"] == "settled"), ZERO)
            used += sum((D(r["reserved_krw"]) for r in rows if r["status"] == "reserved"), ZERO)
            if used + krw > self.s.monthly_budget_krw:
                raise BudgetExceeded(
                    f"월 AI 예산 초과 예상: 사용·예약 {used:.0f}원 + 이번 최대 {krw:.0f}원 > 한도 {self.s.monthly_budget_krw}원"
                )
            cur = c.execute(
                "INSERT INTO ai_budget(month, run_id, reserved_krw, status, estimated, fx_rate, fx_source, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (month, run_id, str(krw), "reserved", int(est), str(rate), src, to_iso(self.clock.now())),
            )
            return int(cur.lastrowid)

    def settle(self, budget_id: int, usd: Decimal, estimated: bool) -> Decimal:
        rate, src, est_fx = self.usdkrw()
        krw = (usd * rate).quantize(Decimal("0.01"))
        self.db.execute(
            "UPDATE ai_budget SET actual_krw=?, status='settled', estimated=?, fx_rate=?, fx_source=?, settled_at=? WHERE id=?",
            (str(krw), int(estimated or est_fx), str(rate), src, to_iso(self.clock.now()), budget_id),
        )
        return krw

    def settle_at_reservation(self, budget_id: int, why: str) -> Decimal:
        """결과를 알 수 없는 호출(타임아웃·crash)은 예약액 전액을 사용한 것으로 보수 정산."""
        row = self.db.query_one("SELECT reserved_krw FROM ai_budget WHERE id=?", (budget_id,))
        krw = D(row["reserved_krw"]) if row else ZERO
        self.db.execute(
            "UPDATE ai_budget SET actual_krw=?, status='settled', estimated=1, settled_at=? WHERE id=? AND status='reserved'",
            (str(krw), to_iso(self.clock.now()), budget_id),
        )
        log.info("AI 예산 보수 정산(%s): %s원", why, krw)
        return krw

    def release(self, budget_id: int) -> None:
        self.db.execute(
            "UPDATE ai_budget SET status='released', actual_krw='0', settled_at=? WHERE id=? AND status='reserved'",
            (to_iso(self.clock.now()), budget_id),
        )

    def recover_stale(self, older_than_min: int = 30) -> int:
        cutoff = self.clock.now() - timedelta(minutes=older_than_min)
        rows = self.db.query("SELECT id, created_at FROM ai_budget WHERE status='reserved'")
        n = 0
        for r in rows:
            ts = parse_iso(r["created_at"])
            if ts is not None and ts < cutoff:
                self.settle_at_reservation(int(r["id"]), "재시작 시 미정산 예약")
                n += 1
        return n

    def total_cost_krw(self) -> Decimal:
        rows = self.db.query("SELECT actual_krw FROM ai_budget WHERE status='settled'")
        return sum((D(r["actual_krw"]) for r in rows), ZERO)
