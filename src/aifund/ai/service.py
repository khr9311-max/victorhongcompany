"""AI 직원(연구팀·검증팀·전략 연구원) 운영. 직원 명부는 ai/team.py.

한 번의 연구(run_desk)는 고정된 순서로 한 번씩만 호출한다(장시간 토론 없음):
  뉴스·공시 애널리스트, 퀀트 애널리스트 → 수석 연구원 → 검증 AI(독립 평가 → 반론) → 리스크 매니저.
애널리스트 메모는 결정적 검증을 통과한 것만 수석 연구원에게 전달하며, 각 단계는 앞 단계 보고서가 검증을 통과했을 때만 진행한다.
호출 빈도(기본): 시장별 정기 연구 시각마다(코인 N시간마다, 주식 개장 전 1회), 급변 이벤트 시 제한된 추가 검토, 전략 검토 주 1회.
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
from datetime import datetime, timedelta
from decimal import Decimal
from importlib import resources
from typing import Any

from pydantic import BaseModel, ValidationError

from aifund.ai import schemas as S
from aifund.ai.budget import AIBudget, BudgetExceeded, PricingMissing
from aifund.ai.evidence import TIME_RULES, Bundle, build_bundle
from aifund.ai.provider import LLMError, LLMProvider
from aifund.ai.team import role_title
from aifund.config.settings import Settings
from aifund.control.flags import Incidents
from aifund.core.ids import new_id
from aifund.core.money import D
from aifund.core.timeutil import KST, Clock, parse_iso, to_iso
from aifund.data.collector import Snapshot
from aifund.markets.calendar import session_info
from aifund.data.news import NewsCollector
from aifund.db.database import Database, dumps, loads
from aifund.domain.models import Action
from aifund.portfolio.allocator import TargetInput

log = logging.getLogger(__name__)

PROMPT_VERSIONS = {
    "news_analyst": "news_analyst_v1",
    "quant_analyst": "quant_analyst_v1",
    "research": "research_v2",
    "review_independent": "review_independent_v2",
    "review": "review_v2",
    "risk_manager": "risk_manager_v1",
    "strategy_review": "strategy_review_v1",
}
MEMO_ROLES = ("news_analyst", "quant_analyst")
RISK_ACTIONS = ("buy", "hold")  # 리스크 매니저가 판정하는 제안(위험 유지·증가). 매도·축소는 막지 않는다.
MEMO_RESERVE_CHARS = 16000  # 수석 연구원 입력에서 애널리스트 메모 자리
MAX_MEMO_EVENTS = 20
_MATERIALITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def load_prompt(name: str) -> str:
    return resources.files("aifund.ai.prompts").joinpath(f"{name}.md").read_text(encoding="utf-8")


def _bundle_ids(bundle: dict[str, Any]) -> set[str]:
    return {x["id"] for k in ("price_facts", "strategy_signals", "sources_UNTRUSTED_DATA") for x in bundle.get(k, [])}


def _compact_memo(role: str, memo: dict[str, Any]) -> dict[str, Any]:
    """수석 연구원·검증팀에 넘기는 메모. 뉴스 사건은 중요도순 상위 MAX_MEMO_EVENTS개만."""
    if role != "news_analyst":
        return memo
    events = sorted(memo.get("events", []), key=lambda e: _MATERIALITY_ORDER.get(e.get("materiality"), 3))
    return {**memo, "events": events[:MAX_MEMO_EVENTS]}


def _filter_memos(memos: dict[str, Any], known: set[str]) -> dict[str, Any]:
    """입력 길이 제한으로 번들에서 빠진 뉴스를 인용한 사건은 메모에서도 뺀다(번들 밖 source_id 인용 방지)."""
    out = dict(memos)
    if "news_analyst" in out:
        out["news_analyst"] = {**out["news_analyst"],
                               "events": [e for e in out["news_analyst"]["events"] if set(e["source_ids"]) <= known]}
    return out


# 공급자별로 확실히 다른 회사의 모델 이름(설정 화면에서 공급자만 바꾸고 모델을 그대로 둔 경우 등)
_FOREIGN_MODEL_PREFIXES = {"anthropic": ("gemini-", "gemma-"), "gemini": ("claude-",)}


def provider_model_mismatch(provider: str, model: str) -> str | None:
    if model.lower().startswith(_FOREIGN_MODEL_PREFIXES.get(provider, ())):
        return f"공급자·모델 불일치({provider} / {model}) → 설정에서 모델을 공급자에 맞게 바꾸세요"
    return None


@dataclass
class AIView:
    available: bool
    reason: str
    research_report_id: str | None = None
    review_report_id: str | None = None
    risk_report_id: str | None = None
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
                 unavailable_reason_fn: Callable[[], str | None] = lambda: None,
                 portfolio_fn: Callable[[str], dict[str, Any]] | None = None) -> None:
        self.db = db
        self.clock = clock
        self.settings_fn = settings_fn
        self.budget_fn = budget_fn
        self.provider_fn = provider_fn
        self.news = news
        self.incidents = incidents
        self.mode = mode
        self.unavailable_reason_fn = unavailable_reason_fn
        self.portfolio_fn = portfolio_fn  # 리스크 매니저 입력(회사 전체 보유·한도, pf:* 항목)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 가용성
    def availability(self) -> tuple[bool, str]:
        s = self.settings_fn().ai
        if not s.enabled or s.provider == "disabled":
            return False, "AI 비활성(설정)"
        extra = self.unavailable_reason_fn()
        if extra:
            return False, extra
        mismatch = provider_model_mismatch(s.provider, s.model)
        if mismatch:
            return False, mismatch
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
                    model_cls: type[BaseModel], trigger: str, effort: str | None = None) -> CallOutcome:
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
        schema = transform_schema(model_cls) if provider.name == "anthropic" else model_cls.model_json_schema()
        budget = self.budget_fn()
        budget_id = None
        if provider.is_paid:
            tokens = await provider.count_tokens(system, user, schema)
            if tokens is None:
                tokens = len((system + user + json.dumps(schema, ensure_ascii=False)).encode("utf-8")) + 2000
            try:
                est = budget.estimate_usd(provider.model, tokens, s.max_output_tokens, s.use_server_fallbacks and provider.name == "anthropic")
                budget_id = budget.reserve(est, run_id)
            except (BudgetExceeded, PricingMissing) as exc:
                record("skipped_budget", error=str(exc))
                return CallOutcome(run_id, "skipped_budget", None, [str(exc)], Decimal(0))
        record("running", budget_id=budget_id)
        try:
            result = await asyncio.wait_for(provider.complete_json(system, user, schema, s.max_output_tokens, effort=effort),
                                            s.timeout_sec + 30)
        except asyncio.TimeoutError:
            cost = budget.settle_at_reservation(budget_id, "타임아웃") if budget_id else Decimal(0)
            record("timeout", error="응답 시간 초과", cost_krw=str(cost), cost_estimated=1)
            self.incidents.open("ai_error", f"{role_title(role)} 타임아웃", market=market)
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
            self.incidents.open("ai_error", f"{role_title(role)} 호출 오류: {exc.kind}", market=market)
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
        fee = {"crypto": s.execution.paper.fee_rate, "kr_stock": s.execution.paper.stock_fee_rate,
               "us_stock": s.execution.paper.us_fee_rate}[snapshot.market]
        return {"fee_rate_each_side": str(fee), "note": "스프레드는 price_facts.spread_pct 참고. 최소 주문 금액 존재.",
                "ai_sleeve_weight": str(s.strategies.sleeves_for(snapshot.market).get("ai_research", 0))}

    async def research(self, market: str, snapshot: Snapshot, signals: list[dict[str, Any]], trigger: str) -> str | None:
        """연구팀: 애널리스트 메모(켜진 직원만) → 수석 연구원 보고서. 애널리스트 보고서는 수석 연구원 보고서의 하위로 묶는다."""
        async with self._lock:
            s = self.settings_fn().ai
            news = self.news.recent(market, limit=s.max_news_items)
            status = [f"트리거: {trigger}"]
            if s.research_focus.strip():
                status.append(f"대표의 연구 관심사: {s.research_focus.strip()}")
            bundle = build_bundle(snapshot, signals, news, self.costs_payload(snapshot), self.clock.now(), status)
            task = {"phase": "research", "task": "시장 요약·전략 유효조건·신중한 제안"}
            memo_ids: list[str] = []
            memos: dict[str, Any] = {}
            # AI를 못 쓰는 상태(키 없음·예산 소진 등)면 애널리스트를 부르지 않는다(건너뜀 기록은 수석 연구원 1건만)
            if (s.news_analyst_enabled or s.quant_analyst_enabled) and self.availability()[0]:
                # 애널리스트와 수석 연구원이 같은 뉴스를 보도록 메모 자리를 남기고 번들을 먼저 줄인다
                bundle.to_user_text(max(2000, s.max_input_chars - MEMO_RESERVE_CHARS), task)
                memo_ids, memos = await self._analysts(market, snapshot.snapshot_id, bundle, trigger)
            user = self._research_text(bundle, s.max_input_chars, task, memos)
            out = await self._call(role="research", market=market, snapshot_id=snapshot.snapshot_id,
                                   system=load_prompt(PROMPT_VERSIONS["research"]), user=user, model_cls=S.ResearchReport,
                                   trigger=trigger)
            if out.parsed is None:
                return None
            report: S.ResearchReport = out.parsed  # type: ignore[assignment]
            errors = S.validate_research(report, bundle.ids(), set(bundle.allowed_instruments))
            rid = self._store_report(out.run_id, "research", market, snapshot.snapshot_id, None, report, bundle, errors,
                                     s.report_ttl_hours)
            if memo_ids:
                marks = ",".join("?" for _ in memo_ids)
                self.db.execute(f"UPDATE ai_reports SET parent_report_id=? WHERE report_id IN ({marks})", (rid, *memo_ids))
            return rid

    async def _analysts(self, market: str, snapshot_id: str, bundle: Bundle, trigger: str) -> tuple[list[str], dict[str, Any]]:
        """연구팀 애널리스트 호출. (저장한 보고서 ID, 검증을 통과한 메모 {역할: 메모}). 실패·검증 실패는 번들 상태에 적는다."""
        s = self.settings_fn().ai
        allowed = set(bundle.allowed_instruments)
        jobs: list[tuple[str, dict[str, Any], type[BaseModel], Callable[[Any], list[str]]]] = []
        if s.news_analyst_enabled:
            if bundle.sources:
                news_ids = {x["id"] for x in bundle.sources}
                jobs.append(("news_analyst", bundle.news_payload({"phase": "news_analyst", "task": "뉴스·공시 사건 정리(제안 금지)"}),
                             S.NewsDigest, lambda r: S.validate_news_digest(r, news_ids, allowed)))
            else:
                bundle.data_status.append("뉴스·공시 애널리스트: 검토할 뉴스·공시 없음")
        if s.quant_analyst_enabled:
            px_ids = {x["id"] for x in bundle.price_facts} | {x["id"] for x in bundle.strategy_signals}
            jobs.append(("quant_analyst", bundle.quant_payload({"phase": "quant_analyst", "task": "국면·전략 적합도 판단(제안 금지)"}),
                         S.QuantMemo, lambda r: S.validate_quant_memo(r, px_ids, allowed)))
        stored: list[str] = []
        memos: dict[str, Any] = {}
        for role, payload, model_cls, validate in jobs:
            out = await self._call(role=role, market=market, snapshot_id=snapshot_id, system=load_prompt(PROMPT_VERSIONS[role]),
                                   user=json.dumps(payload, ensure_ascii=False, default=str), model_cls=model_cls, trigger=trigger,
                                   effort=s.analyst_effort)
            if out.parsed is None:
                bundle.data_status.append(f"{role_title(role)} 메모 없음({out.status})")
                continue
            errors = validate(out.parsed)
            stored.append(self._store_report(out.run_id, role, market, snapshot_id, None, out.parsed, None, errors,
                                             s.report_ttl_hours))
            if errors:
                bundle.data_status.append(f"{role_title(role)} 메모는 검증 실패로 제외")
            else:
                memos[role] = _compact_memo(role, out.parsed.model_dump(mode="json"))
        return stored, memos

    @staticmethod
    def _research_text(bundle: Bundle, max_chars: int, task: dict[str, Any], memos: dict[str, Any]) -> str:
        """메모를 넣은 수석 연구원 입력. 길이 제한으로 뉴스가 빠지면 그 뉴스를 인용한 메모 사건도 빼고 다시 만든다."""
        while True:
            known = bundle.ids()
            kept = _filter_memos(memos, known)
            text = bundle.to_user_text(max_chars, {**task, "analyst_memos": kept} if kept else task)
            if bundle.ids() == known:
                return text

    def _stored_memos(self, research_report_id: str, known: set[str]) -> dict[str, Any]:
        """연구 보고서에 묶인 애널리스트 메모 중 검증을 통과한 것."""
        rows = self.db.query("SELECT role, report_json FROM ai_reports WHERE parent_report_id=? AND valid=1 AND role IN (?,?) "
                             "ORDER BY created_at", (research_report_id, *MEMO_ROLES))
        return _filter_memos({r["role"]: _compact_memo(r["role"], loads(r["report_json"])["report"]) for r in rows}, known)

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
            self.incidents.open("ai_invalid", f"{role_title(role)} 결과 검증 실패: {'; '.join(errors)[:300]}", market=market)
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
            known = _bundle_ids(bundle)
            allowed = set(bundle.get("allowed_instruments", []))
            independent: dict[str, Any] | None = None
            if s.independent_review_pass:
                u1 = json.dumps({**bundle, "phase": "independent", "task": "연구팀 결론 없이 원자료 독립 평가"}, ensure_ascii=False)
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
            memos = self._stored_memos(research_report_id, known)  # 독립 평가 뒤 반론 단계에서만 애널리스트 메모를 본다
            u2 = json.dumps({**bundle, "phase": "review", "independent_assessment": independent, "research_report": data["report"],
                             **({"analyst_memos": memos} if memos else {})}, ensure_ascii=False)
            o2 = await self._call(role="review", market=market, snapshot_id=row["snapshot_id"],
                                  system=load_prompt(PROMPT_VERSIONS["review"]), user=u2, model_cls=S.ReviewReport,
                                  trigger="follow_up")
            if o2.parsed is None:
                return None
            refs = {p["proposal_ref"] for p in data["report"].get("proposals", [])}
            errs = S.validate_review(o2.parsed, known, allowed, refs)  # type: ignore[arg-type]
            return self._store_report(o2.run_id, "review", market, row["snapshot_id"], research_report_id, o2.parsed, None,
                                      errs, s.report_ttl_hours)

    # ------------------------------------------------------------ 리스크 매니저
    async def risk_review(self, market: str, research_report_id: str) -> str | None:
        """검증 AI가 채택한 매수·유지 제안만 회사 전체 보유와 함께 판정한다. 판정할 제안이 없으면 호출하지 않는다."""
        async with self._lock:
            s = self.settings_fn().ai
            if not s.risk_manager_enabled or self.portfolio_fn is None:
                return None
            row = self.db.query_one("SELECT * FROM ai_reports WHERE report_id=?", (research_report_id,))
            review = self.latest_report("review", market, parent=research_report_id)
            if row is None or not row["valid"] or review is None or not review["valid"]:
                return None
            data = loads(row["report_json"])
            bundle, report = data["bundle"], data["report"]
            verdicts = {p["proposal_ref"]: p for p in loads(review["report_json"])["report"].get("proposal_reviews", [])}
            targets = [p for p in report.get("proposals", [])
                       if p["action"] in RISK_ACTIONS and verdicts.get(p["proposal_ref"], {}).get("verdict") == "accept"]
            if not targets:
                return None
            portfolio = self.portfolio_fn(market)
            known = _bundle_ids(bundle) | {x["id"] for x in portfolio["items"]}
            memos = self._stored_memos(research_report_id, known)
            cited = {sid for p in targets for sid in p["source_ids"]}
            cited |= {sid for e in memos.get("news_analyst", {}).get("events", []) for sid in e["source_ids"]}
            payload = {
                "phase": "risk_review", "market": market, "snapshot_time": bundle.get("snapshot_time"),
                "created_at": bundle.get("created_at"), "time_rules": TIME_RULES,
                "proposals_under_review": [{**p, "review_objections": verdicts[p["proposal_ref"]].get("objections", [])}
                                           for p in targets],
                "portfolio": portfolio, "price_facts": bundle.get("price_facts", []), "costs": bundle.get("costs"),
                "sources_UNTRUSTED_DATA": [x for x in bundle.get("sources_UNTRUSTED_DATA", []) if x["id"] in cited],
                **({"analyst_memos": memos} if memos else {}),
            }
            out = await self._call(role="risk_manager", market=market, snapshot_id=row["snapshot_id"],
                                   system=load_prompt(PROMPT_VERSIONS["risk_manager"]),
                                   user=json.dumps(payload, ensure_ascii=False, default=str), model_cls=S.RiskReview,
                                   trigger="follow_up")
            if out.parsed is None:
                return None
            refs = {p["proposal_ref"] for p in targets}
            errs = S.validate_risk_review(out.parsed, known, refs)  # type: ignore[arg-type]
            return self._store_report(out.run_id, "risk_manager", market, row["snapshot_id"], research_report_id, out.parsed,
                                      {"portfolio": portfolio, "proposal_refs": sorted(refs)}, errs, s.report_ttl_hours)

    # ------------------------------------------------------------ 전체 흐름
    async def run_desk(self, market: str, snapshot: Snapshot, signals: list[dict[str, Any]], trigger: str) -> str | None:
        """연구팀 → 검증팀. 앞 단계 보고서가 결정적 검증을 통과했을 때만 다음 단계로 간다. 반환: 수석 연구원 보고서 ID."""
        rid = await self.research(market, snapshot, signals, trigger)
        if rid is None or not self._valid(rid):
            return rid
        rv = await self.review(market, rid)
        if rv is not None and self._valid(rv):
            await self.risk_review(market, rid)
        return rid

    def _valid(self, report_id: str) -> bool:
        row = self.db.query_one("SELECT valid FROM ai_reports WHERE report_id=?", (report_id,))
        return bool(row and row["valid"])

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

    def research_slot(self, market: str) -> tuple[datetime, timedelta] | None:
        """지금 적용되는 정기 연구 시각과 그 구간 길이. 없으면 None.

        코인: daily_research_time_kst부터 crypto_research_interval_hours마다.
        주식: 거래일 정규장 시작 stock_research_lead_min분 전 ~ 장 시작 후 판단 시각 + 2시간(그 뒤엔 다음 거래일).
        """
        s = self.settings_fn()
        now = self.clock.now()
        if market == "crypto":
            h, m = map(int, s.ai.daily_research_time_kst.split(":"))
            now_k = now.astimezone(KST)
            anchor = now_k.replace(hour=h, minute=m, second=0, microsecond=0)
            if anchor > now_k:
                anchor -= timedelta(days=1)
            step = timedelta(hours=s.ai.crypto_research_interval_hours)
            return anchor + step * ((now_k - anchor) // step), step
        sess = session_info(market, now)
        if sess.session_open is None or not (sess.is_open or now < sess.session_open):
            return None  # 휴장·장 마감 후 → 다음 거래일 개장 전까지 정기 연구 없음
        start = sess.session_open - timedelta(minutes=s.ai.stock_research_lead_min)
        end = sess.session_open + timedelta(minutes=s.markets[market].stock_decision_after_open_min, hours=2)  # type: ignore[index]
        return (start, end - start) if start <= now <= end else None

    def due_research(self, market: str) -> bool:
        """정기 연구 시각마다 1회. 실패하면 1시간 뒤 1회만 재시도, AI 불가(skipped)면 구간당 최대 6시간 간격으로만 재확인."""
        slot = self.research_slot(market)
        if slot is None:
            return False
        start, length = slot
        rows = self.db.query("SELECT status, started_at FROM ai_runs WHERE role='research' AND market=? "
                             "AND trigger IN ('scheduled','daily') AND started_at>=?", (market, to_iso(start)))
        attempts = [r for r in rows if r["status"] not in ("skipped", "skipped_budget")]
        if any(r["status"] == "ok" for r in attempts) or len(attempts) >= 2:
            return False
        last_any = max((parse_iso(r["started_at"]) for r in rows), default=None)
        if last_any is None:
            return True
        gap = timedelta(hours=1) if attempts else min(timedelta(hours=6), length)
        return self.clock.now() - last_any > gap  # type: ignore[operator]

    def schedule_text(self, market: str) -> str:
        """대시보드 표시용 정기 연구 규칙."""
        s = self.settings_fn().ai
        if market == "crypto":
            n = s.crypto_research_interval_hours
            return f"매일 {s.daily_research_time_kst}" if n == 24 else f"{s.daily_research_time_kst}부터 {n}시간마다"
        return f"거래일 개장 {s.stock_research_lead_min}분 전"

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
        # C 설정 + 리스크 매니저: 검증 AI가 채택한 매수·유지 제안도 리스크 매니저 판정(승인·축소·거절)을 거친다
        risk_gate = setting == "C" and s.risk_manager_enabled
        risk_verdicts: dict[str, dict[str, Any]] | None = None
        risk_missing = ""
        if risk_gate:
            rk = self.latest_report("risk_manager", market, parent=valid_rr["report_id"])
            if rk is not None and rk["valid"]:
                risk_verdicts = {v["proposal_ref"]: v for v in loads(rk["report_json"])["report"].get("verdicts", [])}
                view.risk_report_id = rk["report_id"]
            else:
                risk_missing = "리스크 매니저 검토 없음" if rk is None else "리스크 매니저 결과 검증 실패"
        if s.veto_rule_buys:
            for v in report.get("instrument_views", []):
                if v["stance"] != "unfavorable":
                    continue
                if setting == "C" and stance_ok.get(v["instrument_id"]) != "agree":
                    continue
                view.veto[v["instrument_id"]] = f"수석 연구원 반대 입장({v['summary'][:80]})"
        # 제안 기록에는 그 보고서를 실제로 만든 프롬프트 버전을 남긴다(버전이 바뀌기 전 보고서 포함)
        prompt_version = self.db.scalar("SELECT prompt_version FROM ai_runs WHERE run_id=?", (valid_rr["run_id"],)) \
            or PROMPT_VERSIONS["research"]
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
            weight = D(str(p["target_weight"]))
            rationale = p["rationale"]
            if blocked is None and risk_gate and p["action"] in RISK_ACTIONS:
                blocked, weight, note = _apply_risk(p["proposal_ref"], weight, risk_verdicts, risk_missing)
                if note:
                    rationale = f"{rationale} [{note}]"
            view.proposals.append(TargetInput(
                "ai_research", p["instrument_id"], Action(p["action"]), weight, rationale, source="ai",
                sources=p["source_ids"], counterarguments=p["counterarguments"], invalidation=p["invalidation"],
                ai_report_id=valid_rr["report_id"], prompt_version=prompt_version, blocked_reason=blocked,
            ))
        return view


def _apply_risk(ref: str, weight: Decimal, verdicts: dict[str, dict[str, Any]] | None,
                missing: str) -> tuple[str | None, Decimal, str]:
    """리스크 매니저 판정 반영: (보류 사유, 목표 비중, 근거 메모). 비중은 줄이기만 한다."""
    if verdicts is None:
        return f"{missing} → C 설정 매수·유지 제안 보류", weight, ""
    v = verdicts.get(ref)
    if v is None:
        return "리스크 매니저 판정 없음", weight, ""
    why = "; ".join(f"{r['category']}: {r['detail']}" for r in v.get("reasons", []))
    if v["verdict"] == "reject":
        return f"리스크 매니저 거절: {why}", weight, ""
    cap = D(str(v["max_weight"]))
    if v["verdict"] == "cap" and cap < weight:
        return None, cap, f"리스크 매니저 비중 축소 {weight}→{cap}: {why}"
    return None, weight, ""
