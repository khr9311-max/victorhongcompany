"""운용 설정 모델.

설정은 DB의 settings_versions에 버전으로 기록되며(변경 이력·적용 버전 추적),
설정 파일(internal_paper·offline_demo는 config/paper.toml, 그 외는 config/config.toml)은
바뀔 때마다 다음 시작 시 변경분이 반영된다(config/store.py). AI에는 설정 변경 경로가 없다.
엔지니어링 프리셋은 검증된 최적 투자조건이 아니다.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MarketName = Literal["crypto", "kr_stock", "us_stock"]
SettingName = Literal["A", "B", "C"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RiskSettings(_Strict):
    principal_cap_krw: Decimal = Decimal("300000")
    gross_exposure_cap_krw: Decimal = Decimal("300000")
    max_order_notional_krw: Decimal = Decimal("100000")
    max_open_positions: int = 3
    daily_loss_stop_krw: Decimal = Decimal("30000")
    max_drawdown_stop_pct: Decimal = Decimal("30")
    # 아래 세 값은 고정. 추가 원금·차입·레버리지는 허용 범위가 아니다.
    leverage: Literal[1] = 1
    borrow: Literal[False] = False
    short: Literal[False] = False
    fee_buffer_pct: Decimal = Decimal("0.3")
    max_quote_age_sec: int = 45
    max_candle_delay_sec: int = 600
    max_fx_age_hours: int = 96
    fx_haircut_pct: Decimal = Decimal("3")
    max_clock_skew_sec: float = 5.0
    min_24h_turnover_krw: Decimal = Decimal("1000000000")
    max_spread_pct: Decimal = Decimal("0.5")
    max_signal_age_sec: int = 900

    @field_validator("principal_cap_krw", "gross_exposure_cap_krw", "max_order_notional_krw", "daily_loss_stop_krw")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("0보다 커야 합니다")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "RiskSettings":
        if self.gross_exposure_cap_krw > self.principal_cap_krw:
            raise ValueError("총 노출 한도는 원금 한도를 넘을 수 없습니다(레버리지 금지)")
        if self.max_order_notional_krw > self.gross_exposure_cap_krw:
            raise ValueError("1회 주문 한도는 총 노출 한도 이하여야 합니다")
        if not (0 < self.max_drawdown_stop_pct <= 100):
            raise ValueError("최대 낙폭 한도는 0~100% 사이여야 합니다")
        if self.max_open_positions < 1:
            raise ValueError("최대 보유 종목 수는 1 이상이어야 합니다")
        return self


class MarketSettings(_Strict):
    enabled: bool = False
    broker: Literal["upbit", "kis", "paper"] = "upbit"
    data_provider: Literal["default", "kiwoom"] = "default"
    account_id: str = "upbit-main"
    instruments: list[str] = Field(default_factory=list)
    candle: str = "60m"  # 코인: "60m" 등 분봉, 주식: "1d"
    allocation_krw: Decimal = Field(Decimal("0"), ge=0)
    decision_delay_sec: int = 20  # 봉 마감 후 대기(거래소 반영 지연 흡수)
    stock_decision_after_open_min: int = 10
    # 판단에 쓰는 완성봉 수. 비우면 400봉(종전 그대로). 정하면 처음 한 번 그만큼 과거 캔들을 받아 채운다
    # (연구소 52주 신고가·상대강도 전략은 일봉 1년 이상이 필요 → 600 권장)
    history_bars: int | None = Field(None, ge=100, le=3000)

    @field_validator("candle")
    @classmethod
    def _candle(cls, v: str) -> str:
        if v == "1d" or (v.endswith("m") and v[:-1].isdigit() and int(v[:-1]) in (1, 3, 5, 10, 15, 30, 60, 240)):
            return v
        raise ValueError("candle은 1d 또는 1/3/5/10/15/30/60/240m 이어야 합니다")

    @property
    def candle_minutes(self) -> int | None:
        return None if self.candle == "1d" else int(self.candle[:-1])


class TrendParams(_Strict):
    fast: int = Field(20, ge=3, le=120)
    slow: int = Field(60, ge=10, le=400)
    exit_buffer_pct: Decimal = Field(Decimal("0"), ge=0, le=5)

    @model_validator(mode="after")
    def _order(self) -> "TrendParams":
        if self.fast >= self.slow:
            raise ValueError("fast는 slow보다 작아야 합니다")
        return self


class MeanRevParams(_Strict):
    bb_period: int = Field(20, ge=5, le=200)
    bb_k: Decimal = Field(Decimal("2.0"), ge=Decimal("0.5"), le=Decimal("4"))
    rsi_period: int = Field(14, ge=2, le=100)
    rsi_entry: Decimal = Field(Decimal("30"), ge=5, le=50)
    rsi_exit: Decimal = Field(Decimal("55"), ge=30, le=95)
    max_hold_bars: int = Field(48, ge=1, le=2000)
    stop_loss_pct: Decimal = Field(Decimal("8"), ge=Decimal("0.5"), le=50)
    trend_filter_period: int = Field(100, ge=10, le=500)
    trend_filter_tolerance_pct: Decimal = Field(Decimal("10"), ge=0, le=50)


class StrategyToggle(_Strict):
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class LabToggle(_Strict):
    """전략 연구소 변형을 운용 전략으로 켠다(전략 id = 'lab:변형 id'). 자금은 슬리브 'lab:변형 id'로 배정."""

    enabled: bool = True
    markets: list[MarketName] = Field(default_factory=lambda: ["kr_stock", "us_stock"])


class StrategySettings(_Strict):
    trend_sma: StrategyToggle = Field(default_factory=StrategyToggle)
    mean_reversion: StrategyToggle = Field(default_factory=StrategyToggle)
    # 전략 연구소 변형(키 = `aifund lab catalog`의 변형 id). 기본은 없음. 과거 검증 결과는 docs/strategy-lab.md
    lab: dict[str, LabToggle] = Field(default_factory=dict)
    # 슬리브: 시장 배정액 대비 전략별 배정 비율. ai_research는 A 설정에서는 현금으로 유지된다.
    sleeves: dict[str, Decimal] = Field(
        default_factory=lambda: {"trend_sma": Decimal("0.4"), "mean_reversion": Decimal("0.4"), "ai_research": Decimal("0.2")}
    )
    # 시장별 슬리브(있으면 그 시장은 sleeves 대신 이것을 쓴다). 예: 주식에만 연구소 전략을 켤 때
    market_sleeves: dict[MarketName, dict[str, Decimal]] = Field(default_factory=dict)
    rebalance_threshold_krw: Decimal = Decimal("10000")

    @model_validator(mode="after")
    def _sleeves(self) -> "StrategySettings":
        for name, sl in [("기본", self.sleeves), *self.market_sleeves.items()]:
            if sum(sl.values(), Decimal(0)) > Decimal("1.0000001"):
                raise ValueError(f"전략 슬리브 합은 1을 넘을 수 없습니다({name})")
            if any(v < 0 for v in sl.values()):
                raise ValueError(f"슬리브는 음수일 수 없습니다({name})")
        from aifund.lab.catalog import compatible, find  # 연구소는 설정을 가져오지 않는다(순환 참조 없음)

        for vid, tg in self.lab.items():
            v = find(vid)
            if v is None:
                raise ValueError(f"알 수 없는 연구소 전략 {vid} (aifund lab catalog 참고)")
            if bad := [m for m in tg.markets if not compatible(v, m)]:
                raise ValueError(f"연구소 전략 {vid}는 {', '.join(bad)}에 쓸 수 없습니다")
        return self

    def sleeves_for(self, market: str) -> dict[str, Decimal]:
        return self.market_sleeves.get(market, self.sleeves)  # type: ignore[call-overload]


class ModelPricing(_Strict):
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    source: str
    checked_at: str
    # 공식 가격표에 예고된 요율 변경(예: 도입가 종료). changes_on(UTC 날짜)부터 new_* 요율로 계산한다.
    changes_on: str | None = None
    new_input_usd_per_mtok: Decimal | None = None
    new_output_usd_per_mtok: Decimal | None = None

    @model_validator(mode="after")
    def _scheduled_change(self) -> "ModelPricing":
        parts = (self.changes_on, self.new_input_usd_per_mtok, self.new_output_usd_per_mtok)
        if any(p is not None for p in parts):
            if not all(p is not None for p in parts):
                raise ValueError("요율 변경은 changes_on·new_input_usd_per_mtok·new_output_usd_per_mtok를 함께 지정해야 합니다")
            date.fromisoformat(self.changes_on)  # type: ignore[arg-type]  # YYYY-MM-DD 형식 검사
        return self

    def rates(self, at: datetime) -> tuple[Decimal, Decimal]:
        """(입력, 출력) USD/백만 토큰. at 시점에 적용되는 요율."""
        if self.changes_on is not None and at.astimezone(timezone.utc).date() >= date.fromisoformat(self.changes_on):
            return self.new_input_usd_per_mtok, self.new_output_usd_per_mtok  # type: ignore[return-value]
        return self.input_usd_per_mtok, self.output_usd_per_mtok


class AISettings(_Strict):
    enabled: bool = True
    research_focus: str = Field("", max_length=1000)
    provider: Literal["anthropic", "gemini", "ollama", "disabled"] = "anthropic"
    model: str = "claude-opus-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    use_server_fallbacks: bool = True
    monthly_budget_krw: Decimal = Decimal("5000")
    max_output_tokens: int = Field(4000, ge=256, le=32000)
    max_input_chars: int = Field(24000, ge=2000, le=200000)
    timeout_sec: float = 120.0
    max_retries: int = Field(1, ge=0, le=3)
    fallback_usdkrw: Decimal = Decimal("1500")
    # 정기 연구 일정: 코인은 daily_research_time_kst부터 crypto_research_interval_hours마다,
    # 주식은 거래일마다 정규장 시작 stock_research_lead_min분 전(판단 직전 자료로 연구·검증).
    daily_research_time_kst: str = "08:50"
    crypto_research_interval_hours: int = Field(24, ge=1, le=24)
    stock_research_lead_min: int = Field(30, ge=5, le=240)
    max_news_items: int = Field(40, ge=5, le=200)  # 연구 입력에 넣는 최근 48시간 뉴스·공시 최대 개수
    weekly_review_weekday: int = Field(0, ge=0, le=6)  # 0=월요일
    weekly_review_time_kst: str = "09:10"
    event_move_pct: Decimal = Decimal("5")
    max_event_calls_per_day: int = Field(1, ge=0, le=24)
    independent_review_pass: bool = True
    # AI 직원 구성(ai/team.py). 끄면 그 단계 없이 진행한다(애널리스트 없이 수석 연구원이 원자료만 보고 연구 등).
    news_analyst_enabled: bool = True
    quant_analyst_enabled: bool = True
    risk_manager_enabled: bool = True  # C 설정에서 검증 통과한 AI 매수·유지 제안을 회사 전체 보유와 함께 축소·거절
    # 애널리스트(분류·정리 업무)의 추론 강도. 나머지 직원은 effort. Gemini는 추론 토큰이 출력 한도에 포함되므로
    # 공식 문서 권고대로 출력 한도를 줄이는 대신 추론 강도를 낮춰 잘림·지연을 막는다.
    analyst_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    veto_rule_buys: bool = True
    report_ttl_hours: int = Field(24, ge=1, le=72)
    when_unavailable: Literal["continue_rules", "hold_new_risk"] = "continue_rules"
    pricing: dict[str, ModelPricing] = Field(
        default_factory=lambda: {
            # 출처: https://ai.google.dev/gemini-api/docs/pricing (2026-09-24 갱신본, 2026-09-30 확인). 출력에 추론 토큰 포함
            "gemini-3.8-flash": ModelPricing(
                input_usd_per_mtok=Decimal("0.75"), output_usd_per_mtok=Decimal("3.75"),
                source="https://ai.google.dev/gemini-api/docs/pricing", checked_at="2026-09-30",
                changes_on="2027-01-01", new_input_usd_per_mtok=Decimal("1.50"), new_output_usd_per_mtok=Decimal("7.50"),
            ),
            "gemini-3.5-flash-lite": ModelPricing(
                input_usd_per_mtok=Decimal("0.30"), output_usd_per_mtok=Decimal("2.50"),
                source="https://ai.google.dev/gemini-api/docs/pricing", checked_at="2026-09-29",
            ),
            # 출처: https://platform.claude.com/docs/en/about-claude/pricing (2026-09-29 확인)
            "claude-opus-5": ModelPricing(
                input_usd_per_mtok=Decimal("5"), output_usd_per_mtok=Decimal("25"),
                source="platform.claude.com/docs/en/about-claude/pricing", checked_at="2026-09-29",
            ),
            "claude-opus-4-8": ModelPricing(
                input_usd_per_mtok=Decimal("5"), output_usd_per_mtok=Decimal("25"),
                source="platform.claude.com/docs/en/about-claude/pricing", checked_at="2026-09-29",
            ),
            "claude-sonnet-5": ModelPricing(
                input_usd_per_mtok=Decimal("2"), output_usd_per_mtok=Decimal("10"),
                source="platform.claude.com/docs/en/about-claude/pricing", checked_at="2026-09-29",
            ),
            "claude-haiku-4-5": ModelPricing(
                input_usd_per_mtok=Decimal("1"), output_usd_per_mtok=Decimal("5"),
                source="platform.claude.com/docs/en/about-claude/pricing", checked_at="2026-09-29",
            ),
        }
    )

    @field_validator("daily_research_time_kst", "weekly_review_time_kst")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        h, _, m = v.partition(":")
        if not (h.isdigit() and m.isdigit() and 0 <= int(h) < 24 and 0 <= int(m) < 60):
            raise ValueError("HH:MM 형식이어야 합니다")
        return v


class PaperSettings(_Strict):
    fee_rate: Decimal = Decimal("0.0005")
    stock_fee_rate: Decimal = Decimal("0.00015")
    kr_sell_tax_rate: Decimal = Decimal("0.0020")  # 증권거래세 등(매도). 연도별 변경 → 사용자 확인 필요
    us_fee_rate: Decimal = Decimal("0.0025")
    slippage_bps: Decimal = Decimal("5")
    max_fill_fraction: Decimal = Decimal("0.5")
    seed: int = 7


class ExecutionSettings(_Strict):
    order_ttl_sec: int = Field(120, ge=10, le=3600)
    limit_offset_bps: Decimal = Decimal("10")
    liquidation_offset_bps: Decimal = Decimal("30")
    liquidation_max_reprice: int = 3
    unknown_resolution_window_sec: int = 900
    paper: PaperSettings = Field(default_factory=PaperSettings)


class ScheduleSettings(_Strict):
    quote_poll_sec: int = Field(15, ge=2, le=600)
    order_poll_sec: int = Field(5, ge=1, le=120)
    reconcile_interval_sec: int = Field(300, ge=30, le=3600)
    equity_snapshot_sec: int = Field(60, ge=10, le=3600)
    heartbeat_sec: int = Field(10, ge=2, le=120)
    news_poll_min: int = Field(60, ge=5, le=1440)
    fx_poll_min: int = Field(60, ge=5, le=1440)


class FxSettings(_Strict):
    provider: Literal["frankfurter", "manual", "none"] = "frankfurter"
    manual_usdkrw: Decimal | None = None
    manual_as_of: str | None = None


class NewsFeed(_Strict):
    name: str
    url: str
    markets: list[MarketName]
    enabled: bool = True
    user_agent: str | None = None
    keywords: dict[str, list[str]] = Field(default_factory=dict)  # instrument_id -> 키워드


class NewsSettings(_Strict):
    feeds: list[NewsFeed] = Field(
        default_factory=lambda: [
            NewsFeed(
                name="CoinDesk RSS",
                url="https://www.coindesk.com/arc/outboundfeeds/rss/",
                markets=["crypto"],
                keywords={
                    "crypto:KRW-BTC": ["bitcoin", "btc"],
                    "crypto:KRW-ETH": ["ether", "ethereum", "eth"],
                    "crypto:KRW-XRP": ["xrp", "ripple"],
                },
            )
        ]
    )
    naver_enabled: bool = False
    # 시장별 네이버 뉴스 검색어. 뉴스 수집 주기마다 검색어 1개당 1회 호출(무료 한도 하루 25,000회).
    naver_queries: dict[MarketName, list[str]] = Field(default_factory=lambda: {"crypto": ["비트코인", "이더리움", "리플"]})
    dart_enabled: bool = False
    max_items_per_feed: int = 30

    @field_validator("naver_queries")
    @classmethod
    def _queries(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for market, queries in v.items():
            if len(queries) > 10:
                raise ValueError(f"네이버 검색어는 시장별 최대 10개입니다({market}: {len(queries)}개)")
            if any(not q.strip() or len(q) > 100 for q in queries):
                raise ValueError("네이버 검색어는 비어 있지 않은 100자 이내여야 합니다")
        return v


class NotifySettings(_Strict):
    telegram: bool = False
    webhook: bool = False
    min_severity: Literal["info", "warning", "critical"] = "warning"
    orders: bool = True  # 주문 접수·체결·취소·거부 알림(min_severity와 무관하게 외부 채널로 전송)


class WebSettings(_Strict):
    host: str = "127.0.0.1"
    port: int = 8765
    require_login_for_read: bool = False
    allowed_hosts: list[str] = Field(default_factory=lambda: ["127.0.0.1", "localhost", "[::1]"])


class Settings(_Strict):
    operating_setting: SettingName = "A"
    risk: RiskSettings = Field(default_factory=RiskSettings)
    markets: dict[MarketName, MarketSettings] = Field(
        default_factory=lambda: {
            "crypto": MarketSettings(
                enabled=True, broker="upbit", account_id="upbit-main",
                instruments=["KRW-BTC", "KRW-ETH", "KRW-XRP"], candle="60m", allocation_krw=Decimal("300000"),
            ),
            "kr_stock": MarketSettings(
                enabled=False, broker="kis", account_id="kis-main", instruments=["005930"], candle="1d",
            ),
            "us_stock": MarketSettings(
                enabled=False, broker="kis", account_id="kis-main", instruments=["NASD:AAPL"], candle="1d",
            ),
        }
    )
    strategies: StrategySettings = Field(default_factory=StrategySettings)
    ai: AISettings = Field(default_factory=AISettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    fx: FxSettings = Field(default_factory=FxSettings)
    news: NewsSettings = Field(default_factory=NewsSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    web: WebSettings = Field(default_factory=WebSettings)

    @model_validator(mode="after")
    def _allocations(self) -> "Settings":
        for market, config in self.markets.items():
            if config.data_provider == "kiwoom" and (market not in ("kr_stock", "us_stock") or config.candle != "1d"):
                raise ValueError("키움 조회는 국내·미국주식 일봉만 지원합니다")
        total = sum((m.allocation_krw for m in self.markets.values() if m.enabled), Decimal(0))
        if total > self.risk.principal_cap_krw:
            raise ValueError("시장별 배정 합계가 원금 한도를 넘습니다")
        return self

    # ---- 편의 ----
    def enabled_markets(self) -> list[str]:
        return [k for k, m in self.markets.items() if m.enabled]

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)

    def hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()[:16]

    def live_scope(self, market: str) -> dict[str, Any]:
        """LIVE 활성화 범위. 이 값이 바뀌면 해당 시장의 LIVE는 다시 확인이 필요하다."""
        m = self.markets[market]  # type: ignore[index]
        return {
            "market": market,
            "broker": m.broker,
            "data_provider": m.data_provider,
            "account_id": m.account_id,
            "instruments": sorted(m.instruments),
            "allocation_krw": str(m.allocation_krw),
            "risk": self.risk.model_dump(mode="json"),
            "operating_setting": self.operating_setting,
        }


def scope_hash(scope: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:16]


def load_settings_file(path: Path) -> Settings:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return Settings.model_validate(data)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def instrument_id(market: str, symbol: str) -> str:
    return f"{market}:{symbol}"


def split_instrument_id(iid: str) -> tuple[str, str]:
    market, _, symbol = iid.partition(":")
    return market, symbol
