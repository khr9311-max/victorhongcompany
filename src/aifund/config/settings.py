"""운용 설정 모델.

설정은 DB의 settings_versions에 버전으로 기록되며(변경 이력·적용 버전 추적),
config/config.toml은 최초/명시적 가져오기 용도다. AI에는 설정 변경 경로가 없다.
엔지니어링 프리셋은 검증된 최적 투자조건이 아니다.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
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
    broker: Literal["upbit", "kis"] = "upbit"
    account_id: str = "upbit-main"
    instruments: list[str] = Field(default_factory=list)
    candle: str = "60m"  # 코인: "60m" 등 분봉, 주식: "1d"
    allocation_krw: Decimal = Decimal("0")
    decision_delay_sec: int = 20  # 봉 마감 후 대기(거래소 반영 지연 흡수)
    stock_decision_after_open_min: int = 10

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


class StrategySettings(_Strict):
    trend_sma: StrategyToggle = Field(default_factory=StrategyToggle)
    mean_reversion: StrategyToggle = Field(default_factory=StrategyToggle)
    # 슬리브: 원금 대비 전략별 배정 비율. ai_research는 A 설정에서는 현금으로 유지된다.
    sleeves: dict[str, Decimal] = Field(
        default_factory=lambda: {"trend_sma": Decimal("0.4"), "mean_reversion": Decimal("0.4"), "ai_research": Decimal("0.2")}
    )
    rebalance_threshold_krw: Decimal = Decimal("10000")

    @model_validator(mode="after")
    def _sleeves(self) -> "StrategySettings":
        total = sum(self.sleeves.values(), Decimal(0))
        if total > Decimal("1.0000001"):
            raise ValueError("전략 슬리브 합은 1을 넘을 수 없습니다")
        if any(v < 0 for v in self.sleeves.values()):
            raise ValueError("슬리브는 음수일 수 없습니다")
        return self


class ModelPricing(_Strict):
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    source: str
    checked_at: str


class AISettings(_Strict):
    enabled: bool = True
    provider: Literal["anthropic", "ollama", "disabled"] = "anthropic"
    model: str = "claude-opus-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    use_server_fallbacks: bool = True
    monthly_budget_krw: Decimal = Decimal("5000")
    max_output_tokens: int = Field(4000, ge=256, le=32000)
    max_input_chars: int = Field(24000, ge=2000, le=200000)
    timeout_sec: float = 120.0
    max_retries: int = Field(1, ge=0, le=3)
    fallback_usdkrw: Decimal = Decimal("1500")
    daily_research_time_kst: str = "08:50"
    weekly_review_weekday: int = Field(0, ge=0, le=6)  # 0=월요일
    weekly_review_time_kst: str = "09:10"
    event_move_pct: Decimal = Decimal("5")
    max_event_calls_per_day: int = 1
    independent_review_pass: bool = True
    veto_rule_buys: bool = True
    report_ttl_hours: int = Field(24, ge=1, le=72)
    when_unavailable: Literal["continue_rules", "hold_new_risk"] = "continue_rules"
    pricing: dict[str, ModelPricing] = Field(
        default_factory=lambda: {
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
    dart_enabled: bool = False
    max_items_per_feed: int = 30


class NotifySettings(_Strict):
    telegram: bool = False
    webhook: bool = False
    min_severity: Literal["info", "warning", "critical"] = "warning"


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
