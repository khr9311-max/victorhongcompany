"""대시보드 표시용 한국어 라벨. 코드 값은 그대로 두고 화면에서만 바꿔 보여 준다."""

from __future__ import annotations

from aifund.ai.team import ROLE_TITLES
from aifund.domain.models import ACTION_LABELS, STATUS_LABELS
from aifund.lab.catalog import all_variants

BOOK_LABELS = {
    "operating": "운용 장부",
    "shadow_A": "비교 A(규칙 전략만)",
    "shadow_B": "비교 B(+AI 연구팀)",
    "shadow_C": "비교 C(+AI 연구팀·검증팀)",
    "baseline_bh": "기준: 매수·보유",
    "baseline_cash": "기준: 현금 유지",
}
STRATEGY_LABELS = {"trend_sma": "봇 A 추세", "mean_reversion": "봇 B 평균회귀", "ai_research": "AI 슬리브", "_book": "장부 공통"}
STRATEGY_LABELS.update({f"lab:{v.id}": f"연구소 {v.label}" for v in all_variants()})  # 전략 연구소 변형(설정 strategies.lab)
MARKET_LABELS = {"crypto": "코인", "kr_stock": "국내주식", "us_stock": "미국주식", "all": "전체"}
# 시장 색 슬롯(자산 구성·시장 표시가 같은 색을 쓰도록 고정)
MARKET_SLOTS = {"crypto": 1, "kr_stock": 2, "us_stock": 3}
ROLE_LABELS = ROLE_TITLES
# AI 보고서의 분류 값(뉴스 사건·국면·위험 판정·반론 분류 등) → 화면 문구
AI_LABELS = {
    "earnings": "실적", "guidance_outlook": "전망·가이던스", "product_demand": "제품·수요", "regulation_legal": "규제·법률",
    "macro_policy": "거시·정책", "flows_supply": "수급·자금 흐름", "corporate_action": "기업 행위", "security_incident": "보안 사고",
    "other": "기타", "positive": "긍정", "negative": "부정", "mixed": "혼재", "unclear": "불명확",
    "high": "높음", "medium": "중간", "low": "낮음", "normal": "보통", "unknown": "알 수 없음",
    "official": "공식 발표·공시", "reported": "언론 보도", "opinion_or_rumor": "의견·루머",
    "uptrend": "상승 추세", "downtrend": "하락 추세", "range": "횡보", "volatile": "변동성 장세", "insufficient_data": "자료 부족",
    "ok": "양호", "caution": "주의", "fits": "적합", "does_not_fit": "부적합",
    "approve": "승인", "cap": "비중 축소", "reject": "거절", "accept": "채택 가능",
    "agree": "동의", "disagree": "반대", "insufficient": "판단 불가",
    "cross_market_concentration": "시장 간 집중", "theme_concentration": "테마 집중", "position_count": "보유 종목 수",
    "liquidity": "유동성", "volatility": "변동성", "event_risk": "이벤트 위험", "cost": "비용", "data_quality": "자료 품질",
    "evidence_error": "근거 오류", "conflicting_info": "상충 정보", "lookahead": "미래정보", "cost_omitted": "비용 누락",
    "concentration": "집중", "trend_sma": "봇 A 추세", "mean_reversion": "봇 B 평균회귀",
}
TRIGGER_LABELS = {"scheduled": "정기", "daily": "정기(일일)", "manual": "수동", "follow_up": "후속 검증", "weekly": "주간",
                  "test": "테스트"}
RUN_STATUS_LABELS = {"ok": "성공", "invalid": "검증 실패", "error": "오류", "skipped": "건너뜀(AI 사용 불가)",
                     "skipped_budget": "건너뜀(예산)", "timeout": "시간 초과", "refusal": "모델 거절", "running": "실행 중"}
SEVERITY_LABELS = {"info": "정보", "warning": "주의", "critical": "심각"}
PURPOSE_LABELS = {"rebalance": "전략 조정", "baseline": "기준선 매수", "liquidation": "보유분 청산"}
STANCE_LABELS = {"favorable": "우호", "neutral": "중립", "unfavorable": "비우호", "insufficient_data": "자료 부족"}
INCIDENT_LABELS = {
    "ai_error": "AI 호출 오류", "ai_invalid": "AI 결과 검증 실패", "crash_recovery": "비정상 종료 복구",
    "cycle_error": "사이클 처리 실패", "data_error": "데이터 수집 실패", "fill_gap": "체결 누락 의심",
    "fill_overflow": "체결 수량 초과", "liquidation_incomplete": "청산 미완료", "order_missing": "주문 조회 불가",
    "order_unknown": "주문 상태 불명", "reconcile_mismatch": "대사 불일치",
}
SETTING_LABELS = {"A": "A · 규칙 전략만", "B": "B · +AI 연구팀", "C": "C · +AI 연구팀·검증팀", "BUY_HOLD": "매수·보유", "CASH": "현금"}
SERVICE_LABELS = {"running": "실행 중", "starting": "시작 중", "stopping": "종료 중", "stopped": "중지됨",
                  "대시보드 단독": "대시보드만 실행"}
CYCLE_LABELS = {"done": "완료", "running": "진행 중", "skipped_stale": "건너뜀(지난 봉)", "skipped_no_data": "건너뜀(데이터 없음)",
                "data_error": "데이터 오류"}

LABELS = {"status": STATUS_LABELS, "action": ACTION_LABELS, "book": BOOK_LABELS, "strategy": STRATEGY_LABELS,
          "market": MARKET_LABELS, "role": ROLE_LABELS, "run": RUN_STATUS_LABELS, "severity": SEVERITY_LABELS,
          "purpose": PURPOSE_LABELS, "stance": STANCE_LABELS, "incident": INCIDENT_LABELS, "setting": SETTING_LABELS,
          "service": SERVICE_LABELS, "cycle": CYCLE_LABELS, "ai": AI_LABELS}


def trigger_label(trigger: str | None) -> str:
    if not trigger:
        return "-"
    if trigger.startswith("event"):
        return "급변" + (trigger[5:].replace(": 급변 이벤트:", ":") if len(trigger) > 5 else "")
    return TRIGGER_LABELS.get(trigger, trigger)


def flag_label(key: str) -> str:
    """제어 플래그 키 → 사람이 읽는 이름."""
    if key == "halt:global":
        return "신규 매수 중지(전체)"
    kind, _, rest = key.partition(":")
    if kind == "halt":
        market = rest.removeprefix("market:")
        return f"신규 매수 중지({MARKET_LABELS.get(market, market)})"
    names = {"recon_block": "대사 불일치로 주문 차단", "auth_error": "계좌 인증 오류", "liquidating": "청산 진행 중"}
    target = MARKET_LABELS.get(rest, rest)
    return f"{names[kind]}({target})" if kind in names else key
