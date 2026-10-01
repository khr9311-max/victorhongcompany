"""AI 직원 명부.

직원 수와 모델 수는 같을 필요가 없다. 같은 모델을 역할마다 다른 프롬프트·입력·출력 스키마로 순차 호출하고,
실행 기록(ai_runs.role)과 보고서(ai_reports.role)를 역할별로 분리한다. 모든 직원은 제안·의견만 내며
주문·쉘·설정 변경 권한이 없다.

- 연구팀(B·C 설정에 반영): 뉴스·공시 애널리스트와 퀀트 애널리스트가 원자료를 나눠 분석하고,
  수석 연구원이 원자료와 두 메모를 종합해 최종 보고서·AI 슬리브 제안을 쓴다.
- 검증팀(C 설정에 반영): 검증 AI가 독립 평가 뒤 반론을 내고, 리스크 매니저가 검증을 통과한 매수·유지 제안을
  회사 전체 보유와 함께 보고 비중을 줄이거나 거절한다(늘릴 수는 없음).
- 전략 연구원: 주 1회 전략 파라미터 개선 후보를 낸다(사람이 실험 후 승격). A/B/C 비용에는 넣지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

RESEARCH_TEAM = "연구팀"
CONTROL_TEAM = "검증팀"
STRATEGY_TEAM = "전략"


@dataclass(frozen=True)
class Employee:
    key: str
    title: str
    team: str
    roles: tuple[str, ...]  # ai_runs.role — 검증 AI는 독립 평가·반론 두 단계
    duty: str
    inputs: str
    authority: str
    toggle: str | None = None  # AISettings의 켜기/끄기 필드(None이면 AI를 쓰는 동안 항상 근무)


ROSTER: tuple[Employee, ...] = (
    Employee(
        "news_analyst", "뉴스·공시 애널리스트", RESEARCH_TEAM, ("news_analyst",),
        "최근 뉴스·공시를 종목별 사건으로 정리하고 분류·방향·중요도·확인 수준(공식/보도/의견)을 표시",
        "뉴스·공시(출처·발표·수집 시각), 허용 종목, 대표의 연구 관심사",
        "수석 연구원에게 메모 전달(제안 권한 없음)", "news_analyst_enabled",
    ),
    Employee(
        "quant_analyst", "퀀트 애널리스트", RESEARCH_TEAM, ("quant_analyst",),
        "코드가 계산한 가격 사실·지표·봇 신호로 종목별 국면(추세·횡보·변동성)과 봇 A·B 적합도를 판단",
        "가격 사실(수익률·변동성·RSI·이동평균·스프레드·거래대금), 봇 A·B 신호, 수수료",
        "수석 연구원에게 메모 전달(제안 권한 없음)", "quant_analyst_enabled",
    ),
    Employee(
        "research", "수석 연구원", RESEARCH_TEAM, ("research",),
        "원자료와 두 애널리스트 메모를 종합해 시장 요약·종목 입장·AI 슬리브 제안을 작성(기존 '연구 AI')",
        "공유 원자료 번들 + 애널리스트 메모 + 대표의 연구 관심사",
        "B·C 설정의 AI 슬리브 목표 비중 제안, '비우호' 입장으로 규칙 전략 신규 매수 보류",
    ),
    Employee(
        "reviewer", "검증 AI", CONTROL_TEAM, ("review_independent", "review"),
        "연구 결론을 보지 않고 원자료를 독립 평가한 뒤, 제안의 근거 오류·상충·미래정보·비용 누락·집중을 반박",
        "같은 원자료(독립 평가) → 연구 보고서·애널리스트 메모(반론)",
        "C 설정에서 제안별 채택/거절",
    ),
    Employee(
        "risk_manager", "리스크 매니저", CONTROL_TEAM, ("risk_manager",),
        "검증을 통과한 매수·유지 제안을 회사 전체 보유·시장별 노출·다른 시장 AI 제안과 함께 보고 비중 축소·거절",
        "C 장부의 보유·현금·노출·위험 한도(읽기 전용), 다른 시장 최신 AI 제안, 가격 사실, 관련 뉴스",
        "C 설정에서 AI 매수·유지 제안의 비중 축소·거절만(늘리기·매도 강제 불가)", "risk_manager_enabled",
    ),
    Employee(
        "strategist", "전략 연구원", STRATEGY_TEAM, ("strategy_review",),
        "주 1회 전략별 성과·거래 수·비용을 보고 허용 범위 안의 파라미터 개선 후보를 제안",
        "전략별 지표, 현재 파라미터·허용 범위",
        "개선 후보 등록만(사람이 실험 후 승격)",
    ),
)


# 실행 기록·보고서의 역할(role) → 화면 이름
ROLE_TITLES = {
    "news_analyst": "뉴스·공시 애널리스트",
    "quant_analyst": "퀀트 애널리스트",
    "research": "수석 연구원",
    "review_independent": "검증 AI 독립 평가",
    "review": "검증 AI 반론",
    "risk_manager": "리스크 매니저",
    "strategy_review": "전략 연구원(주간 검토)",
}


def role_title(role: str) -> str:
    return ROLE_TITLES.get(role, role)


def team_roles(team: str) -> tuple[str, ...]:
    return tuple(role for e in ROSTER if e.team == team for role in e.roles)


def setting_roles(setting: str) -> tuple[str, ...]:
    """운용 설정별로 비용을 반영하는 역할. A=없음, B=연구팀, C=연구팀+검증팀(공유 비용은 나누지 않고 각 설정에 모두 반영)."""
    research, control = team_roles(RESEARCH_TEAM), team_roles(CONTROL_TEAM)
    return {"A": (), "B": research, "C": research + control}.get(setting, ())
