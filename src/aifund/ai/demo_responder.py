"""offline_demo/테스트용 결정적 AI 응답. 모든 문구에 [데모]를 붙인다."""

from __future__ import annotations

import json
from typing import Any


def _views(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for f in facts:
        r = f.get("ret_24bar_pct")
        stance = "insufficient_data" if r is None else ("favorable" if r > 1 else "unfavorable" if r < -3 else "neutral")
        out.append({
            "instrument_id": f["instrument_id"], "stance": stance, "summary": "[데모] 코드가 계산한 가격 사실 요약",
            "claims": [{"text": "[데모] 24봉 수익률 기준 판단", "source_ids": [f["id"]]}], "data_gaps": [],
        })
    return out


def _regime(f: dict[str, Any]) -> str:
    r = f.get("ret_24bar_pct")
    return "insufficient_data" if r is None else ("uptrend" if r > 1 else "downtrend" if r < -3 else "range")


def demo_responder(system: str, user: str) -> dict[str, Any]:
    data = json.loads(user)
    phase = data.get("phase", "research")
    facts = data.get("price_facts", [])
    if phase == "news_analyst":
        allowed = set(data.get("allowed_instruments", []))
        return {
            "summary": "[데모] 뉴스 사건 정리",
            "events": [{"headline": "[데모] 수집된 기사", "instrument_ids": [i for i in s.get("instruments", []) if i in allowed],
                        "category": "other", "direction": "unclear", "materiality": "low", "verification": "reported",
                        "source_ids": [s["id"]], "note": "[데모]"} for s in data.get("sources_UNTRUSTED_DATA", [])[:5]],
            "coverage_gaps": [],
        }
    if phase == "quant_analyst":
        return {
            "summary": "[데모] 가격 사실 기반 국면 판단",
            "instruments": [{"instrument_id": f["instrument_id"], "regime": _regime(f), "volatility": "unknown", "liquidity": "unknown",
                             "reading": "[데모] 24봉 수익률 기준", "source_ids": [f["id"]]} for f in facts],
            "strategy_conditions": [{"strategy_id": sid, "regime_fit": "unclear", "reason": "[데모]", "source_ids": []}
                                    for sid in ("trend_sma", "mean_reversion")],
            "cost_notes": "[데모] 수수료·스프레드 고려", "data_gaps": [],
        }
    if phase == "risk_review":
        book = [x["id"] for x in data["portfolio"]["items"] if x["id"] == "pf:book"]
        verdicts = []
        for p in data["proposals_under_review"]:
            if p["target_weight"] > 0.25:  # 데모 리스크 매니저는 한 종목 AI 비중을 0.25로 줄인다
                verdicts.append({"proposal_ref": p["proposal_ref"], "verdict": "cap", "max_weight": 0.25, "reasons": [
                    {"category": "theme_concentration", "detail": "[데모] 한 종목 비중 축소", "source_ids": book}]})
            else:
                verdicts.append({"proposal_ref": p["proposal_ref"], "verdict": "approve", "max_weight": p["target_weight"],
                                 "reasons": []})
        return {"summary": "[데모] 리스크 매니저 판정", "verdicts": verdicts, "portfolio_concerns": []}
    if phase == "independent":
        return {"summary": "[데모] 독립 평가", "instrument_views": _views(facts), "key_risks": []}
    if phase == "review":
        rr = data["research_report"]
        return {
            "summary": "[데모] 검증 AI 반론 요약",
            "stance_checks": [
                {"instrument_id": v["instrument_id"], "agreement": "agree", "reason": "[데모] 가격 사실과 일치", "source_ids": []}
                for v in rr.get("instrument_views", [])
            ],
            "proposal_reviews": [
                {"proposal_ref": p["proposal_ref"],
                 "verdict": "reject" if p.get("target_weight", 0) > 0.5 else "accept",
                 "objections": [] if p.get("target_weight", 0) <= 0.5 else [
                     {"category": "concentration", "detail": "[데모] 한 종목 비중 과다", "source_ids": []}]}
                for p in rr.get("proposals", [])
            ],
            "general_objections": [],
        }
    if phase == "strategy_review":
        return {"summary": "[데모] 주간 전략 검토", "data_sufficiency": "[데모] 데이터 부족", "improvement_ideas": []}
    best = None
    for f in facts:
        r = f.get("ret_24bar_pct")
        if r is not None and (best is None or r > best[1]):
            best = (f, r)
    proposals = []
    if best is not None and best[1] > 1:
        proposals.append({
            "proposal_ref": "P1", "instrument_id": best[0]["instrument_id"], "action": "buy", "target_weight": 0.3,
            "rationale": "[데모] 상대적으로 강한 흐름", "source_ids": [best[0]["id"]], "counterarguments": ["[데모] 단기 과열 가능"],
            "invalidation": "[데모] 24봉 수익률이 음전하면 무효", "horizon_hours": 24, "cost_considered": "[데모] 수수료 고려",
        })
    ids = [f["id"] for f in facts]
    return {
        "market_summary": "[데모] 가짜 데이터 기반 요약", "data_status": "[데모] 실제 자료 아님",
        "instrument_views": _views(facts),
        "strategy_conditions": [
            {"strategy_id": "trend_sma", "regime_fit": "unclear", "reason": "[데모]", "source_ids": ids[:1]},
            {"strategy_id": "mean_reversion", "regime_fit": "unclear", "reason": "[데모]", "source_ids": ids[:1]},
        ],
        "proposals": proposals, "improvement_ideas": [],
    }
