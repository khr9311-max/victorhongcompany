"""상시 실행 런타임.

시작: 프로세스 잠금(호출자) → DB 마이그레이션·무결성 → 미정산 AI 예약 정리 → 미전송/전송중단 주문 복구 →
      계좌 대사(완료 전 신규 주문 금지) → 루프 시작.
정상 종료: 신규 주문 중지 → 진행 중 작업 대기 → 미체결 주문 상태 기록 → 원장 보존.
놓친 봉의 신호는 한꺼번에 실행하지 않는다(가장 최근 봉도 max_signal_age를 넘으면 건너뜀).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timedelta
from typing import Any

from aifund.control import actions
from aifund.control.readiness import reconciler_for, update_broker_health
from aifund.core.ids import new_id
from aifund.core.timeutil import floor_to_interval, kst_str, parse_iso, to_iso
from aifund.data.collector import Snapshot
from aifund.db.database import dumps, loads
from aifund.markets.calendar import session_info
from aifund.service.context import AppContext
from aifund.service.cycle import DecisionCycle

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.cycle = DecisionCycle(ctx)
        self.stop_event = asyncio.Event()
        self.run_id = new_id("run")
        self.status = "starting"
        self.last_cycle: dict[str, dict[str, Any]] = {}
        self.processed: dict[str, str] = {}
        self.cycle_lock = asyncio.Lock()
        self.web_server: Any = None
        self.errors: dict[str, str] = {}

    # ------------------------------------------------------------------ 시작·종료
    async def startup(self) -> None:
        ctx = self.ctx
        integrity = ctx.db.integrity_check()
        if integrity != "ok":
            raise RuntimeError(f"DB 무결성 검사 실패: {integrity} — backups/에서 복구하세요")
        ctx.db.execute("INSERT INTO service_runs(run_id, started_at, pid, host, mode, code_version, settings_version, status) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (self.run_id, to_iso(ctx.clock.now()), os.getpid(), ctx.host, ctx.mode, ctx.code_version,
                        ctx.settings_version, "starting"))
        n = ctx.budget().recover_stale()
        if n:
            log.warning("미정산 AI 예약 %s건을 보수 정산", n)
        ctx.db.execute("UPDATE intents SET status='expired' WHERE status='approved' AND order_id IS NULL")
        for ex in ctx.all_executors():
            out = ex.recover_pending()
            if out["never_sent"] or out["to_unknown"]:
                ctx.incidents.open("crash_recovery", f"{ex.account_id}: 미전송 {out['never_sent']}건 정리, 상태불명 {out['to_unknown']}건 조회 필요",
                                   account_id=ex.account_id)
        for market, mr in ctx.markets.items():
            if mr.collector is not None:
                try:
                    await mr.collector.refresh_instruments(market, ctx.settings.markets[market].instruments)  # type: ignore[index]
                except Exception as exc:
                    self.errors[f"instruments:{market}"] = str(exc)
                    log.warning("상품 정보 갱신 실패(%s): %s", market, exc)
        await self.reconcile_all("startup")
        try:
            await ctx.fx.refresh()
        except Exception as exc:  # pragma: no cover
            log.warning("환율 갱신 실패: %s", exc)
        self.status = "running"
        ctx.notifier.notify("info", "서비스 시작", f"모드 {ctx.mode}, 코드 {ctx.code_version}, 설정 v{ctx.settings_version}")

    async def reconcile_all(self, reason: str) -> None:
        ctx = self.ctx
        for market, mr in ctx.markets.items():
            if mr.operating_executor is None or mr.operating_broker is None:
                continue
            acct = mr.operating_executor.account_id
            try:
                if ctx.mode in ("live", "broker_sandbox"):
                    await update_broker_health(ctx, market)
                rec = reconciler_for(ctx, market)
                if rec is None:
                    continue
                res = await rec.run(reason)
                hard = [m for m in res.mismatches if not m.startswith("상태 불명")]
                if not hard:
                    if not ctx.startup_reconciled.get(acct):
                        log.info("대사 완료(%s): 신규 주문 허용", acct)
                    ctx.startup_reconciled[acct] = True
                else:
                    log.warning("대사 불일치(%s): %s", acct, hard)
            except Exception as exc:
                self.errors[f"reconcile:{market}"] = str(exc)
                log.warning("대사 실패(%s): %s", market, exc)

    def request_stop(self, reason: str = "요청") -> None:
        if not self.stop_event.is_set():
            log.warning("종료 요청: %s", reason)
            self.stop_reason = reason
            self.stop_event.set()

    async def shutdown(self) -> None:
        ctx = self.ctx
        self.status = "stopping"
        for ex in ctx.all_executors():
            ex.stopping = True
        try:
            await asyncio.wait_for(self.cycle_lock.acquire(), timeout=60)
            self.cycle_lock.release()
        except asyncio.TimeoutError:
            log.warning("진행 중 사이클 대기 시간 초과")
        open_orders = sum(len(ex.open_orders()) for ex in ctx.all_executors())
        ctx.db.execute("UPDATE service_runs SET stopped_at=?, status='stopped', stop_reason=? WHERE run_id=?",
                       (to_iso(ctx.clock.now()), getattr(self, "stop_reason", "stop"), self.run_id))
        ctx.flags.event("system", "service_stopped", "service", {"open_orders": open_orders})
        self.status = "stopped"
        self.write_heartbeat()
        for mr in ctx.markets.values():
            try:
                if mr.data is not None:
                    await mr.data.close()
                if mr.operating_broker is not None:
                    await mr.operating_broker.close()
            except Exception:  # pragma: no cover
                pass
        log.info("서비스 정상 종료(미체결 주문 %s건은 거래소에 남아 있을 수 있음, 다음 시작 시 대사)", open_orders)

    # ------------------------------------------------------------------ 하트비트
    def heartbeat_data(self) -> dict[str, Any]:
        ctx = self.ctx
        return {
            "pid": os.getpid(), "mode": ctx.mode, "status": self.status, "run_id": self.run_id, "ts": to_iso(ctx.clock.now()),
            "settings_version": ctx.settings_version, "code_version": ctx.code_version,
            "startup_reconciled": ctx.startup_reconciled, "last_cycle": self.last_cycle, "errors": self.errors,
            "web": f"http://{ctx.settings.web.host}:{ctx.settings.web.port}",
        }

    def write_heartbeat(self) -> None:
        p = self.ctx.paths.heartbeat_path
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.heartbeat_data(), ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, p)

    # ------------------------------------------------------------------ 루프들
    async def _every(self, seconds_fn: Any, fn: Any, name: str) -> None:
        while not self.stop_event.is_set():
            try:
                await fn()
                self.errors.pop(name, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors[name] = f"{exc.__class__.__name__}: {str(exc)[:200]}"
                log.exception("루프 %s 오류", name)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=seconds_fn())
            except asyncio.TimeoutError:
                pass

    async def poll_quotes(self) -> None:
        ctx = self.ctx
        for market, mr in ctx.markets.items():
            if mr.data is None:
                continue
            insts = [i for i in (ctx.market_store.instrument(f"{market}:{s}") for s in ctx.settings.markets[market].instruments) if i]  # type: ignore[index]
            if insts:
                ctx.market_store.save_quotes(await mr.data.quotes(insts))

    async def poll_orders(self) -> None:
        for ex in self.ctx.all_executors():
            await ex.poll()
        for market in self.ctx.markets:
            await actions.liquidation_followup(self.ctx, market)

    async def record_equity(self) -> None:
        for book in self.ctx.book_ids():
            self.cycle.record_equity(book)

    async def settings_watch(self) -> None:
        if self.ctx.reload_settings():
            self.ctx.notifier.notify("info", "설정 변경 적용", f"설정 버전 {self.ctx.settings_version}")

    async def fetch_news(self) -> None:
        ctx = self.ctx
        if ctx.mode == "offline_demo":
            return
        for feed in ctx.settings.news.feeds:
            if feed.enabled and any(ctx.settings.markets[m].enabled for m in feed.markets):  # type: ignore[index]
                await ctx.news.fetch_feed(feed, ctx.settings.news.max_items_per_feed)
        if ctx.settings.news.dart_enabled and ctx.secrets.dart_api_key and ctx.settings.markets["kr_stock"].enabled:
            await ctx.news.fetch_dart(ctx.secrets.dart_api_key, ctx.settings.markets["kr_stock"].instruments)

    async def fx_refresh(self) -> None:
        await self.ctx.fx.refresh()

    async def prune(self) -> None:
        """시세는 72시간, 1분 단위 평가 기록은 7일 보관 후 장부·시간당 1개로 축약(최대낙폭은 이후 시간 단위 근사)."""
        self.ctx.market_store.prune_quotes(72)
        cutoff = to_iso(self.ctx.clock.now() - timedelta(days=7))
        self.ctx.db.execute(
            "DELETE FROM equity_snapshots WHERE ts < ? AND id NOT IN (SELECT MIN(id) FROM equity_snapshots WHERE ts < ? "
            "GROUP BY book_id, substr(ts, 1, 13))", (cutoff, cutoff))

    async def heartbeat(self) -> None:
        self.write_heartbeat()
        stop_file = self.ctx.paths.run_dir / "stop.request"
        if stop_file.exists():
            stop_file.unlink(missing_ok=True)
            self.request_stop("stop.request 파일")

    async def process_commands(self) -> None:
        ctx = self.ctx
        rows = ctx.db.query("SELECT * FROM control_commands WHERE status='queued' ORDER BY id")
        for r in rows:
            ctx.db.execute("UPDATE control_commands SET status='running' WHERE id=?", (r["id"],))
            args = loads(r["args_json"], {})
            try:
                result: Any
                if r["command"] == "stop":
                    self.request_stop(f"명령({r['actor']})")
                    result = "종료 요청"
                elif r["command"] == "cancel_open":
                    result = await actions.cancel_open(ctx, args.get("market"), r["actor"])
                elif r["command"] == "liquidate":
                    result = await actions.liquidate(ctx, args["market"], args["phrase"], r["actor"])
                elif r["command"] == "reconcile":
                    await self.reconcile_all("manual")
                    result = "대사 실행"
                elif r["command"] == "research":
                    result = await self.run_research(args["market"], "manual")
                elif r["command"] == "cycle":
                    result = (await self.run_cycle(args["market"], trigger="manual")).status
                else:
                    result = f"알 수 없는 명령 {r['command']}"
                ctx.db.execute("UPDATE control_commands SET status='done', result_json=?, processed_at=? WHERE id=?",
                               (dumps(result), to_iso(ctx.clock.now()), r["id"]))
            except Exception as exc:
                ctx.db.execute("UPDATE control_commands SET status='failed', result_json=?, processed_at=? WHERE id=?",
                               (dumps(str(exc)), to_iso(ctx.clock.now()), r["id"]))

    # ------------------------------------------------------------------ 의사결정 일정
    def decision_target(self, market: str, now: datetime) -> tuple[str | None, datetime | None, str]:
        """(처리 키, 실행 시각, 설명). 키가 None이면 지금은 대상 없음."""
        ms = self.ctx.settings.markets[market]  # type: ignore[index]
        s = self.ctx.settings
        if ms.candle != "1d" or market == "crypto":
            minutes = ms.candle_minutes or 1440
            close = floor_to_interval(now, minutes)
            run_at = close + timedelta(seconds=ms.decision_delay_sec)
            if now - close > timedelta(seconds=s.risk.max_signal_age_sec):
                return None, close + timedelta(minutes=minutes, seconds=ms.decision_delay_sec), "다음 봉 대기"
            return to_iso(close), run_at, "봉 마감 후"
        sess = session_info(market, now)
        if not sess.is_open or sess.session_open is None:
            return None, sess.next_open, sess.reason
        run_at = sess.session_open + timedelta(minutes=ms.stock_decision_after_open_min)
        if now - run_at > timedelta(hours=2):
            return None, None, "오늘 결정 시간대 지남"
        return sess.session_open.date().isoformat(), run_at, "장 시작 후"

    async def run_cycle(self, market: str, trigger: str = "candle") -> Any:
        async with self.cycle_lock:
            res = await self.cycle.run(market, trigger=trigger)
        self.last_cycle[market] = {"cycle_id": res.cycle_id, "status": res.status, "at": to_iso(self.ctx.clock.now()),
                                   "notes": res.notes[:5], "orders": len(res.orders)}
        return res

    def cycle_last_snapshot(self, market: str) -> Snapshot | None:
        return self.cycle.last_snapshots.get(market)

    async def market_loop(self, market: str) -> None:
        ctx = self.ctx
        while not self.stop_event.is_set():
            now = ctx.clock.now()
            key, run_at, why = self.decision_target(market, now)
            wait = 30.0
            try:
                ex = ctx.markets[market].operating_executor
                gate_ok = ex is None or ctx.startup_reconciled.get(ex.account_id, False)
                if key is not None and run_at is not None and now >= run_at and self.processed.get(market) != key:
                    if not gate_ok:
                        # 대사 전에는 위험 검사에서 운용 장부 주문이 모두 차단된다. 사이클 전에 한 번 더 대사를 시도한다.
                        log.warning("대사 미완료: %s 사이클 전 재대사", market)
                        await self.reconcile_all("retry_before_cycle")
                    res = await self.run_cycle(market)
                    if res.status in ("done", "skipped_stale", "skipped_no_data"):
                        self.processed[market] = key
                    await self.maybe_event_research(market)
                elif run_at is not None:
                    wait = max(1.0, min(30.0, (run_at - now).total_seconds()))
            except Exception as exc:
                self.errors[f"market:{market}"] = str(exc)
                log.exception("시장 루프 오류 %s", market)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ AI
    async def _snapshot_for_ai(self, market: str) -> Snapshot | None:
        snap = self.cycle_last_snapshot(market)
        if snap is not None and self.ctx.clock.now() - snap.created_at < timedelta(hours=2):
            return snap
        mr = self.ctx.markets.get(market)
        if mr is None or mr.collector is None:
            return None
        return await mr.collector.build(market, self.ctx.settings.markets[market], self.ctx.settings.risk)  # type: ignore[index]

    async def run_research(self, market: str, trigger: str) -> str | None:
        snap = await self._snapshot_for_ai(market)
        if snap is None:
            return None
        rid = await self.ctx.ai.research(market, snap, self.cycle.signals_payload(snap), trigger)
        if rid is not None:
            row = self.ctx.db.query_one("SELECT valid FROM ai_reports WHERE report_id=?", (rid,))
            if row and row["valid"]:
                await self.ctx.ai.review(market, rid)
        return rid

    async def maybe_event_research(self, market: str) -> None:
        snap = self.cycle_last_snapshot(market)
        if snap is None:
            return
        why = self.ctx.ai.event_trigger(snap)
        if why and self.ctx.ai.event_calls_today(market) < self.ctx.settings.ai.max_event_calls_per_day:
            asyncio.create_task(self.run_research(market, f"event: {why}"))

    async def ai_tick(self) -> None:
        ctx = self.ctx
        for market, mr in ctx.markets.items():
            if mr.collector is not None and ctx.ai.due_daily(market):
                await self.run_research(market, "daily")
        if ctx.ai.due_weekly():
            from aifund.evaluation.candidates import run_weekly_review

            await run_weekly_review(ctx)

    # ------------------------------------------------------------------ 실행
    async def run(self, *, with_web: bool = True) -> None:
        ctx = self.ctx
        loop = asyncio.get_running_loop()
        for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self.request_stop, f"signal {sig}")
            except (NotImplementedError, RuntimeError):  # Windows
                pass
        await self.startup()
        s = lambda: ctx.settings.schedule  # noqa: E731
        tasks = [
            asyncio.create_task(self._every(lambda: s().heartbeat_sec, self.heartbeat, "heartbeat")),
            asyncio.create_task(self._every(lambda: s().quote_poll_sec, self.poll_quotes, "quotes")),
            asyncio.create_task(self._every(lambda: s().order_poll_sec, self.poll_orders, "orders")),
            asyncio.create_task(self._every(lambda: s().reconcile_interval_sec, lambda: self.reconcile_all("periodic"), "reconcile")),
            asyncio.create_task(self._every(lambda: s().equity_snapshot_sec, self.record_equity, "equity")),
            asyncio.create_task(self._every(lambda: s().fx_poll_min * 60, self.fx_refresh, "fx")),
            asyncio.create_task(self._every(lambda: s().news_poll_min * 60, self.fetch_news, "news")),
            asyncio.create_task(self._every(lambda: 60, self.ai_tick, "ai")),
            asyncio.create_task(self._every(lambda: 2, self.process_commands, "commands")),
            asyncio.create_task(self._every(lambda: 10, self.settings_watch, "settings")),
            asyncio.create_task(self._every(lambda: 3600, self.prune, "prune")),
            asyncio.create_task(ctx.notifier.run()),
        ]
        for market in ctx.markets:
            tasks.append(asyncio.create_task(self.market_loop(market)))
        web_task = None
        if with_web:
            import uvicorn

            from aifund.web.app import create_app

            app = create_app(ctx, self)
            config = uvicorn.Config(app, host=ctx.settings.web.host, port=ctx.settings.web.port, log_level="warning",
                                    access_log=False)
            self.web_server = uvicorn.Server(config)
            self.web_server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
            web_task = asyncio.create_task(self.web_server.serve())
            log.info("대시보드: http://%s:%s", ctx.settings.web.host, ctx.settings.web.port)
        await self.stop_event.wait()
        if self.web_server is not None and web_task is not None:
            # 웹 서버가 스스로 정리(lifespan 종료)하도록 기다린 뒤 나머지 작업을 멈춘다
            self.web_server.should_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(web_task), timeout=15)
            except asyncio.TimeoutError:
                web_task.cancel()
        await self.shutdown()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def read_heartbeat(ctx_paths: Any) -> dict[str, Any] | None:
    try:
        return json.loads(ctx_paths.heartbeat_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def heartbeat_age_text(hb: dict[str, Any] | None, now: datetime) -> str:
    if not hb:
        return "기록 없음"
    ts = parse_iso(hb.get("ts"))
    if ts is None:
        return "알 수 없음"
    return f"{(now - ts).total_seconds():.0f}초 전 ({kst_str(ts)})"
