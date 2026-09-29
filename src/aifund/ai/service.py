"""연구 AI·검증 AI 운영.

호출 빈도(기본): 자료 요약·제안 하루 1회, 급변 이벤트 시 제한된 추가 검토, 전략 검토 주 1회.
실패(잘못된 JSON, 미지원 종목, 근거 없는 숫자, 타임아웃, 예산 초과, 거절)는 모두 기록하고 신규 AI 제안을 보류한다.
기존 포지션의 위험관리·주문 상태 확인은 AI와 무관하게 계속된다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from importlib import resources
from typing import Any

from pydantic import BaseModel, ValidationError

from aifund.ai import schemas as S
from aifund.ai.budget import AIBudget, BudgetExceeded, PricingMissing
from aifund.ai.evidence import Bundle, build_bundle
from aifund.ai.provider import LLMError, LLMProvider
from aifund.config.settings import Settings
from aifund.control.flags import Incidents
from aifund.core.ids import new_id
from aifund.core.money import D
from aifund.core.timeutil import KST, Clock, parse_iso, to_iso
from aifund.data.collector import Snapshot
from aifund.data.news import NewsCollector
from aifund.db.database import Database, dumps, loads
from aifund.domain.models import Action
from aifund.portfolio.allocator import TargetInput

log = logging.getLogger(__name__)

PROMPT_VERSIONS = {
    "research": "research_v1",
    "review_independent": "review_independent_v1",
    "review": "review_v1",
    "strategy_review": "strategy_review_v1",
}


def load_prompt(name: str) -> str:
    return resources.files("aifund.ai.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")


@dataclass
class AIView:
    available: bool
    reason: str
    research_report_id: str | None = None
    review_report_id: str | None = None
    proposals: list[TargetInput] = field(default_factory=list)
    veto: dict[str, str] = field(default_factory=dict)
    hold_new_risk: bool = False


@dataclass
class CallOutcome:
    run_id: str
    status: str
    parsed: BaseModel | None
    errors: list[str]
    cost_krw: Decimal


class AIService:
    def __init__(self, *, db: Database, clock: Clock, settings_fn: Callable[[], Settings], budget_fn: Callable[[], AIBudget],
                 provider_fn: Callable[[], LLMProvider | None], news: NewsCollector, incidents: Incidents, mode: str,
                 unavailable_reason_fn: Callable[[], str | None] = lambda: None) -> None:
        self.db = db
        self.clock = clock
        self.settings_fn = settings_fn
        self.budget_fn = budget_fn
        self.provider_fn = provider_fn
        self.news = news
        self.incidents = incidents
        self.mode = mode
        self.unavailable_reason_fn = unavailable_reason_fn
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 가용성
    def availability(self) -> tuple[bool, str]:
        s = self.settings_fn().ai
        if not s.enabled or s.provider == "disabled":
            return False, "AI 비활성(설정)"
        extra = self.unavailable_reason_fn()
        if extra:
            return False, extra
        provider = self.provider_fn()
        if provider is None:
            return False, "AI 공급자 미설정(API 키 없음 등)"
        if provider.is_paid:
            budget = self.budget_fn()
            if budget.pricing_for(provider.model) is None:
                return False, f"요율 미설정({provider.model}) → 유료 자동 호출 비활성"
            usage = budget.month_usage()
            if usage.remaining_krw <= 0:
                return False, f"월 AI 예산 소진({usage.settled_krw:,.0f}/{usage.cap_krw:,.0f}원)"
        return True, "사용 가능"

    # ------------------------------------------------------------ 공통 호출
    async def _call(self, *, role: str, market: str | None, snapshot_id: str | None, system: str, user: str,
                    model_cls: type[BaseModel], trigger: str) -> CallOutcome:
        from anthropic import transform_schema

        s = self.settings_fn().ai
        run_id = new_id("ai")
        now = self.clock.now()
        prompt_version = PROMPT_VERSIONS[role]
        input_hash = hashlib.sha256((system + user).encode()).hexdigest()[:16]
        ok, reason = self.availability()
        provider = self.provider_fn() if ok else None

        def record(status: str, **cols: Any) -> None:
            base = {"run_id": run_id, "role": role, "provider": provider.name if provider else s.provider,
                    "model": provider.model if provider else s.model, "prompt_version": prompt_version, "snapshot_id": snapshot_id,
                    "started_at": to_iso(now), "finished_at": to_iso(self.clock.now()), "status": status, "input_hash": input_hash,
                    "market": market, "trigger": trigger}
            base.update(cols)
            keys = ",".join(base)
            marks = ",".join("?" for _ in base)
            updates = ",".join(f"{k}=excluded.{k}" for k in base if k != "run_id")
            self.db.execute(f"INSERT INTO ai_runs({keys}) VALUES ({marks}) ON CONFLICT(run_id) DO UPDATE SET {updates}",
                            tuple(base.values()))

        if provider is None:
            record("skipped", error=reason)
            return CallOutcome(run_id, "skipped", None, [reason], Decimal(0))
        schema = transform_schema(model_cls)
        budget = self.budget_fn()
        budget_id = None
        if provider.is_paid:
            tokens = await provider.count_tokens(system, user, schema)
            if tokens is None:
                tokens = len(system) + len(user) + 2000  # 보수적 추정(문자당 1토큰 이상)
            try:
                est = budget.estimate_usd(provider.model, tokens, s.max_output_tokens, s.use_server_fallbacks)
                budget_id = budget.reserve(est, run_id)
            except (BudgetExceeded, PricingMissing) as exc:
                record("skipped_budget", error=str(exc))
                return CallOutcome(run_id, "skipped_budget", None, [str(exc)], Decimal(0))
        record("running", budget_id=budget_id)
        try:
            result = await asyncio.wait_for(provider.complete_json(system, user, schema, s.max_output_tokens), s.timeout_sec + 30)
        except asyncio.TimeoutError:
            cost = budget.settle_at_reservation(budget_id, "타임아웃") if budget_id else Decimal(0)
            record("timeout", error="응답 시간 초과", cost_krw=str(cost), cost_estimated=1)
            self.incidents.open("ai_error", f"{role} 타임아웃", market=market)
            return CallOutcome(run_id, "timeout", None, ["타임아웃"], cost)
        except LLMError as exc:
            if budget_id:
                if exc.kind in ("bad_request", "auth", "rate_limited"):
                    budget.release(budget_id)
                    cost = Decimal(0)
                else:
                    cost = budget.settle_at_reservation(budget_id, f"오류 {exc.kind}")
            else:
                cost = Decimal(0)
            record("error", error=str(exc)[:500], cost_krw=str(cost), cost_estimated=int(cost > 0))
            self.incidents.open("ai_error", f"{role} 호출 오류: {exc.kind}", market=market)
            return CallOutcome(run_id, "error", None, [str(exc)], cost)
        usd, est = budget.cost_usd(result.usages)
        cost = budget.settle(budget_id, usd, est) if budget_id else Decimal(0)
        tokens_in = sum(u.input_tokens for u in result.usages)
        tokens_out = sum(u.output_tokens for u in result.usages)
        common = {"input_tokens": tokens_in, "output_tokens": tokens_out, "cost_krw": str(cost), "cost_usd": str(usd),
                  "cost_estimated": int(est), "request_id": result.request_id, "stop_reason": result.stop_reason,
                  "served_models": ",".join(sorted({u.model for u in result.usages}))}
        if result.refusal:
            record("refusal", error="모델이 요청을 거절함", **common)
            return CallOutcome(run_id, "refusal", None, ["refusal"], cost)
        if result.stop_reason == "max_tokens":
            record("invalid", error="출력 길이 한도로 응답이 잘림", output_json=(result.text or "")[:20000], **common)
            return CallOutcome(run_id, "invalid", None, ["max_tokens"], cost)
        try:
            parsed = model_cls.model_validate(json.loads(result.text or ""))
        except (json.JSONDecodeError, ValidationError) as exc:
            record("invalid", error=f"스키마 검증 실패: {str(exc)[:400]}", output_json=(result.text or "")[:20000], **common)
            return CallOutcome(run_id, "invalid", None, [f"스키마 검증 실패: {str(exc)[:200]}"], cost)
        record("ok", output_json=parsed.model_dump_json(), **common)
        return CallOutcome(run_id, "ok", parsed, [], cost)

    # ------------------------------------------------------------ 연구
    def costs_payload(self, snapshot: Snapshot) -> dict[str, Any]:
        s = self.settings_fn()
        fee = s.execution.paper.fee_rate if snapshot.market == "crypto" else s.execution.paper.stock_fee_rate
        return {"fee_rate_each_side": str(fee), "note": "스프레드는 price_facts.spread_pct 참고. 최소 주문 금액 존재.",
                "ai_sleeve_weight": str(s.strategies.sleeves.get("ai_research", 0))}

    async def research(self, market: str, snapshot: Snapshot, signals: list[dict[str, Any]], trigger: str) -> str | None:
        async with self._lock:
            news = self.news.recent(market)
            bundle = build_bundle(snapshot, signals, news, self.costs_payload(snapshot), self.clock.now(),
                                  [f"트리거: {trigger}"])
            s = self.settings_fn().ai
            user = bundle.to_user_text(s.max_input_chars, {"phase": "research", "task": "시장 요약·전략 유효조건·신중한 제안"})
            out = await self._call(role="research", market=market, snapshot_id=snapshot.snapshot_id,
                                   system=load_prompt(PROMPT_VERSIONS["research"]), user=user, model_cls=S.ResearchReport,
                                   trigger=trigger)
            if out.parsed is None:
                return None
            report: S.ResearchReport = out.parsed  # type: ignore[assignment]
            errors = S.validate_research(report, bundle.ids(), set(bundle.allowed_instruments))
            return self._store_report(out.run_id, "research", market, snapshot.snapshot_id, None, report, bundle, errors,
                                      s.report_ttl_hours)

    def _store_report(self, run_id: str, role: str, market: str, snapshot_id: str | None, parent: str | None,
                      report: BaseModel, bundle: Bundle | dict | None, errors: list[str], ttl_hours: int) -> str:
        now = self.clock.now()
        rid = new_id("rep")
        payload = {"report": report.model_dump(mode="json"),
                   "bundle": bundle.payload() if isinstance(bundle, Bundle) else bundle}
        self.db.execute(
            "INSERT INTO ai_reports(report_id, run_id, role, market, snapshot_id, parent_report_id, created_at, expires_at, valid, "
            "validation_errors, report_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rid, run_id, role, market, snapshot_id, parent, to_iso(now), to_iso(now + timedelta(hours=ttl_hours)),
             int(not errors), dumps(errors), dumps(payload)),
        )
        if errors:
            self.db.execute("UPDATE ai_runs SET status='invalid', error=? WHERE run_id=?", ("; ".join(errors)[:800], run_id))
            self.incidents.open("ai_invalid", f"{role} 결과 검증 실패: {'; '.join(errors)[:300]}", market=market)
        return rid

    # ------------------------------------------------------------ 검증
    async def review(self, market: str, research_report_id: str) -> str | None:
        async with self._lock:
            row = self.db.query_one("SELECT * FROM ai_reports WHERE report_id=?", (research_report_id,))
            if row is None or not row["valid"]:
                return None
            data = loads(row["report_json"])
            bundle = data["bundle"]
            s = self.settings_fn().ai
            known = {x["id"] for k in ("price_facts", "strategy_signals", "sources_UNTRUSTED_DATA") for x in bundle.get(k, [])}
            allowed = set(bundle.get("allowed_instruments", []))
            independent: dict[str, Any] | None = None
            if s.independent_review_pass:
                u1 = json.dumps({**bundle, "phase": "independent", "task": "연구 AI 결론 없이 원자료 독립 평가"}, ensure_ascii=False)
                o1 = await self._call(role="review_independent", market=market, snapshot_id=row["snapshot_id"],
                                      system=load_prompt(PROMPT_VERSIONS["review_independent"]), user=u1,
                                      model_cls=S.IndependentAssessment, trigger="follow_up")
                if o1.parsed is None:
                    return None
                errs1 = S.validate_independent(o1.parsed, known, allowed)  # type: ignore[arg-type]
                self._store_report(o1.run_id, "review_independent", market, row["snapshot_id"], research_report_id, o1.parsed,
                                   None, errs1, s.report_ttl_hours)
                if errs1:
                    return None
                independent = o1.parsed.model_dump(mode="json")
            u2 = json.dumps({**bundle, "phase": "review", "independent_assessment": independent,
                             "research_report": data["report"]}, ensure_ascii=False)
            o2 = await self._call(role="review", market=market, snapshot_id=row["snapshot_id"],
                                  system=load_prompt(PROMPT_VERSIONS["review"]), user=u2, model_cls=S.ReviewReport,
                                  trigger="follow_up")
            if o2.parsed is None:
                return None
            refs = {p["proposal_ref"] for p in data["report"].get("proposals", [])}
            errs = S.validate_review(o2.parsed, known, allowed, refs)  # type: ignore[arg-type]
            return self._store_report(o2.run_id, "review", market, row["snapshot_id"], research_report_id, o2.parsed, None,
                                      errs, s.report_ttl_hours)

    # ------------------------------------------------------------ 주간 전략 검토
    async def strategy_review(self, metrics: list[dict[str, Any]], params: dict[str, Any], bounds: dict[str, Any]) -> str | None:
        async with self._lock:
            payload = {"phase": "strategy_review", "metrics": metrics, "current_params": params, "param_bounds": bounds,
                       "note": "metric:* id를 근거로 인용"}
            known = {m["id"] for m in metrics}
            out = await self._call(role="strategy_review", market=None, snapshot_id=None,
                                   system=load_prompt(PROMPT_VERSIONS["strategy_review"]),
                                   user=json.dumps(payload, ensure_ascii=False, default=str), model_cls=S.StrategyReviewReport,
                                   trigger="weekly")
            if out.parsed is None:
                return None
            errs = S.validate_strategy_review(out.parsed, known)  # type: ignore[arg-type]
            return self._store_report(out.run_id, "strategy_review", "all", None, None, out.parsed, {"metrics": metrics},
                                      errs, 24 * 7)

    # ------------------------------------------------------------ 일정
    def _today_start(self) -> str:
        now_k = self.clock.now().astimezone(KST)
        return to_iso(now_k.replace(hour=0, minute=0, second=0, microsecond=0))  # type: ignore[return-value]

    def due_daily(self, market: str) -> bool:
        """하루 1회. 실패하면 1시간 뒤 1회만 재시도, AI 불가(skipped)면 6시간 간격으로만 재확인."""
        s = self.settings_fn().ai
        now_k = self.clock.now().astimezone(KST)
        h, m = map(int, s.daily_research_time_kst.split(":"))
        if (now_k.hour, now_k.minute) < (h, m):
            return False
        rows = self.db.query("SELECT status, started_at FROM ai_runs WHERE role='research' AND market=? AND trigger='daily' "
                             "AND started_at>=?", (market, self._today_start()))
        attempts = [r for r in rows if r["status"] not in ("skipped", "skipped_budget")]
        if any(r["status"] == "ok" for r in attempts) or len(attempts) >= 2:
            return False
        last_any = max((parse_iso(r["started_at"]) for r in rows), default=None)
        if last_any is None:
            return True
        gap = timedelta(hours=1) if attempts else timedelta(hours=6)
        return self.clock.now() - last_any > gap  # type: ignore[operator]

    def event_calls_today(self, market: str) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM ai_runs WHERE role='research' AND market=? AND trigger LIKE 'event%' AND started_at>=? "
            "AND status NOT IN ('skipped','skipped_budget')", (market, self._today_start())) or 0)

    def event_trigger(self, snapshot: Snapshot) -> str | None:
        s = self.settings_fn().ai
        for iid, it in snapshot.items.items():
            closes = it.closes
            if len(closes) >= 2 and closes[-2] > 0:
                move = abs(closes[-1] / closes[-2] - 1) * 100
                if move >= float(s.event_move_pct):
                    return f"급변 이벤트: {iid} 직전 봉 {move:.1f}% 변동"
        return None

    def due_weekly(self) -> bool:
        s = self.settings_fn().ai
        now_k = self.clock.now().astimezone(KST)
        h, m = map(int, s.weekly_review_time_kst.split(":"))
        if now_k.weekday() != s.weekly_review_weekday or (now_k.hour, now_k.minute) < (h, m):
            return False
        week_start = (now_k - timedelta(days=now_k.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        n = self.db.scalar("SELECT COUNT(*) FROM ai_runs WHERE role='strategy_review' AND started_at>=?", (to_iso(week_start),))
        return int(n or 0) == 0

    # ------------------------------------------------------------ 의사결정용 보기
    def latest_report(self, role: str, market: str, parent: str | None = None) -> dict | None:
        if parent:
            r = self.db.query_one("SELECT * FROM ai_reports WHERE role=? AND market=? AND parent_report_id=? ORDER BY created_at DESC LIMIT 1",
                                  (role, market, parent))
        else:
            r = self.db.query_one("SELECT * FROM ai_reports WHERE role=? AND market=? ORDER BY created_at DESC LIMIT 1", (role, market))
        return None if r is None else dict(r)

    def view(self, market: str, setting: str, ai_positions: dict[str, Decimal]) -> AIView:
        s = self.settings_fn().ai
        now = self.clock.now()
        rr = self.latest_report("research", market)
        valid_rr = rr if rr and rr["valid"] and parse_iso(rr["expires_at"]) > now else None  # type: ignore[operator]
        hold = s.when_unavailable == "hold_new_risk"
        if valid_rr is None:
            reason = "유효한 연구 보고서 없음" + (f"(최근: {'검증 실패' if rr and not rr['valid'] else '만료'})" if rr else "")
            v = AIView(False, reason, hold_new_risk=hold)
            # AI 근거가 만료된 AI 슬리브 보유분은 정리한다
            last_exp = parse_iso(rr["expires_at"]) if rr else None
            if ai_positions and (last_exp is None or now - last_exp > timedelta(hours=s.report_ttl_hours)):
                for iid, q in ai_positions.items():
                    if q > 0:
                        v.proposals.append(TargetInput("ai_research", iid, Action.SELL, D(0), "AI 근거 만료로 AI 슬리브 보유분 정리",
                                                       source="ai"))
            return v
        report = loads(valid_rr["report_json"])["report"]
        created = parse_iso(valid_rr["created_at"])
        review = None
        if setting == "C":
            rv = self.latest_report("review", market, parent=valid_rr["report_id"])
            if rv is None or not rv["valid"]:
                return AIView(False, "검증 AI 보고서 없음/검증 실패 → C 설정은 AI 제안 보류", valid_rr["report_id"], hold_new_risk=hold)
            review = loads(rv["report_json"])["report"]
        view = AIView(True, "사용", valid_rr["report_id"], None if review is None else rv["report_id"])  # type: ignore[possibly-undefined]
        verdicts = {p["proposal_ref"]: p for p in (review or {}).get("proposal_reviews", [])}
        stance_ok = {c["instrument_id"]: c["agreement"] for c in (review or {}).get("stance_checks", [])}
        if s.veto_rule_buys:
            for v in report.get("instrument_views", []):
                if v["stance"] != "unfavorable":
                    continue
                if setting == "C" and stance_ok.get(v["instrument_id"]) != "agree":
                    continue
                view.veto[v["instrument_id"]] = f"연구 AI 반대 입장({v['summary'][:80]})"
        for p in report.get("proposals", []):
            exp = created + timedelta(hours=min(int(p["horizon_hours"]), s.report_ttl_hours))  # type: ignore[operator]
            if exp <= now:
                continue
            blocked = None
            if setting == "C":
                rv_p = verdicts.get(p["proposal_ref"])
                if rv_p is None or rv_p["verdict"] != "accept":
                    obs = "; ".join(f"{o['category']}: {o['detail']}" for o in (rv_p or {}).get("objections", []))
                    blocked = f"검증 AI 거절: {obs or '검토 없음'}"
            view.proposals.append(TargetInput(
                "ai_research", p["instrument_id"], Action(p["action"]), D(str(p["target_weight"])), p["rationale"], source="ai",
                sources=p["source_ids"], counterarguments=p["counterarguments"], invalidation=p["invalidation"],
                ai_report_id=valid_rr["report_id"], prompt_version=PROMPT_VERSIONS["research"], blocked_reason=blocked,
            ))
        return view
