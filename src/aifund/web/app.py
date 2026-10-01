"""한국어 대표 대시보드(FastAPI + Jinja2 서버 렌더링). 버튼은 모두 실제 기능에 연결된다."""

from __future__ import annotations

import logging
from datetime import datetime
from importlib import resources
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from aifund.config.settings import Settings
from aifund.control import actions
from aifund.control.readiness import disable_live, enable_live, live_readiness
from aifund.control.selftest import run_selftest
from aifund.core.money import D, fmt_krw, fmt_num
from aifund.core.paths import MODE_LABELS
from aifund.core.timeutil import kst_str, parse_iso
from aifund.evaluation import candidates as cand
from aifund.lab.catalog import all_variants, find
from aifund.service.context import AppContext
from aifund.strategies.lab import PREFIX as LAB_PREFIX, is_lab
from aifund.web import charts, labels, views
from aifund.web.auth import COOKIE, check_host, make_session, read_session, token_ok

log = logging.getLogger(__name__)
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _fmt_pct(v: Any, nd: int = 2) -> str:
    if v is None:
        return "-"
    return f"{D(v):.{nd}f}%"


def _num(v: Any, nd: int = 8) -> str:
    if v is None or v == "":
        return "-"
    try:
        return fmt_num(D(v), nd)
    except Exception:
        return str(v)


def ago_text(value: Any, now: datetime) -> str:
    """'3분 전'처럼 지금 기준 경과 시간(미래면 '… 후')."""
    dt = parse_iso(value) if isinstance(value, str) else value
    if not dt:
        return "-"
    sec = (now - dt).total_seconds()
    suffix = "전" if sec >= 0 else "후"
    sec = abs(sec)
    if sec < 45:
        return "방금" if suffix == "전" else "곧"
    for unit, size in (("일", 86400), ("시간", 3600), ("분", 60)):
        if sec >= size:
            return f"{int(sec // size)}{unit} {suffix}"
    return f"{int(sec)}초 {suffix}"


def krw_short(v: Any) -> str:
    """큰 원화 금액 요약(예: 5,012,300 → 501.2만원). 정확한 값은 옆에 전체 금액으로 함께 표시한다."""
    if v is None or v == "":
        return "-"
    x = D(v)
    sign = "-" if x < 0 else ""
    x = abs(x)
    if x >= 100_000_000:
        return f"{sign}{x / 100_000_000:,.2f}억원"
    if x >= 10_000:
        return f"{sign}{x / 10_000:,.1f}만원"
    return f"{sign}{x:,.0f}원"


def create_app(ctx: AppContext, runtime: Any = None) -> FastAPI:
    app = FastAPI(title="빅터홍컴퍼니 AI 투자회사", docs_url=None, redoc_url=None, openapi_url=None)
    tpl_dir = resources.files("aifund.web").joinpath("templates")
    static_dir = resources.files("aifund.web").joinpath("static")
    templates = Jinja2Templates(directory=str(tpl_dir))
    templates.env.filters.update({
        "krw": lambda v, sign=False: fmt_krw(D(v), sign) if v is not None and v != "" else "-",
        "num": _num, "kst": lambda v: kst_str(v) if v else "-", "pct": _fmt_pct,
        "label": lambda v, kind: views.LABELS.get(kind, {}).get(v, v),
        "ago": lambda v: ago_text(v, ctx.clock.now()), "krw_short": krw_short,
        "trigger": labels.trigger_label, "flag": labels.flag_label, "chart": charts.spec_json,
        "hhmm": lambda v: kst_str(v, with_seconds=False)[5:-4] if v else "-",
    })
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    web = ctx.settings.web
    remote = web.host not in LOOPBACK

    def session(request: Request) -> dict[str, Any] | None:
        return read_session(request.cookies.get(COOKIE), ctx.secrets.admin_token, ctx.mode)

    def guard_read(request: Request) -> dict[str, Any] | None:
        check_host(request, ctx.settings.web.allowed_hosts + ([web.host] if remote else []), web.port)
        sess = session(request)
        if (remote or ctx.settings.web.require_login_for_read) and sess is None:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return sess

    async def guard_write(request: Request) -> dict[str, Any]:
        check_host(request, ctx.settings.web.allowed_hosts + ([web.host] if remote else []), web.port)
        if not ctx.secrets.admin_token:
            raise HTTPException(status_code=403, detail="관리자 토큰 미설정: `aifund setup`을 실행해 .env에 AIFUND_ADMIN_TOKEN을 만드세요")
        sess = session(request)
        if sess is None:
            raise HTTPException(status_code=401, detail="로그인이 필요합니다")
        form = await request.form()
        if not token_ok(str(form.get("csrf", "")), sess.get("csrf")):
            raise HTTPException(status_code=403, detail="CSRF 토큰 불일치")
        return sess

    def render(request: Request, name: str, sess: dict[str, Any] | None, **data: Any) -> HTMLResponse:
        base = {"request": request, "mode": ctx.mode, "sess": sess, "csrf": (sess or {}).get("csrf", ""),
                "msg": request.query_params.get("msg"), "err": request.query_params.get("err"),
                "demo": ctx.mode == "offline_demo", "live": ctx.mode == "live", "L": views.LABELS,
                "mode_label": MODE_LABELS[ctx.mode], "path": request.url.path, "now": ctx.clock.now(),
                "service_status": runtime.status if runtime else None}
        return templates.TemplateResponse(request, name, base | data)

    def back(path: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
        q = []
        if msg:
            q.append("msg=" + quote(msg[:500]))
        if err:
            q.append("err=" + quote(err[:500]))
        return RedirectResponse(path + ("?" + "&".join(q) if q else ""), status_code=303)

    # ------------------------------------------------------------------ 인증
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
        check_host(request, ctx.settings.web.allowed_hosts + ([web.host] if remote else []), web.port)
        return render(request, "login.html", None, has_token=bool(ctx.secrets.admin_token))

    @app.post("/login")
    async def login(request: Request, token: str = Form(...)) -> RedirectResponse:
        check_host(request, ctx.settings.web.allowed_hosts + ([web.host] if remote else []), web.port)
        if not token_ok(token, ctx.secrets.admin_token):
            return back("/login", err="토큰이 올바르지 않습니다")
        cookie, _ = make_session(ctx.secrets.admin_token or "", ctx.mode)
        resp = back("/", msg="로그인했습니다")
        resp.set_cookie(COOKIE, cookie, httponly=True, samesite="strict", max_age=12 * 3600)
        return resp

    @app.post("/logout")
    async def logout(request: Request, sess: dict = Depends(guard_write)) -> RedirectResponse:
        resp = back("/", msg="로그아웃")
        resp.delete_cookie(COOKIE)
        return resp

    # ------------------------------------------------------------------ 조회
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request, sess: dict | None = Depends(guard_read)) -> HTMLResponse:
        return render(request, "overview.html", sess, d=views.overview(ctx, runtime))

    @app.get("/strategies", response_class=HTMLResponse)
    async def strategies(request: Request, sess: dict | None = Depends(guard_read)) -> HTMLResponse:
        return render(request, "strategies.html", sess, d=views.strategies_page(ctx))

    @app.get("/research", response_class=HTMLResponse)
    async def research(request: Request, sess: dict | None = Depends(guard_read)) -> HTMLResponse:
        return render(request, "research.html", sess, d=views.research_page(ctx))

    @app.get("/orders", response_class=HTMLResponse)
    async def orders(request: Request, book: str | None = None, sess: dict | None = Depends(guard_read)) -> HTMLResponse:
        return render(request, "orders.html", sess, d=views.orders_page(ctx, book))

    @app.get("/orders/{order_id}", response_class=HTMLResponse)
    async def order_detail(request: Request, order_id: str, sess: dict | None = Depends(guard_read)) -> HTMLResponse:
        d = views.order_trace(ctx, order_id)
        if d is None:
            raise HTTPException(404, "주문 없음")
        return render(request, "order.html", sess, d=d)

    @app.get("/control", response_class=HTMLResponse)
    async def control(request: Request, sess: dict | None = Depends(guard_read)) -> HTMLResponse:
        return render(request, "control.html", sess, d=views.control_page(ctx))

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_view(request: Request, sess: dict | None = Depends(guard_read)) -> HTMLResponse:
        return render(request, "settings.html", sess, d=views.settings_page(ctx))

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": runtime.status if runtime else "dashboard", "mode": ctx.mode})

    @app.get("/api/status")
    async def api_status(request: Request, sess: dict | None = Depends(guard_read)) -> JSONResponse:
        cap = views.capital(ctx)
        return JSONResponse({
            "mode": ctx.mode, "status": runtime.status if runtime else "dashboard", "settings_version": ctx.settings_version,
            "equity_krw": str(cap["equity"]), "cash_krw": str(cap["cash"]), "reserved_krw": str(cap["reserved"]),
            "exposure_krw": str(cap["exposure"]), "pnl_krw": str(cap["pnl"]), "ai_cost_krw": str(cap["ai_cost"]),
            "flags": [f["key"] for f in ctx.flags.all()], "startup_reconciled": ctx.startup_reconciled,
        })

    # ------------------------------------------------------------------ 제어
    @app.post("/control/halt")
    async def c_halt(request: Request, scope: str = Form(...), reason: str = Form(""), sess: dict = Depends(guard_write)):
        return back("/control", msg=actions.halt(ctx, scope, reason, "web"))

    @app.post("/control/resume")
    async def c_resume(request: Request, scope: str = Form(...), sess: dict = Depends(guard_write)):
        return back("/control", msg=actions.resume(ctx, scope, "web"))

    @app.post("/control/cancel-open")
    async def c_cancel(request: Request, market: str = Form(""), sess: dict = Depends(guard_write)):
        res = await actions.cancel_open(ctx, market or None, "web")
        return back("/control", msg=f"미체결 취소 요청 {len(res)}건: " + "; ".join(f"{a[:10]}…:{b}" for a, b in res[:5]))

    @app.post("/control/liquidate")
    async def c_liq(request: Request, market: str = Form(...), phrase: str = Form(...), sess: dict = Depends(guard_write)):
        try:
            res = await actions.liquidate(ctx, market, phrase, "web")
        except (PermissionError, RuntimeError) as exc:
            return back("/control", err=str(exc))
        return back("/control", msg=f"청산 주문 {len(res)}건 처리(지정가). 주문 화면에서 체결을 확인하세요.")

    @app.post("/control/live/check")
    async def c_check(request: Request, market: str = Form(...), ack: str = Form(""), sess: dict = Depends(guard_write)):
        items = await live_readiness(ctx, market, ack_no_withdraw=ack == "yes")
        failed = [i for i in items if i.blocking and not i.ok]
        return back("/control", msg="사전 점검 통과" if not failed else None,
                    err=("실패: " + "; ".join(i.name for i in failed)) if failed else None)

    @app.post("/control/live/enable")
    async def c_enable(request: Request, market: str = Form(...), phrase: str = Form(...), ack: str = Form(""),
                       sess: dict = Depends(guard_write)):
        ok, _items, msg = await enable_live(ctx, market, phrase, "web", ack_no_withdraw=ack == "yes")
        return back("/control", msg=msg if ok else None, err=None if ok else msg)

    @app.post("/control/live/disable")
    async def c_disable(request: Request, market: str = Form(...), reason: str = Form(""), sess: dict = Depends(guard_write)):
        return back("/control", msg=disable_live(ctx, market, "web", reason))

    @app.post("/control/selftest")
    async def c_selftest(request: Request, sess: dict = Depends(guard_write)):
        res = await run_selftest(ctx.db, ctx.code_version)
        bad = [r.name for r in res if not r.ok]
        return back("/control", msg=None if bad else f"자체검증 {len(res)}개 통과", err=("실패: " + ", ".join(bad)) if bad else None)

    @app.post("/control/reconcile")
    async def c_recon(request: Request, sess: dict = Depends(guard_write)):
        if runtime is not None:
            await runtime.reconcile_all("manual_web")
        return back("/orders", msg="대사 실행")

    @app.post("/control/reset-drawdown")
    async def c_reset_dd(request: Request, confirm: str = Form(...), sess: dict = Depends(guard_write)):
        if confirm.strip() != "낙폭 정지 해제":
            return back("/control", err="확인 문구 '낙폭 정지 해제'를 정확히 입력하세요")
        cap = views.capital(ctx)
        ctx.equity.reset_drawdown("operating", "web", "사용자 해제", cap["equity"])
        return back("/control", msg="최대 낙폭 정지를 해제하고 현재 자산을 새 고점으로 설정했습니다")

    @app.post("/control/research")
    async def c_research(request: Request, market: str = Form(...), sess: dict = Depends(guard_write)):
        if runtime is None:
            return back("/research", err="서비스가 실행 중이 아닙니다")
        rid = await runtime.run_research(market, "manual")
        return back("/research", msg=f"연구 실행: {rid or '생략/실패(실행 기록 참고)'}")

    # ------------------------------------------------------------------ 설정
    @app.post("/settings/save")
    async def s_save(request: Request, sess: dict = Depends(guard_write)):
        form = await request.form()
        try:
            new = apply_form(ctx.settings, {k: str(v) for k, v in form.items()})
        except (ValueError, KeyError) as exc:
            return back("/settings", err=f"검증 실패: {str(exc)[:400]}")
        reason = str(form.get("reason") or "웹 설정 변경")
        version = ctx.store_settings.save(new, "web", reason)
        ctx.reload_settings()
        return back("/settings", msg=f"설정 버전 {version} 저장·적용(LIVE 범위가 바뀌면 재확인 필요)")

    @app.post("/settings/candidate/{cid}/{action}")
    async def s_candidate(request: Request, cid: str, action: str, sess: dict = Depends(guard_write)):
        try:
            if action == "promote":
                v = cand.promote(ctx, cid, "web")
                return back("/settings", msg=f"후보 승격 → 설정 버전 {v}")
            if action == "rollback":
                v = cand.rollback(ctx, cid, "web")
                return back("/settings", msg=f"되돌리기 → 설정 버전 {v}")
            if action == "backtest":
                cand.backtest_candidate(ctx, cid)
                return back("/settings", msg="백테스트 완료(개발·평가 구간 결과 참고)")
        except (ValueError, LookupError, RuntimeError) as exc:
            return back("/settings", err=str(exc))
        raise HTTPException(404)

    @app.exception_handler(HTTPException)
    async def http_exc(request: Request, exc: HTTPException):  # type: ignore[no-untyped-def]
        if exc.status_code == 303 and exc.headers:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        return HTMLResponse(f"<h3>{exc.status_code}</h3><p>{exc.detail}</p><p><a href='/'>처음으로</a></p>", status_code=exc.status_code)

    return app


FORM_FIELDS: dict[str, str] = {
    # form name → settings path
    "operating_setting": "operating_setting",
    "risk.principal_cap_krw": "risk.principal_cap_krw",
    "risk.gross_exposure_cap_krw": "risk.gross_exposure_cap_krw",
    "risk.max_order_notional_krw": "risk.max_order_notional_krw",
    "risk.max_open_positions": "risk.max_open_positions",
    "risk.daily_loss_stop_krw": "risk.daily_loss_stop_krw",
    "risk.max_drawdown_stop_pct": "risk.max_drawdown_stop_pct",
    "risk.max_quote_age_sec": "risk.max_quote_age_sec",
    "risk.max_spread_pct": "risk.max_spread_pct",
    "strategies.rebalance_threshold_krw": "strategies.rebalance_threshold_krw",
    "ai.enabled": "ai.enabled",
    "ai.provider": "ai.provider",
    "ai.model": "ai.model",
    "ai.effort": "ai.effort",
    "ai.monthly_budget_krw": "ai.monthly_budget_krw",
    "ai.max_output_tokens": "ai.max_output_tokens",
    "ai.daily_research_time_kst": "ai.daily_research_time_kst",
    "ai.crypto_research_interval_hours": "ai.crypto_research_interval_hours",
    "ai.stock_research_lead_min": "ai.stock_research_lead_min",
    "ai.max_event_calls_per_day": "ai.max_event_calls_per_day",
    "ai.max_news_items": "ai.max_news_items",
    "ai.max_input_chars": "ai.max_input_chars",
    "ai.timeout_sec": "ai.timeout_sec",
    "ai.when_unavailable": "ai.when_unavailable",
    "ai.veto_rule_buys": "ai.veto_rule_buys",
    "ai.independent_review_pass": "ai.independent_review_pass",
    "ai.news_analyst_enabled": "ai.news_analyst_enabled",
    "ai.quant_analyst_enabled": "ai.quant_analyst_enabled",
    "ai.risk_manager_enabled": "ai.risk_manager_enabled",
    "ai.analyst_effort": "ai.analyst_effort",
    "schedule.quote_poll_sec": "schedule.quote_poll_sec",
    "schedule.order_poll_sec": "schedule.order_poll_sec",
    "news.naver_enabled": "news.naver_enabled",
    "notify.telegram": "notify.telegram",
    "notify.webhook": "notify.webhook",
    "notify.min_severity": "notify.min_severity",
}


def apply_form(current: Settings, form: dict[str, str]) -> Settings:
    data = current.model_dump(mode="json")

    def put(path: str, value: Any) -> None:
        cur = data
        parts = path.split(".")
        for p in parts[:-1]:
            cur = cur[p]
        old = cur[parts[-1]]
        if isinstance(old, bool):
            value = value in ("on", "true", "1", "yes")
        elif isinstance(old, int) and not isinstance(old, bool):
            value = int(value)
        cur[parts[-1]] = value

    for name, path in FORM_FIELDS.items():
        if name in form and form[name] != "":
            put(path, form[name])
        elif name.endswith(("enabled", "veto_rule_buys", "independent_review_pass", "telegram", "webhook")) and "_bools" in form:
            put(path, "off")
    for market in ("crypto", "kr_stock", "us_stock"):
        if f"m.{market}.present" not in form:
            continue
        m = data["markets"][market]
        m["enabled"] = form.get(f"m.{market}.enabled") == "on"
        if form.get(f"m.{market}.instruments") is not None:
            m["instruments"] = [x.strip() for x in form[f"m.{market}.instruments"].split(",") if x.strip()]
        if form.get(f"m.{market}.allocation_krw"):
            m["allocation_krw"] = form[f"m.{market}.allocation_krw"]
        if form.get(f"m.{market}.data_provider"):
            m["data_provider"] = form[f"m.{market}.data_provider"]
        if form.get(f"m.{market}.account_id"):
            m["account_id"] = form[f"m.{market}.account_id"].strip()
        if form.get(f"n.{market}.queries") is not None:
            queries = [x.strip() for x in form[f"n.{market}.queries"].split(",") if x.strip()]
            if queries:
                data["news"]["naver_queries"][market] = queries
            else:
                data["news"]["naver_queries"].pop(market, None)
    if "ai.research_focus" in form:  # 비워서 저장하면 관심사 지시를 없앤다
        data["ai"]["research_focus"] = form["ai.research_focus"].strip()
    st = data["strategies"]
    for v in all_variants():  # 연구소 전략: 설정에 있는 것은 사용·시장을 고치고, 카탈로그에서 '추가'를 체크한 것은 새로 넣는다
        on = form.get(f"lab.{v.id}.enabled") == "on"
        if f"lab.{v.id}.present" not in form and not (on and v.id not in st["lab"]):
            continue
        markets = [m for m in ("crypto", "kr_stock", "us_stock") if form.get(f"lab.{v.id}.m.{m}") == "on"]
        if on and not markets:
            raise ValueError(f"연구소 {v.label}: 쓸 시장을 하나 이상 고르세요")
        st["lab"][v.id] = {"enabled": on, "markets": markets}
    sleeve_sets = {"sleeve.": st["sleeves"]} | {f"msleeve.{m}.": sl for m, sl in st["market_sleeves"].items()}
    for key, value in form.items():  # 슬리브 표의 칸(기본·시장별). 비우면 그 전략 몫을 없앤다(= 0)
        prefix = next((p for p in sleeve_sets if key.startswith(p)), None)
        if prefix is None:
            continue
        sid, value = key[len(prefix):], value.strip()
        vid = sid[len(LAB_PREFIX):] if is_lab(sid) else None
        if sid not in ("trend_sma", "mean_reversion", "ai_research") and (vid is None or find(vid) is None):
            raise ValueError(f"알 수 없는 슬리브 {sid}")
        if not value:
            sleeve_sets[prefix].pop(sid, None)
        elif vid is not None and vid not in st["lab"]:
            if D(value) > 0:
                raise ValueError(f"{labels.STRATEGY_LABELS[sid]}: 슬리브를 넣으려면 '추가'도 체크하세요")
        else:
            sleeve_sets[prefix][sid] = value
    for sid, keys in (("trend_sma", ("fast", "slow")), ("mean_reversion", ("bb_period", "bb_k", "rsi_entry", "rsi_exit", "stop_loss_pct", "max_hold_bars"))):
        for k in keys:
            v = form.get(f"p.{sid}.{k}")
            if v:
                data["strategies"][sid]["params"][k] = v
        if f"s.{sid}.present" in form:
            data["strategies"][sid]["enabled"] = form.get(f"s.{sid}.enabled") == "on"
    return Settings.model_validate(data)

