"""애플리케이션 구성(모드별 격리).

장부(book)
- operating: 사용자가 선택한 단일 운용 설정(A/B/C). internal_paper/offline_demo에서는 내부 모의체결,
  broker_sandbox에서는 증권사 모의투자, live에서는 실제 계좌(LIVE 가드 경유)로 주문한다.
- shadow_A / shadow_B / shadow_C: 같은 스냅샷으로 독립 운용되는 가상 장부(가상 원금, 실예산과 합산 금지).
  A=규칙 전략만, B=+AI 연구팀, C=+AI 연구팀·검증팀(ai/team.py).
- baseline_bh: 허용 종목 동일비중 매수·보유 기준, baseline_cash: 현금 유지 기준.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from aifund import __version__
from aifund.ai.budget import AIBudget
from aifund.ai.demo_responder import demo_responder
from aifund.ai.gemini import GeminiProvider, thinking_level_for
from aifund.ai.provider import AnthropicProvider, DemoProvider, LLMProvider, OllamaProvider
from aifund.ai.service import AIService
from aifund.ai.team import setting_roles
from aifund.brokers.base import BrokerAdapter, MarketData
from aifund.brokers.paper import PaperBroker
from aifund.config.settings import Settings
from aifund.config.store import SettingsStore, env_overrides
from aifund.control.flags import Flags, Incidents
from aifund.control.live import LiveActivations, LiveGuardedBroker
from aifund.core.money import D, ZERO
from aifund.core.paths import ModePaths
from aifund.core.secrets import ModeSecrets, load_mode_secrets
from aifund.core.timeutil import Clock, SystemClock, parse_iso
from aifund.data.collector import SnapshotCollector
from aifund.data.demo import DemoMarketData
from aifund.data.fx import FxService
from aifund.data.news import NewsCollector
from aifund.data.store import MarketStore
from aifund.db.database import Database, loads
from aifund.execution.executor import OrderExecutor
from aifund.execution.reconcile import Reconciler
from aifund.ledger.ledger import Ledger
from aifund.ledger.valuation import EquityTracker
from aifund.notify import Notifier

log = logging.getLogger(__name__)

SHADOW_SETTINGS = ("A", "B", "C")
OPERATING = "operating"


def code_version(root: Path) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=3).stdout.strip()
            return out.stdout.strip() + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        pass
    import hashlib

    h = hashlib.sha256()
    src = root / "src" / "aifund"
    for p in sorted(src.rglob("*.py")):
        h.update(p.read_bytes())
    return f"{__version__}+src.{h.hexdigest()[:10]}"


@dataclass
class MarketRuntime:
    market: str
    data: MarketData | None
    data_reason: str
    collector: SnapshotCollector | None
    operating_account: str
    operating_executor: OrderExecutor | None
    operating_broker: BrokerAdapter | None
    broker_reason: str
    reconciler: Reconciler | None = None


@dataclass
class AppContext:
    mode: str
    paths: ModePaths
    db: Database
    clock: Clock
    secrets: ModeSecrets
    store_settings: SettingsStore
    settings_version: int
    settings: Settings
    locked_settings: dict[str, str]
    market_store: MarketStore
    fx: FxService
    ledger: Ledger
    equity: EquityTracker
    flags: Flags
    incidents: Incidents
    notifier: Notifier
    activations: LiveActivations
    news: NewsCollector
    ai: AIService
    markets: dict[str, MarketRuntime] = field(default_factory=dict)
    shadow_executors: dict[str, OrderExecutor] = field(default_factory=dict)
    code_version: str = ""
    host: str = field(default_factory=socket.gethostname)
    startup_reconciled: dict[str, bool] = field(default_factory=dict)
    replay: MarketData | None = None
    _provider: LLMProvider | None = None

    # ---------------- 설정 ----------------
    def current_settings(self) -> Settings:
        return self.settings

    def reload_settings(self) -> bool:
        version, s = self.store_settings.current()
        if version == self.settings_version:
            return False
        s, locked = env_overrides(s)
        self.settings_version, self.settings, self.locked_settings = version, s, locked
        self._provider = None
        log.info("설정 버전 %s 적용", version)
        return True

    # ---------------- AI ----------------
    def provider(self) -> LLMProvider | None:
        if self._provider is not None:
            return self._provider
        s = self.settings.ai
        if self.mode == "offline_demo":
            self._provider = DemoProvider(demo_responder)
        elif s.provider == "anthropic" and self.secrets.anthropic_api_key:
            self._provider = AnthropicProvider(api_key=self.secrets.anthropic_api_key, model=s.model, effort=s.effort,
                                               use_fallbacks=s.use_server_fallbacks, timeout=s.timeout_sec,
                                               max_retries=s.max_retries)
        elif s.provider == "gemini" and self.secrets.gemini_api_key:
            self._provider = GeminiProvider(api_key=self.secrets.gemini_api_key, model=s.model, timeout=s.timeout_sec,
                                            thinking_level=thinking_level_for(s.model, s.effort))
        elif s.provider == "ollama":
            self._provider = OllamaProvider(model=s.model, host=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
        return self._provider

    def budget(self) -> AIBudget:
        return AIBudget(self.db, self.settings.ai, self.fx, self.clock)

    # ---------------- 장부 ----------------
    def book_ids(self) -> list[str]:
        return [r["book_id"] for r in self.ledger.books()]

    def book_setting(self, book_id: str) -> str:
        if book_id == OPERATING:
            return self.settings.operating_setting
        r = self.ledger.book(book_id)
        return r["setting"] if r else "?"

    def executor_for(self, book_id: str, market: str) -> OrderExecutor | None:
        if book_id == OPERATING:
            mr = self.markets.get(market)
            return mr.operating_executor if mr else None
        return self.shadow_executors.get(book_id)

    def all_executors(self) -> list[OrderExecutor]:
        seen: dict[str, OrderExecutor] = {}
        for mr in self.markets.values():
            if mr.operating_executor is not None:
                seen[mr.operating_executor.account_id] = mr.operating_executor
        for ex in self.shadow_executors.values():
            seen[ex.account_id] = ex
        return list(seen.values())

    def price(self, iid: str) -> Decimal | None:
        q = self.market_store.latest_quote(iid)
        if q is not None and q.mid is not None:
            return q.mid
        c = self.db.query_one("SELECT close FROM candles WHERE instrument_id=? ORDER BY open_time DESC LIMIT 1", (iid,))
        return D(c["close"]) if c else None

    def market_capital(self, book_id: str, market: str) -> Decimal:
        """장부의 시장별 운용 자본(KRW). live 운용 장부는 LIVE 활성화 때 실제 배정된 금액, 그 외는 원금 × 시장 배정 비율."""
        if book_id == OPERATING and self.mode == "live":
            rows = self.db.query("SELECT delta FROM ledger_entries WHERE book_id=? AND kind='principal' AND ref_type='live_alloc' "
                                 "AND ref_id=?", (book_id, market))
            return sum((D(r[0]) for r in rows), ZERO)
        cap = self.settings.risk.principal_cap_krw
        ms = self.settings.markets.get(market)  # type: ignore[call-overload]
        if ms is None or cap <= 0:
            return ZERO
        return self.ledger.principal(book_id) * ms.allocation_krw / cap

    def ai_cost_for_setting(self, setting: str) -> Decimal:
        """전략별 비용 배분: A=0, B=연구팀 비용 전액, C=연구팀+검증팀 비용 전액(공유 비용은 나누지 않고 각 설정에 모두 반영)."""
        roles = setting_roles(setting)
        if not roles:
            return ZERO
        marks = ",".join("?" for _ in roles)
        rows = self.db.query(f"SELECT cost_krw FROM ai_runs WHERE role IN ({marks}) AND cost_krw IS NOT NULL", roles)
        return sum((D(r[0]) for r in rows), ZERO)

    def risk_book(self) -> str:
        """리스크 매니저가 보는 장부: 판정이 적용되는 C 설정 장부(운용 장부가 C면 운용 장부, 아니면 비교 C)."""
        return OPERATING if self.settings.operating_setting == "C" else "shadow_C"

    def ai_portfolio(self, market: str) -> dict[str, Any]:
        """리스크 매니저 입력. 모든 값은 원장·설정에서 코드가 계산하며 항목마다 인용용 pf:* ID가 있다(한도는 읽기 전용)."""
        from aifund.ledger.valuation import ASSET_CLASS, value_book

        s = self.settings
        book = self.risk_book()
        val = value_book(self.db, self.ledger, book, self.price, self.market_store.instrument, self.fx, s.risk)
        eq = val.equity_krw

        def pct(v: Decimal) -> float | None:
            return round(float(v / eq * 100), 2) if eq > 0 else None

        qty: dict[str, dict[str, str]] = {}
        for p in self.ledger.positions(book):
            qty.setdefault(p.instrument_id, {})[p.strategy_id] = str(p.qty)
        items: list[dict[str, Any]] = [{
            "id": "pf:book", "kind": "book_summary", "book_id": book, "equity_krw": f"{eq:.0f}", "cash_krw": f"{val.cash_krw:.0f}",
            "reserved_krw": f"{val.reserved_krw:.0f}", "exposure_krw": f"{val.exposure_krw:.0f}",
            "open_positions": len(val.by_instrument), "stale": val.stale,
        }]
        for iid, v in sorted(val.by_instrument.items(), key=lambda kv: -kv[1]):
            inst = self.market_store.instrument(iid)
            items.append({"id": f"pf:pos:{iid}", "kind": "holding", "instrument_id": iid, "name": inst.name if inst else None,
                          "asset_class": ASSET_CLASS.get(iid.split(":")[0]), "value_krw": f"{v:.0f}", "pct_of_equity": pct(v),
                          "qty_by_strategy": qty.get(iid, {})})
        for mk, v in sorted(val.by_market.items()):
            items.append({"id": f"pf:market:{mk}", "kind": "market_exposure", "market": mk, "value_krw": f"{v:.0f}",
                          "pct_of_equity": pct(v), "allocation_krw": str(s.markets[mk].allocation_krw)})  # type: ignore[index]
        st = self.equity.state(book)
        items.append({"id": "pf:risk_state", "kind": "risk_state", "note": "손실 기준 기록 없음(첫 평가 전)"} if st is None else
                     {"id": "pf:risk_state", "kind": "risk_state", "daily_pnl_krw": f"{st.daily_pnl:.0f}",
                      "drawdown_pct": f"{st.drawdown_pct:.2f}", "daily_stop_active": st.daily_stop_active,
                      "drawdown_stop_active": st.drawdown_stop_active})
        items.append({"id": "pf:limits", "kind": "risk_limits_read_only", "max_open_positions": s.risk.max_open_positions,
                      "gross_exposure_cap_krw": str(s.risk.gross_exposure_cap_krw),
                      "max_order_notional_krw": str(s.risk.max_order_notional_krw),
                      "daily_loss_stop_krw": str(s.risk.daily_loss_stop_krw), "max_drawdown_stop_pct": str(s.risk.max_drawdown_stop_pct)})
        sleeve = s.strategies.sleeves_for(market).get("ai_research", ZERO) * self.market_capital(book, market)
        items.append({"id": f"pf:ai_sleeve:{market}", "kind": "ai_sleeve", "market": market, "sleeve_capital_krw": f"{sleeve:.0f}",
                      "note": "제안 target_weight는 이 금액 대비 비중"})
        now = self.clock.now()
        for other in s.enabled_markets():
            if other == market:
                continue
            rr = self.ai.latest_report("research", other)
            if rr is None or not rr["valid"] or (parse_iso(rr["expires_at"]) or now) <= now:
                continue
            for p in loads(rr["report_json"])["report"].get("proposals", []):
                items.append({"id": f"pf:ai:{other}:{p['proposal_ref']}", "kind": "other_market_ai_proposal", "market": other,
                              "instrument_id": p["instrument_id"], "action": p["action"], "target_weight": p["target_weight"],
                              "proposed_at": rr["created_at"], "rationale": p["rationale"][:200]})
        return {"book_id": book, "valuation_complete": not val.stale, "items": items}


def _paper_account(book_id: str) -> str:
    return f"paper:{book_id}"


def build_context(paths: ModePaths, *, clock: Clock | None = None, config_path: Path | None = None,
                  replay: MarketData | None = None, live_broker_factory: Any = None) -> AppContext:
    """AppContext 생성. 비밀은 모드에 맞는 것만 로딩된다. live_broker_factory는 테스트 주입용."""
    clock = clock or SystemClock()
    mode = paths.mode
    paths.ensure()
    db = Database(paths.db_path)
    db.migrate()
    if db.get_meta("mode") is None:
        db.set_meta("mode", mode)
    elif db.get_meta("mode") != mode:
        raise RuntimeError(f"DB 모드 불일치: {db.get_meta('mode')} ≠ {mode} (모드별 DB 격리 위반)")
    secrets = load_mode_secrets(mode)
    store_settings = SettingsStore(db)
    version, settings = store_settings.ensure_initialized(config_path)
    settings, locked = env_overrides(settings)
    market_store = MarketStore(db, clock)
    fx = FxService(db, provider=settings.fx.provider if mode != "offline_demo" else "manual", clock=clock,
                   manual_rate=settings.fx.manual_usdkrw if mode != "offline_demo" else D("1400"),
                   manual_as_of=settings.fx.manual_as_of)
    ledger = Ledger(db, clock)
    flags = Flags(db, clock)
    holder: dict[str, AppContext] = {}
    notifier = Notifier(db, clock, lambda: holder["ctx"].settings, secrets, mode)
    incidents = Incidents(db, clock, notifier)
    news = NewsCollector(db, clock)
    ai = AIService(db=db, clock=clock, settings_fn=lambda: holder["ctx"].settings, budget_fn=lambda: holder["ctx"].budget(),
                   provider_fn=lambda: holder["ctx"].provider(), news=news, incidents=incidents, mode=mode,
                   portfolio_fn=lambda market: holder["ctx"].ai_portfolio(market))
    ctx = AppContext(mode=mode, paths=paths, db=db, clock=clock, secrets=secrets, store_settings=store_settings,
                     settings_version=version, settings=settings, locked_settings=locked, market_store=market_store, fx=fx,
                     ledger=ledger, equity=EquityTracker(db, clock), flags=flags, incidents=incidents, notifier=notifier,
                     activations=LiveActivations(db, clock), news=news, ai=ai, code_version=code_version(paths.root),
                     replay=replay)
    holder["ctx"] = ctx
    _setup_books(ctx)
    if mode in ("internal_paper", "offline_demo"):
        _resize_paper_books(ctx)
    _setup_markets(ctx, live_broker_factory)
    return ctx


def _setup_books(ctx: AppContext) -> None:
    s = ctx.settings
    principal = s.risk.principal_cap_krw
    op_account = _paper_account(OPERATING) if ctx.mode in ("offline_demo", "internal_paper") else f"{ctx.mode}:by-market"
    # live: 원금 0으로 시작하고 LIVE 활성화 때 min(시장 배정액, 실제 주문가능금액)만 명시적으로 배정한다.
    ctx.ledger.create_book(OPERATING, kind="operating", setting=s.operating_setting,
                           principal_krw=principal if ctx.mode != "live" else ZERO,
                           virtual=ctx.mode in ("offline_demo", "internal_paper", "broker_sandbox"), account_id=op_account,
                           description="운용 장부(선택된 단일 설정)" if ctx.mode != "live" else "실계좌 운용 장부(시장별 LIVE 활성화 시 배정)")
    for st in SHADOW_SETTINGS:
        ctx.ledger.create_book(f"shadow_{st}", kind="shadow", setting=st, principal_krw=principal, virtual=True,
                               account_id=_paper_account(f"shadow_{st}"), description=f"{st} 설정 가상 비교 장부")
    ctx.ledger.create_book("baseline_bh", kind="baseline", setting="BUY_HOLD", principal_krw=principal, virtual=True,
                           account_id=_paper_account("baseline_bh"), description="허용 종목 동일비중 매수·보유 기준")
    ctx.ledger.create_book("baseline_cash", kind="baseline", setting="CASH", principal_krw=principal, virtual=True,
                           account_id=_paper_account("baseline_cash"), description="현금 유지 기준")


def _resize_paper_books(ctx: AppContext) -> None:
    """모의 원금 변경을 기록한다. 잔고/주문 예약은 보존하며 실제 계좌에는 적용하지 않는다."""
    from aifund.core.money import dstr
    from aifund.core.timeutil import to_iso
    from aifund.ledger.ledger import BOOK_STRATEGY

    with ctx.db.tx() as c:
        for book in ctx.ledger.books():
            bid = book["book_id"]
            delta = ctx.settings.risk.principal_cap_krw - ctx.ledger.principal(bid)
            if not delta:
                continue
            reserved = sum((D(r[0]) for r in c.execute(
                "SELECT amount_remaining FROM reservations WHERE book_id=? AND asset='KRW' AND kind='cash' AND status='active'",
                (bid,)).fetchall()), ZERO)
            balance = c.execute("SELECT free FROM paper_balances WHERE account_id=? AND asset='KRW'", (_paper_account(bid),)).fetchone()
            if ctx.ledger.cash(bid, "KRW", c) - reserved + delta < 0 or (balance and D(balance[0]) + delta < 0):
                raise ValueError("모의 원금 축소에 필요한 원화 현금 부족: 보유분·예약금을 먼저 정리하세요")
            ctx.ledger._entry(c, bid, to_iso(ctx.clock.now()), "principal", BOOK_STRATEGY, "KRW", delta, None,
                              "paper_capital", str(ctx.settings_version), "설정 변경에 따른 가상 원금 조정")
            c.execute("UPDATE books SET principal_krw=? WHERE book_id=?", (dstr(ctx.settings.risk.principal_cap_krw), bid))
            risk = c.execute("SELECT day_start_equity, peak_equity FROM risk_state WHERE book_id=?", (bid,)).fetchone()
            if risk:
                c.execute("UPDATE risk_state SET day_start_equity=?, peak_equity=? WHERE book_id=?",
                          (dstr(D(risk[0]) + delta), dstr(D(risk[1]) + delta), bid))
            if balance:
                c.execute("UPDATE paper_balances SET free=? WHERE account_id=? AND asset='KRW'",
                          (dstr(D(balance[0]) + delta), _paper_account(bid)))


def _paper(ctx: AppContext, book_id: str) -> PaperBroker:
    pb = PaperBroker(ctx.db, _paper_account(book_id), ctx.market_store, ctx.clock, ctx.settings.execution.paper)
    principal = ctx.ledger.principal(book_id)
    pb.fund("KRW", principal)
    return pb


def _executor(ctx: AppContext, broker: BrokerAdapter) -> OrderExecutor:
    return OrderExecutor(db=ctx.db, ledger=ctx.ledger, broker=broker, store=ctx.market_store, fx=ctx.fx, flags=ctx.flags,
                         incidents=ctx.incidents, clock=ctx.clock, settings_fn=lambda: ctx.settings, mode=ctx.mode, notifier=ctx.notifier)


def _setup_markets(ctx: AppContext, live_broker_factory: Any) -> None:
    from aifund.brokers.kis import KisBroker, KisClient, KisMarketData
    from aifund.brokers.upbit import UpbitBroker, UpbitMarketData

    s = ctx.settings
    for book in ("shadow_A", "shadow_B", "shadow_C", "baseline_bh"):
        ctx.shadow_executors[book] = _executor(ctx, _paper(ctx, book))
    op_paper = _executor(ctx, _paper(ctx, OPERATING)) if ctx.mode in ("offline_demo", "internal_paper") else None
    kis_data_client = None
    kiwoom_data_client = None
    for market, ms in s.markets.items():
        if not ms.enabled:
            continue
        data: MarketData | None = None
        reason = ""
        if ctx.replay is not None:
            data, reason = ctx.replay, "재생 데이터(명시적)"
        elif ctx.mode == "offline_demo":
            data, reason = DemoMarketData(ctx.clock), "데모(가짜 데이터)"
        elif market == "crypto":
            data, reason = UpbitMarketData(clock=ctx.clock), "업비트 공개 시세"
        elif market in ("kr_stock", "us_stock") and ms.data_provider == "kiwoom":
            from aifund.brokers.kiwoom import KiwoomReadClient, KiwoomMarketData, KiwoomUSMarketData
            if ctx.mode == "live" and ctx.secrets.kiwoom_data and ctx.secrets.kiwoom_data.env == "mock":
                reason = "미연결: 실거래에서 키움 모의 시세 사용 차단"
            elif ctx.secrets.kiwoom_data is not None:
                if kiwoom_data_client is None:
                    kiwoom_data_client = KiwoomReadClient(ctx.secrets.kiwoom_data, clock=ctx.clock)
                data_class = KiwoomUSMarketData if market == "us_stock" else KiwoomMarketData
                data = data_class(kiwoom_data_client)
                reason = "키움 REST 조회 전용 (" + ctx.secrets.kiwoom_data.env + ")"
            else:
                reason = "미연결: KIWOOM_DATA_APP_KEY/SECRET 미설정"
        elif ctx.secrets.kis_data is not None:
            if kis_data_client is None:
                kis_data_client = KisClient(ctx.secrets.kis_data, token_cache_dir=ctx.paths.secrets_dir, clock=ctx.clock)
            data, reason = KisMarketData(kis_data_client, clock=ctx.clock), "KIS 시세"
        else:
            reason = "미연결: KIS 시세용 앱키(KIS_DATA_* 또는 KIS_LIVE_*) 미설정"
        broker: BrokerAdapter | None = None
        breason = ""
        account = ms.account_id
        if ctx.mode in ("offline_demo", "internal_paper"):
            broker, breason, account = (op_paper.broker if op_paper else None), "내부 모의체결", _paper_account(OPERATING)
        elif live_broker_factory is not None:
            inner = live_broker_factory(market, ms)
            broker = LiveGuardedBroker(inner, market, ctx.mode, ctx.activations, lambda: ctx.settings) if ctx.mode == "live" else inner
            breason = "테스트 주입 브로커"
        elif ctx.mode == "broker_sandbox":
            if market == "crypto":
                breason = "미지원: 업비트 공식 모의투자 환경 미확인"
            elif ms.broker != "kis":
                breason = "미지원: 증권사 공식 모의주문은 KIS만 구현됨; 키움 시세는 internal_paper 사용"
            elif ctx.secrets.kis_trade is None or ctx.secrets.kis_trade.env != "demo":
                breason = "미연결: KIS_SANDBOX_* 키 미설정"
            else:
                client = KisClient(ctx.secrets.kis_trade, token_cache_dir=ctx.paths.secrets_dir, clock=ctx.clock)
                broker, breason = KisBroker(ms.account_id, market, client, live_money=False, clock=ctx.clock), "KIS 모의투자"
        elif ctx.mode == "live":
            inner_b: BrokerAdapter | None = None
            if ms.broker == "upbit" and market == "crypto":
                if ctx.secrets.upbit is None:
                    breason = "미연결: UPBIT_LIVE_ACCESS_KEY/SECRET_KEY 미설정"
                else:
                    inner_b = UpbitBroker(ms.account_id, ctx.secrets.upbit, live_money=True, clock=ctx.clock)
            elif ms.broker == "kis" and market in ("kr_stock", "us_stock"):
                if ctx.secrets.kis_trade is None or ctx.secrets.kis_trade.env != "real":
                    breason = "미연결: KIS_LIVE_* 키 미설정"
                else:
                    client = KisClient(ctx.secrets.kis_trade, token_cache_dir=ctx.paths.secrets_dir, clock=ctx.clock)
                    inner_b = KisBroker(ms.account_id, market, client, live_money=True, clock=ctx.clock)
            else:
                breason = f"지원하지 않는 조합: {market}/{ms.broker}"
            if inner_b is not None:
                broker = LiveGuardedBroker(inner_b, market, "live", ctx.activations, lambda: ctx.settings)
                breason = "실계좌(LIVE 가드: 활성화 전 주문 차단)"
        if op_paper is not None and broker is op_paper.broker:
            executor = op_paper
        else:
            executor = _executor(ctx, broker) if broker is not None else None
        meta = None
        if broker is not None and ctx.mode in ("live", "broker_sandbox") and broker.capabilities.instrument_meta:
            meta = broker.instrument_meta
        collector = SnapshotCollector(ctx.db, ctx.market_store, data, ctx.clock, meta) if data is not None else None
        ctx.markets[market] = MarketRuntime(market, data, reason, collector, account, executor, broker, breason)
