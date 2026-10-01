"""대시보드 표시용 한국어 라벨. 코드 값은 그대로 두고 화면에서만 바꿔 보여 준다."""

from __future__ import annotations

from aifund.domain.models import ACTION_LABELS, STATUS_LABELS

BOOK_LABELS = {
    "operating": "운용 장부",
    "shadow_A": "비교 A(규칙 전략만)",
    "shadow_B": "비교 B(+연구 AI)",
    "shadow_C": "비교 C(+연구·검증 AI)",
    "baseline_bh": "기준: 매수·보유",
    "baseline_cash": "기준: 현금 유지",
}
STRATEGY_LABELS = {"trend_sma": "봇 A 추세", "mean_reversion": "봇 B 평균회귀", "ai_research": "AI 슬리브", "_book": "장부 공통"}
MARKET_LABELS = {"crypto": "코인", "kr_stock": "국내주식", "us_stock": "미국주식", "all": "전체"}
# 시장 색 슬롯(자산 구성·시장 표시가 같은 색을 쓰도록 고정)
MARKET_SLOTS = {"crypto": 1, "kr_stock": 2, "us_stock": 3}
ROLE_LABELS = {"research": "연구 AI", "review_independent": "검증 AI 독립 평가", "review": "검증 AI 반론",
               "strategy_review": "주간 전략 검토"}
TRIGGER_LABELS = {"scheduled": "정기", "daily": "정기(일일)", "manual": "수동", "follow_up": "후속 검증", "weekly": "주간",
                  "test": "테스트"}
RUN_STATUS_LABELS = {"ok": "성공", "invalid": "검증 실패", "error": "오류", "skipped": "건너뜀(AI 사용 불가)",
                     "skipped_budget": "건너뜀(예산)", "timeout": "시간 초과", "refusal": "모델 거절", "running": "실행 중"}
SEVERITY_LABELS = {"info": "정보", "warning": "주의", "critical": "심각"}
PURPOSE_LABELS = {"rebalance": "전략 조정", "baseline": "기준선 매수", "liquidation": "보유분 청산"}
STANCE_LABELS = {"favorable": "우호", "neutral": "중립", "unfavorable": "비우호"}
INCIDENT_LABELS = {
    "ai_error": "AI 호출 오류", "ai_invalid": "AI 결과 검증 실패", "crash_recovery": "비정상 종료 복구",
    "cycle_error": "사이클 처리 실패", "data_error": "데이터 수집 실패", "fill_gap": "체결 누락 의심",
    "fill_overflow": "체결 수량 초과", "liquidation_incomplete": "청산 미완료", "order_missing": "주문 조회 불가",
    "order_unknown": "주문 상태 불명", "reconcile_mismatch": "대사 불일치",
}
SETTING_LABELS = {"A": "A · 규칙 전략만", "B": "B · +연구 AI", "C": "C · +연구·검증 AI", "BUY_HOLD": "매수·보유", "CASH": "현금"}
SERVICE_LABELS = {"running": "실행 중", "starting": "시작 중", "stopping": "종료 중", "stopped": "중지됨",
                  "대시보드 단독": "대시보드만 실행"}
CYCLE_LABELS = {"done": "완료", "running": "진행 중", "skipped_stale": "건너뜀(지난 봉)", "skipped_no_data": "건너뜀(데이터 없음)",
                "data_error": "데이터 오류"}

LABELS = {"status": STATUS_LABELS, "action": ACTION_LABELS, "book": BOOK_LABELS, "strategy": STRATEGY_LABELS,
          "market": MARKET_LABELS, "role": ROLE_LABELS, "run": RUN_STATUS_LABELS, "severity": SEVERITY_LABELS,
          "purpose": PURPOSE_LABELS, "stance": STANCE_LABELS, "incident": INCIDENT_LABELS, "setting": SETTING_LABELS,
          "service": SERVICE_LABELS, "cycle": CYCLE_LABELS}


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
