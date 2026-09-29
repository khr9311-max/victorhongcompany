"""AI 출력 스키마와 결정적 검증.

- 모델에는 JSON 스키마(구조화 출력)를 강제하고, 응답은 여기서 다시 엄격히 검증한다.
- 근거 없는 숫자 금지: 숫자가 들어간 주장·근거는 번들에 존재하는 source_id를 1개 이상 인용해야 한다.
- AI는 가격·수량을 정하지 않는다. 목표 비중(0~1)만 제안하고 주문 가격·수량은 코드가 최신 시세로 계산한다.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_DIGIT = re.compile(r"\d")


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Claim(_M):
    text: str = Field(description="한 문장 주장(한국어, 400자 이내)", max_length=600)
    source_ids: list[str] = Field(description="근거로 인용한 source_id 목록(번들에 있는 것만)")


class InstrumentView(_M):
    instrument_id: str
    stance: Literal["favorable", "neutral", "unfavorable", "insufficient_data"]
    summary: str = Field(max_length=800)
    claims: list[Claim]
    data_gaps: list[str]


class StrategyCondition(_M):
    strategy_id: Literal["trend_sma", "mean_reversion"]
    regime_fit: Literal["fits", "does_not_fit", "unclear"]
    reason: str = Field(max_length=600)
    source_ids: list[str]


class AIProposal(_M):
    proposal_ref: str = Field(description="보고서 내 고유 참조(예: P1)")
    instrument_id: str
    action: Literal["buy", "hold", "reduce", "sell"]
    target_weight: float = Field(ge=0, le=1, description="AI 슬리브 대비 목표 비중 0~1")
    rationale: str = Field(max_length=800)
    source_ids: list[str]
    counterarguments: list[str]
    invalidation: str = Field(max_length=400, description="이 제안이 무효가 되는 조건")
    horizon_hours: int = Field(ge=1, le=72)
    cost_considered: str = Field(max_length=300, description="수수료·스프레드 고려 내용")


class ParamChange(_M):
    name: str
    value: float


class ImprovementIdea(_M):
    strategy_id: Literal["trend_sma", "mean_reversion"]
    changes: list[ParamChange]
    rationale: str = Field(max_length=800)
    source_ids: list[str]


class ResearchReport(_M):
    market_summary: str = Field(max_length=1500)
    data_status: str = Field(max_length=600, description="자료 부족·지연 상태를 명시")
    instrument_views: list[InstrumentView]
    strategy_conditions: list[StrategyCondition]
    proposals: list[AIProposal]
    improvement_ideas: list[ImprovementIdea]


class IndependentAssessment(_M):
    summary: str = Field(max_length=1500)
    instrument_views: list[InstrumentView]
    key_risks: list[Claim]


class Objection(_M):
    category: Literal["evidence_error", "conflicting_info", "lookahead", "cost_omitted", "concentration", "other"]
    detail: str = Field(max_length=600)
    source_ids: list[str]


class ProposalReview(_M):
    proposal_ref: str
    verdict: Literal["accept", "reject"]
    objections: list[Objection]


class StanceCheck(_M):
    instrument_id: str
    agreement: Literal["agree", "disagree", "insufficient"]
    reason: str = Field(max_length=600)
    source_ids: list[str]


class ReviewReport(_M):
    summary: str = Field(max_length=1500)
    stance_checks: list[StanceCheck]
    proposal_reviews: list[ProposalReview]
    general_objections: list[Objection]


class StrategyReviewReport(_M):
    summary: str = Field(max_length=1500)
    data_sufficiency: str = Field(max_length=600)
    improvement_ideas: list[ImprovementIdea]


# ---------------- 결정적 검증 ----------------


def _check_sources(ids: list[str], known: set[str], where: str, errors: list[str]) -> None:
    for sid in ids:
        if sid not in known:
            errors.append(f"{where}: 번들에 없는 source_id 인용 '{sid}'")


def _check_claim_text(text: str, ids: list[str], where: str, errors: list[str]) -> None:
    if _DIGIT.search(text) and not ids:
        errors.append(f"{where}: 근거 없는 숫자 포함")


def validate_research(r: ResearchReport, known_sources: set[str], allowed_instruments: set[str]) -> list[str]:
    errors: list[str] = []
    for v in r.instrument_views:
        if v.instrument_id not in allowed_instruments:
            errors.append(f"미지원 종목: {v.instrument_id}")
        for i, c in enumerate(v.claims):
            _check_sources(c.source_ids, known_sources, f"{v.instrument_id} 주장{i + 1}", errors)
            _check_claim_text(c.text, c.source_ids, f"{v.instrument_id} 주장{i + 1}", errors)
        _check_claim_text(v.summary, [s for c in v.claims for s in c.source_ids], f"{v.instrument_id} 요약", errors)
    for sc in r.strategy_conditions:
        _check_sources(sc.source_ids, known_sources, f"전략조건 {sc.strategy_id}", errors)
    refs: set[str] = set()
    total_weight = 0.0
    for p in r.proposals:
        if p.proposal_ref in refs:
            errors.append(f"중복 proposal_ref: {p.proposal_ref}")
        refs.add(p.proposal_ref)
        if p.instrument_id not in allowed_instruments:
            errors.append(f"미지원 종목 제안: {p.instrument_id}")
        if not p.source_ids:
            errors.append(f"{p.proposal_ref}: 근거(source_ids) 없음")
        _check_sources(p.source_ids, known_sources, p.proposal_ref, errors)
        _check_claim_text(p.rationale, p.source_ids, f"{p.proposal_ref} 근거", errors)
        if not p.invalidation.strip():
            errors.append(f"{p.proposal_ref}: 무효화 조건 없음")
        if p.action in ("buy", "hold"):
            total_weight += p.target_weight
    if total_weight > 1.0 + 1e-9:
        errors.append(f"AI 슬리브 목표 비중 합계 {total_weight:.2f} > 1")
    for idea in r.improvement_ideas:
        _check_sources(idea.source_ids, known_sources, f"개선안 {idea.strategy_id}", errors)
    return errors


def validate_independent(a: IndependentAssessment, known_sources: set[str], allowed_instruments: set[str]) -> list[str]:
    errors: list[str] = []
    for v in a.instrument_views:
        if v.instrument_id not in allowed_instruments:
            errors.append(f"미지원 종목: {v.instrument_id}")
        for i, c in enumerate(v.claims):
            _check_sources(c.source_ids, known_sources, f"{v.instrument_id} 주장{i + 1}", errors)
            _check_claim_text(c.text, c.source_ids, f"{v.instrument_id} 주장{i + 1}", errors)
    for i, c in enumerate(a.key_risks):
        _check_sources(c.source_ids, known_sources, f"위험{i + 1}", errors)
        _check_claim_text(c.text, c.source_ids, f"위험{i + 1}", errors)
    return errors


def validate_review(rv: ReviewReport, known_sources: set[str], allowed_instruments: set[str], proposal_refs: set[str]) -> list[str]:
    errors: list[str] = []
    for s in rv.stance_checks:
        if s.instrument_id not in allowed_instruments:
            errors.append(f"미지원 종목: {s.instrument_id}")
        _check_sources(s.source_ids, known_sources, f"입장검토 {s.instrument_id}", errors)
        _check_claim_text(s.reason, s.source_ids, f"입장검토 {s.instrument_id}", errors)
    seen: set[str] = set()
    for pr in rv.proposal_reviews:
        if pr.proposal_ref not in proposal_refs:
            errors.append(f"존재하지 않는 제안 검토: {pr.proposal_ref}")
        seen.add(pr.proposal_ref)
        for o in pr.objections:
            _check_sources(o.source_ids, known_sources, f"{pr.proposal_ref} 반론", errors)
            _check_claim_text(o.detail, o.source_ids, f"{pr.proposal_ref} 반론", errors)
    missing = proposal_refs - seen
    if missing:
        errors.append(f"검토 누락 제안: {sorted(missing)}")
    for o in rv.general_objections:
        _check_sources(o.source_ids, known_sources, "일반 반론", errors)
    return errors


def validate_strategy_review(r: StrategyReviewReport, known_sources: set[str]) -> list[str]:
    errors: list[str] = []
    for idea in r.improvement_ideas:
        _check_sources(idea.source_ids, known_sources, f"개선안 {idea.strategy_id}", errors)
        if not idea.changes:
            errors.append(f"개선안 {idea.strategy_id}: 변경 파라미터 없음")
    return errors
