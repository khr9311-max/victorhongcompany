"""aifund 명령행.

모드 선택: --mode > AIFUND_MODE 환경변수 > internal_paper(기본). 모드마다 DB·로그·토큰 캐시가 분리된다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import secrets as pysecrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aifund.core.lock import LockHeldError, ProcessLock, is_locked, read_lock_info
from aifund.core.paths import MODE_LABELS, MODES, mode_paths, project_root
from aifund.core.secrets import load_dotenv


def _mode(args: argparse.Namespace) -> str:
    m = getattr(args, "mode", None) or os.environ.get("AIFUND_MODE") or "internal_paper"
    if m not in MODES:
        raise SystemExit(f"알 수 없는 모드 {m} (가능: {', '.join(MODES)})")
    return m


def _config_path(mode: str = "internal_paper") -> Path | None:
    if mode in ("internal_paper", "offline_demo"):
        paper = project_root() / "config" / "paper.toml"
        if paper.exists():
            return paper
    p = project_root() / "config" / "config.toml"
    return p if p.exists() else None


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _ctx(mode: str, **kw: Any):  # type: ignore[no-untyped-def]
    from aifund.core.logging_setup import setup_logging
    from aifund.service.context import build_context

    paths = mode_paths(mode).ensure()
    setup_logging(paths.log_dir, os.environ.get("AIFUND_LOG_LEVEL", "INFO"), console=kw.pop("console", True))
    return build_context(paths, config_path=_config_path(mode), **kw)


def _service_running(mode: str) -> bool:
    return is_locked(mode_paths(mode).lock_path)


def _enqueue(mode: str, command: str, args: dict[str, Any], actor: str = "cli") -> int:
    from aifund.core.timeutil import to_iso, utcnow
    from aifund.db.database import Database, dumps

    db = Database(mode_paths(mode).db_path)
    cur = db.execute("INSERT INTO control_commands(created_at, actor, command, args_json, status) VALUES (?,?,?,?,?)",
                     (to_iso(utcnow()), actor, command, dumps(args), "queued"))
    return int(cur.lastrowid)


def _wait_command(mode: str, cid: int, timeout: float = 120) -> Any:
    from aifund.db.database import Database, loads

    db = Database(mode_paths(mode).db_path)
    end = time.time() + timeout
    while time.time() < end:
        r = db.query_one("SELECT status, result_json FROM control_commands WHERE id=?", (cid,))
        if r and r["status"] in ("done", "failed"):
            return {"status": r["status"], "result": loads(r["result_json"])}
        time.sleep(1)
    return {"status": "timeout", "result": "서비스가 명령을 처리하지 않았습니다(실행 중인지 확인)"}


async def _inline(mode: str, fn):  # type: ignore[no-untyped-def]
    """서비스가 꺼져 있을 때: 잠금을 잡고(단일 실행기 보장) 대사 후 작업을 실행한다."""
    from aifund.service.runtime import Runtime

    with ProcessLock(mode_paths(mode).lock_path, purpose="cli-inline"):
        ctx = _ctx(mode)
        rt = Runtime(ctx)
        for ex in ctx.all_executors():
            ex.recover_pending()
        await rt.reconcile_all("cli_inline")
        return await fn(ctx, rt)


# ---------------------------------------------------------------------- setup / doctor
def cmd_setup(args: argparse.Namespace) -> int:
    root = project_root()
    mode = _mode(args)
    env = root / ".env"
    example = root / ".env.example"
    if not env.exists():
        shutil.copy(example, env)
        print(f".env 생성(예제 복사): {env}")
    text = env.read_text(encoding="utf-8")
    if "AIFUND_ADMIN_TOKEN=" not in text or any(
        line.strip() == "AIFUND_ADMIN_TOKEN=" for line in text.splitlines()
    ):
        token = pysecrets.token_urlsafe(32)
        lines = [ln for ln in text.splitlines() if not ln.startswith("AIFUND_ADMIN_TOKEN=")]
        lines.append(f"AIFUND_ADMIN_TOKEN={token}")
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("관리자 토큰을 .env에 생성했습니다(AIFUND_ADMIN_TOKEN). 대시보드 로그인에 사용하세요.")
    try:
        os.chmod(env, 0o600)
    except OSError:
        pass
    cfg = root / "config" / "config.toml"
    if not cfg.exists():
        shutil.copy(root / "config" / "config.example.toml", cfg)
        print(f"설정 파일 생성: {cfg}")
    load_dotenv(env)
    ctx = _ctx(mode, console=False)
    print(f"모드 {mode}({MODE_LABELS[mode]}) DB 초기화: {ctx.paths.db_path} (설정 v{ctx.settings_version})")
    from aifund.control.selftest import run_selftest

    res = asyncio.run(run_selftest(ctx.db, ctx.code_version))
    for r in res:
        print(("  ✔ " if r.ok else "  ✘ ") + f"{r.name}: {r.detail}")
    print("\n다음 단계: `aifund doctor` → `aifund run` (대시보드 http://127.0.0.1:%d)" % ctx.settings.web.port)
    return 0 if all(r.ok for r in res) else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    mode = _mode(args)
    root = project_root()
    rows: list[tuple[str, str, str]] = []

    def add(level: str, name: str, detail: str) -> None:
        rows.append((level, name, detail))

    v = sys.version_info
    add("ok" if (v.major, v.minor) == (3, 12) else "fail", "Python", platform.python_version())
    add("ok" if sys.prefix != sys.base_prefix else "warn", "가상환경", sys.prefix)
    sysname = platform.system()
    add("ok" if sysname == "Darwin" else "warn", "운영체제",
        f"{sysname} {platform.machine()}" + ("" if sysname == "Darwin" else " (launchd 등 맥 전용 기능은 이 환경에서 미검증)"))
    for mod in ("fastapi", "uvicorn", "httpx", "pydantic", "jinja2", "jwt", "anthropic", "exchange_calendars", "defusedxml"):
        try:
            __import__(mod)
            add("ok", f"모듈 {mod}", "설치됨")
        except ImportError as exc:
            add("fail", f"모듈 {mod}", str(exc))
    env = root / ".env"
    if env.exists():
        mode_bits = oct(env.stat().st_mode & 0o777)
        add("ok" if sysname == "Windows" or env.stat().st_mode & 0o077 == 0 else "warn", ".env", f"존재 (권한 {mode_bits})")
    else:
        add("warn", ".env", "없음 → `aifund setup`")
    paths = mode_paths(mode)
    if paths.db_path.exists():
        conn = sqlite3.connect(str(paths.db_path))
        add("ok" if conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok" else "fail", "DB 무결성", str(paths.db_path))
        add("ok", "DB 저널", conn.execute("PRAGMA journal_mode").fetchone()[0])
        conn.close()
    else:
        add("warn", "DB", "아직 없음 → `aifund setup`")
    free = shutil.disk_usage(root).free / 1e9
    add("ok" if free > 2 else "warn", "디스크 여유", f"{free:.1f} GB")
    running = _service_running(mode)
    add("ok", "서비스", f"실행 중 {read_lock_info(paths.lock_path)}" if running else "실행 중 아님")
    try:
        hb = json.loads(paths.heartbeat_path.read_text(encoding="utf-8"))
        add("ok", "하트비트", f"{hb.get('status')} @ {hb.get('ts')}")
    except (OSError, ValueError):
        add("warn", "하트비트", "기록 없음")
    try:
        ctx = _ctx(mode, console=False)
        for k, val in ctx.secrets.describe().items():
            add("ok" if val == "설정됨" else "info", f"자격증명 {k}", val)
        ok, why = ctx.ai.availability()
        add("ok" if ok else "warn", "AI", why)
        for m, mr in ctx.markets.items():
            add("ok" if mr.data else "warn", f"{m} 데이터", mr.data_reason)
            add("ok" if mr.operating_broker else "warn", f"{m} 주문 경로", mr.broker_reason)
        if mode != "offline_demo":
            async def net() -> list[tuple[str, str, str]]:
                out = []
                from aifund.brokers.upbit import UpbitMarketData

                md = UpbitMarketData()
                try:
                    insts = await md.instruments("crypto", ["KRW-BTC"])
                    await md.quotes(insts)
                    skew = md.http.last_clock_skew
                    out.append(("ok" if skew is not None and abs(skew) < ctx.settings.risk.max_clock_skew_sec else "warn",
                                "업비트 공개 API·시계 오차", f"{skew:+.2f}초" if skew is not None else "측정 불가"))
                except Exception as exc:
                    out.append(("fail", "업비트 공개 API", str(exc)[:150]))
                finally:
                    await md.close()
                fx = await ctx.fx.refresh()
                out.append(("ok" if fx else "warn", "환율(Frankfurter)", f"{fx.rate} 기준 {fx.as_of}" if fx else "실패"))
                return out

            rows.extend(asyncio.run(net()))
    except Exception as exc:
        add("fail", "컨텍스트", repr(exc)[:200])
    if sysname == "Darwin":
        label = "com.victorhong.aifund"
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True)
        add("ok" if r.returncode == 0 else "info", "launchd", "등록됨" if r.returncode == 0 else "미등록(scripts/macos/install_launchd.sh)")
        r = subprocess.run(["pmset", "-g"], capture_output=True, text=True)
        sleep_line = next((ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("sleep")), "확인 불가")
        add("info", "시스템 잠자기(pmset, 읽기 전용)", sleep_line + " — 설정은 변경하지 않습니다(README 참고)")
    logs = paths.log_dir
    size = sum(p.stat().st_size for p in logs.glob("*")) / 1e6 if logs.exists() else 0
    add("ok" if size < 200 else "warn", "로그 크기", f"{size:.1f} MB")
    backups = sorted(paths.backup_dir.glob("*.sqlite3")) if paths.backup_dir.exists() else []
    add("ok" if backups else "warn", "백업", backups[-1].name if backups else "없음 → `aifund backup`")
    icon = {"ok": "✔", "warn": "!", "fail": "✘", "info": "·"}
    for level, name, detail in rows:
        print(f" {icon[level]} {name}: {detail}")
    return 1 if any(r[0] == "fail" for r in rows) else 0


# ---------------------------------------------------------------------- run / status / stop
def cmd_run(args: argparse.Namespace) -> int:
    mode = _mode(args)
    paths = mode_paths(mode).ensure()
    try:
        lock = ProcessLock(paths.lock_path, purpose="service").acquire()
    except LockHeldError as exc:
        print(str(exc))
        return 2
    try:
        replay = None
        if args.replay:
            from aifund.data.replay import ReplayMarketData
            from aifund.core.timeutil import SystemClock

            replay = ReplayMarketData.from_json(Path(args.replay), SystemClock())
        ctx = _ctx(mode, replay=replay)
        web = ctx.settings.web
        if web.host not in ("127.0.0.1", "localhost", "::1"):
            tok = ctx.secrets.admin_token or ""
            if os.environ.get("AIFUND_ALLOW_REMOTE") != "1" or len(tok) < 32:
                print("원격 바인딩 거부: AIFUND_ALLOW_REMOTE=1 과 32자 이상의 AIFUND_ADMIN_TOKEN이 필요합니다(docs/operations-macos.md)")
                return 2
        if mode == "live":
            print("주의: live 모드입니다. LIVE를 활성화한 시장만 실제 주문합니다(활성화는 `aifund live enable`).")
        from aifund.service.runtime import Runtime

        rt = Runtime(ctx)
        asyncio.run(rt.run(with_web=not args.no_web))
        return 0
    finally:
        lock.release()


def cmd_status(args: argparse.Namespace) -> int:
    mode = _mode(args)
    paths = mode_paths(mode)
    out: dict[str, Any] = {"mode": mode, "running": _service_running(mode), "lock": read_lock_info(paths.lock_path)}
    try:
        out["heartbeat"] = json.loads(paths.heartbeat_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        out["heartbeat"] = None
    if paths.db_path.exists():
        from aifund.db.database import Database

        db = Database(paths.db_path)
        eq = db.query_one("SELECT ts, equity_krw, cash_krw, positions_krw, reserved_krw FROM equity_snapshots WHERE book_id='operating' "
                          "ORDER BY id DESC LIMIT 1")
        out["operating_equity"] = dict(eq) if eq else None
        out["flags"] = [dict(r) for r in db.query("SELECT key, reason, updated_at FROM control_flags")]
        out["open_orders"] = db.scalar("SELECT COUNT(*) FROM orders WHERE status IN ('pending','submitted','partially_filled','cancel_pending','unknown')")
        out["unknown_orders"] = db.scalar("SELECT COUNT(*) FROM orders WHERE status='unknown'")
        out["live"] = [dict(r) for r in db.query("SELECT market, account_id, activated_at FROM live_activations WHERE deactivated_at IS NULL")]
        out["last_cycles"] = [dict(r) for r in db.query("SELECT market, status, started_at FROM cycles ORDER BY started_at DESC LIMIT 5")]
        out["open_incidents"] = db.scalar("SELECT COUNT(*) FROM incidents WHERE resolved_at IS NULL")
    _print(out)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    mode = _mode(args)
    paths = mode_paths(mode)
    if not _service_running(mode):
        print("실행 중인 서비스가 없습니다.")
        return 0
    (paths.run_dir / "stop.request").write_text("stop", encoding="utf-8")
    info = read_lock_info(paths.lock_path) or {}
    pid = info.get("pid")
    if pid and os.name != "nt":
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
    print("정상 종료 요청(신규 주문 중지 → 진행 중 작업 대기 → 상태 기록).", end=" ", flush=True)
    end = time.time() + args.timeout
    while time.time() < end:
        if not _service_running(mode):
            print("종료됨.")
            return 0
        time.sleep(1)
    print("시간 내 종료되지 않았습니다. `aifund status`로 확인하세요.")
    return 1


# ---------------------------------------------------------------------- backup / restore
def cmd_backup(args: argparse.Namespace) -> int:
    from aifund.db.database import Database

    mode = _mode(args)
    paths = mode_paths(mode).ensure()
    if not paths.db_path.exists():
        print("DB가 없습니다.")
        return 1
    dest = paths.backup_dir / f"aifund-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite3"
    Database(paths.db_path).backup_to(dest)
    chk = sqlite3.connect(str(dest)).execute("PRAGMA integrity_check").fetchone()[0]
    if chk != "ok":
        print(f"백업 무결성 실패: {chk}")
        return 1
    files = sorted(paths.backup_dir.glob("aifund-*.sqlite3"))
    for old in files[: max(0, len(files) - args.keep)]:
        old.unlink()
    print(f"백업 완료: {dest} (보관 {min(len(files), args.keep)}개)")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    mode = _mode(args)
    paths = mode_paths(mode).ensure()
    src = Path(args.file)
    if _service_running(mode):
        print("서비스 실행 중에는 복구할 수 없습니다. `aifund stop` 후 다시 실행하세요.")
        return 2
    if not args.yes:
        print("복구는 현재 DB를 대체합니다. 확인하려면 --yes 를 붙이세요.")
        return 2
    if sqlite3.connect(str(src)).execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        print("백업 파일 무결성 실패")
        return 1
    with ProcessLock(paths.lock_path, purpose="restore"):
        if paths.db_path.exists():
            keep = paths.db_path.with_name(f"aifund.pre-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite3")
            s = sqlite3.connect(str(paths.db_path))
            d = sqlite3.connect(str(keep))
            s.backup(d)
            s.close()
            d.close()
            print(f"현재 DB 보존: {keep}")
        s = sqlite3.connect(str(src))
        d = sqlite3.connect(str(paths.db_path))
        s.backup(d)
        s.close()
        d.close()
    print("복구 완료. 다음 `aifund run` 시작 시 거래소 대사가 끝나기 전까지 신규 주문이 차단됩니다.")
    return 0


# ---------------------------------------------------------------------- 검증·데모
def cmd_selftest(args: argparse.Namespace) -> int:
    from aifund.control.selftest import run_selftest

    ctx = _ctx(_mode(args), console=False)
    res = asyncio.run(run_selftest(ctx.db, ctx.code_version))
    for r in res:
        print(("✔ " if r.ok else "✘ ") + f"{r.name}: {r.detail}")
    return 0 if all(r.ok for r in res) else 1


def cmd_demo(args: argparse.Namespace) -> int:
    from aifund.core.logging_setup import setup_logging
    from aifund.core.timeutil import UTC, ManualClock
    from aifund.service.context import build_context
    from aifund.service.simulate import simulate

    paths = mode_paths("offline_demo").ensure()
    if _service_running("offline_demo"):
        print("offline_demo 서비스가 실행 중입니다. 먼저 `aifund stop --mode offline_demo`.")
        return 2
    setup_logging(paths.log_dir, "WARNING")
    with ProcessLock(paths.lock_path, purpose="demo"):
        start = datetime.now(UTC) - timedelta(hours=args.hours + 2)
        last = None
        if paths.db_path.exists():
            from aifund.db.database import Database

            last = Database(paths.db_path).scalar("SELECT MAX(started_at) FROM cycles")
        if last:
            from aifund.core.timeutil import parse_iso

            start = max(start, parse_iso(last) + timedelta(minutes=1))  # type: ignore[operator]
        ctx = build_context(paths, clock=ManualClock(start), config_path=_config_path("offline_demo"))
        summary = asyncio.run(simulate(ctx, args.hours))
    print(f"[데모·가짜 데이터] {args.hours}시간 시뮬레이션: 사이클 {summary['cycles']}, 주문 {summary['orders']}, AI 연구 {summary['research']}")
    if summary["notes"]:
        print("원장 검증 문제:", summary["notes"])
        return 1
    print("대시보드: `aifund run --mode offline_demo` 후 http://127.0.0.1:8765")
    return 0


# ---------------------------------------------------------------------- 제어
def cmd_halt(args: argparse.Namespace) -> int:
    from aifund.control import actions

    ctx = _ctx(_mode(args), console=False)
    print(actions.halt(ctx, args.scope, args.reason or "CLI", "cli"))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    from aifund.control import actions

    ctx = _ctx(_mode(args), console=False)
    print(actions.resume(ctx, args.scope, "cli", args.reason or ""))
    return 0


def cmd_cancel_open(args: argparse.Namespace) -> int:
    mode = _mode(args)
    if _service_running(mode):
        _print(_wait_command(mode, _enqueue(mode, "cancel_open", {"market": args.market})))
        return 0
    from aifund.control import actions

    res = asyncio.run(_inline(mode, lambda ctx, rt: actions.cancel_open(ctx, args.market, "cli")))
    _print(res)
    return 0


def cmd_liquidate(args: argparse.Namespace) -> int:
    from aifund.control import actions

    mode = _mode(args)
    ctx = _ctx(mode, console=False)
    pv = actions.preview_liquidation(ctx, args.market)
    print(f"[청산 미리보기] {args.market}")
    for r in pv.rows:
        print(f"  {r['instrument_id']} 수량 {r['qty']} 매수1호가 {r['bid']} 예상 {r['est_proceeds']} {r['note']}")
    for w in pv.warnings:
        print("  - " + w)
    if not args.confirm:
        print(f"\n실행하려면: aifund liquidate {args.market} --confirm \"{pv.confirm_phrase}\"")
        return 0
    if _service_running(mode):
        _print(_wait_command(mode, _enqueue(mode, "liquidate", {"market": args.market, "phrase": args.confirm})))
        return 0
    res = asyncio.run(_inline(mode, lambda c, rt: actions.liquidate(c, args.market, args.confirm, "cli")))
    _print(res)
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    mode = _mode(args)
    if _service_running(mode):
        _print(_wait_command(mode, _enqueue(mode, "reconcile", {})))
        return 0

    async def go(ctx, rt):  # type: ignore[no-untyped-def]
        await rt.reconcile_all("cli")
        return [dict(r) for r in ctx.db.query("SELECT account_id, ts, ok, mismatches_json FROM reconciliations ORDER BY id DESC LIMIT 5")]

    _print(asyncio.run(_inline(mode, go)))
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    from aifund.control.live import confirm_phrase
    from aifund.control.readiness import disable_live, enable_live, live_readiness, reconciler_for

    mode = _mode(args)
    ctx = _ctx(mode, console=False)
    if args.live_cmd == "status":
        for m, ms in ctx.settings.markets.items():
            act = ctx.activations.active(m)
            ok, why = ctx.activations.authorized(mode, m, ms.account_id, None, ctx.settings)
            print(f"{m}: {'활성' if act else '비활성'} ({why}) 계좌 {ms.account_id}")
        return 0
    market = args.market
    ms = ctx.settings.markets[market]  # type: ignore[index]
    print(f"[LIVE 범위] 시장 {market} / 계좌 {ms.account_id} / 브로커 {ms.broker} / 허용 종목 {', '.join(ms.instruments)} / "
          f"원금 한도 {ctx.settings.risk.principal_cap_krw}원 / 총노출 {ctx.settings.risk.gross_exposure_cap_krw}원 / "
          f"1회 {ctx.settings.risk.max_order_notional_krw}원 / 일손실 {ctx.settings.risk.daily_loss_stop_krw}원 / "
          f"최대낙폭 {ctx.settings.risk.max_drawdown_stop_pct}%")
    if args.live_cmd == "check":
        items = asyncio.run(live_readiness(ctx, market, ack_no_withdraw=args.ack_no_withdraw))
        for i in items:
            print(("✔ " if i.ok else ("✘ " if i.blocking else "! ")) + f"{i.name}: {i.detail}")
        return 0 if all(i.ok for i in items if i.blocking) else 1
    if args.live_cmd == "enable":
        if not args.confirm:
            print(f"활성화하려면 정확히: aifund live enable {market} --ack-no-withdraw --confirm \"{confirm_phrase(market, ms.account_id)}\"")
            return 2
        ok, items, msg = asyncio.run(enable_live(ctx, market, args.confirm, "cli", ack_no_withdraw=args.ack_no_withdraw))
        for i in items:
            print(("✔ " if i.ok else ("✘ " if i.blocking else "! ")) + f"{i.name}: {i.detail}")
        print(msg)
        return 0 if ok else 1
    if args.live_cmd == "disable":
        print(disable_live(ctx, market, "cli", args.reason or "CLI"))
        return 0
    if args.live_cmd == "baseline":
        if args.confirm != f"기준 재설정 {ms.account_id}":
            print(f"현재 거래소 보유분 중 봇 장부에 없는 수량을 '기존 보유분'으로 다시 기록합니다. 확인: --confirm \"기준 재설정 {ms.account_id}\"")
            return 2
        rec = reconciler_for(ctx, market)
        if rec is None:
            print("계좌 연결이 없습니다")
            return 1
        _print(asyncio.run(rec.capture_baseline("cli", "사용자 기준 재설정")))
        return 0
    return 2


# ---------------------------------------------------------------------- 설정·평가
def cmd_settings(args: argparse.Namespace) -> int:
    from aifund.config.settings import load_settings_file, file_hash

    ctx = _ctx(_mode(args), console=False)
    if args.settings_cmd == "show":
        print(ctx.settings.model_dump_json(indent=2))
    elif args.settings_cmd == "history":
        _print(ctx.store_settings.history(50))
    elif args.settings_cmd == "import":
        p = Path(args.file)
        v = ctx.store_settings.save(load_settings_file(p), "cli", f"파일 가져오기 {p.name}", source_file_hash=file_hash(p))
        print(f"설정 버전 {v} 저장(실행 중인 서비스는 10초 내 적용)")
    elif args.settings_cmd == "set":
        data = ctx.settings.model_dump(mode="json")
        for kv in args.pairs:
            k, _, val = kv.partition("=")
            cur = data
            parts = k.split(".")
            for p in parts[:-1]:
                cur = cur[p]
            old = cur[parts[-1]]
            if isinstance(old, bool):
                cur[parts[-1]] = val.lower() in ("1", "true", "yes", "on")
            elif isinstance(old, int):
                cur[parts[-1]] = int(val)
            elif isinstance(old, list):
                cur[parts[-1]] = [x.strip() for x in val.split(",") if x.strip()]
            else:
                cur[parts[-1]] = val
        from aifund.config.settings import Settings

        v = ctx.store_settings.save(Settings.model_validate(data), "cli", args.reason or "CLI 설정 변경")
        print(f"설정 버전 {v} 저장")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from aifund.evaluation.metrics import comparison

    ctx = _ctx(_mode(args), console=False)
    comp = comparison(ctx)
    print(f"{'장부':<14}{'설정':<9}{'평가자산':>12}{'손익(비용차감)':>14}{'수수료':>9}{'AI비용':>9}{'AI포함손익':>12}{'MDD%':>8}{'체결':>6}  데이터")
    for m in comp["books"]:
        print(f"{m.book_id:<14}{m.setting:<9}{m.equity:>12,.0f}{m.pnl_net:>14,.0f}{m.fees:>9,.0f}{m.ai_cost:>9,.0f}{m.pnl_after_ai:>12,.0f}"
              f"{(m.max_drawdown_pct or 0):>8.2f}{m.trades:>6}  {'충분' if m.sufficient else f'부족({m.days:.1f}일)'}")
    for n in comp["notes"]:
        print(" - " + n)
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from aifund.core.timeutil import UTC
    from aifund.evaluation.backtest import run_backtest, side_fee_rate
    from aifund.strategies.base import build_strategies

    mode = _mode(args)
    ctx = _ctx(mode, console=False)
    market = args.market
    ms = ctx.settings.markets[market]  # type: ignore[index]
    if args.fetch:
        from aifund.service.backfill import backfill

        n = asyncio.run(backfill(ctx, market, args.days))
        print(f"캔들 {n}개 수집·저장")
    candles, insts = {}, {}
    for sym in ms.instruments:
        iid = f"{market}:{sym}"
        inst = ctx.market_store.instrument(iid)
        cs = ctx.market_store.closed_candles(iid, ms.candle, ctx.clock.now(), 10000)
        if inst and cs:
            candles[iid], insts[iid] = cs, inst
    if not candles:
        print("저장된 캔들이 없습니다. --fetch 를 붙이세요.")
        return 1
    # 미국주식 캔들은 달러이므로 자본도 달러로 환산한다(원화 금액을 달러로 착각하면 1주 단위 반올림이 달라짐).
    ccy = next(iter(insts.values())).quote_ccy
    usdkrw = None
    if ccy != "KRW":
        fx = ctx.fx.status(ctx.settings.risk.max_fx_age_hours)
        if not fx.fresh:
            asyncio.run(ctx.fx.refresh())
            fx = ctx.fx.status(ctx.settings.risk.max_fx_age_hours)
        if not fx.fresh or fx.rate is None:
            print(f"환율이 없어 {ccy} 자본을 계산할 수 없습니다: {fx.reason}")
            return 1
        usdkrw = fx.rate.rate
    fee_rate = side_fee_rate(ctx.settings.execution.paper, market)

    def fee_text(v: Any) -> str:
        return f"{v:,.0f}원" if ccy == "KRW" else f"{v:,.2f} {ccy}"

    split = datetime.fromisoformat(args.split).replace(tzinfo=UTC) if args.split else None
    for st in build_strategies(ctx.settings.strategies.model_dump(include={"trend_sma", "mean_reversion"})):
        cap_krw = ms.allocation_krw * ctx.settings.strategies.sleeves_for(market).get(st.strategy_id, 0)
        cap = cap_krw / usdkrw if usdkrw else cap_krw
        res = run_backtest(st, candles, insts, capital=cap, fee_rate=fee_rate,
                           slippage_bps=ctx.settings.execution.paper.slippage_bps, split_at=split)
        fx_note = f" (≈ {cap:,.2f} {ccy}, 환율 {usdkrw})" if usdkrw else ""
        print(f"\n[{st.title}] 자본 {cap_krw:,.0f}원{fx_note}, 편도 비용률 {fee_rate * 100:.3f}%")
        for seg in ("dev", "eval", "total"):
            r = res.get(seg)
            if r:
                rp = f"{r['return_pct']:.2f}%" if r["return_pct"] is not None else "데이터 부족"
                bh = f"{r['buy_hold_return_pct']:.2f}%" if r["buy_hold_return_pct"] is not None else "-"
                mdd = f"{r['max_drawdown_pct']:.2f}%" if r["max_drawdown_pct"] is not None else "-"
                print(f"  {({'dev': '개발 구간', 'eval': '평가 구간', 'total': '전체'})[seg]}: 수익률 {rp}, 최대낙폭 {mdd}, "
                      f"거래 {r['trades']}회, 수수료 {fee_text(r['fees'])}, 매수보유 {bh}, 봉 {r['bars']}")
        print("  " + res["note"])
    return 0


def cmd_candidates(args: argparse.Namespace) -> int:
    from aifund.evaluation import candidates as cand

    ctx = _ctx(_mode(args), console=False)
    if args.cand_cmd == "list":
        _print([dict(r) for r in ctx.db.query("SELECT candidate_id, created_at, source, strategy_id, params_json, status FROM strategy_candidates ORDER BY created_at DESC")])
    elif args.cand_cmd == "add":
        changes = {k: v for k, _, v in (kv.partition("=") for kv in args.pairs)}
        print(cand.create_candidate(ctx, args.strategy, changes, args.reason or "사용자 후보", "user"))
    elif args.cand_cmd == "backtest":
        _print(cand.backtest_candidate(ctx, args.id))
    elif args.cand_cmd == "promote":
        print(f"설정 버전 {cand.promote(ctx, args.id, 'cli')}")
    elif args.cand_cmd == "rollback":
        print(f"설정 버전 {cand.rollback(ctx, args.id, 'cli')}")
    return 0


def cmd_ai(args: argparse.Namespace) -> int:
    mode = _mode(args)
    ctx = _ctx(mode, console=False)
    if args.ai_cmd == "status":
        u = ctx.budget().month_usage()
        ok, why = ctx.ai.availability()
        print(f"AI {why} · 공급자 {ctx.settings.ai.provider} · 모델 {ctx.settings.ai.model} · 이번 달 {u.settled_krw:,.0f}원 사용 + {u.reserved_krw:,.0f}원 예약 / {u.cap_krw:,.0f}원")
        return 0
    if args.ai_cmd == "research":
        if _service_running(mode):
            _print(_wait_command(mode, _enqueue(mode, "research", {"market": args.market}), timeout=600))
        else:
            print(asyncio.run(_inline(mode, lambda c, rt: rt.run_research(args.market, "manual"))))
    return 0


def cmd_orders(args: argparse.Namespace) -> int:
    ctx = _ctx(_mode(args), console=False)
    rows = ctx.db.query("SELECT created_at, book_id, instrument_id, side, limit_price, qty, filled_qty, status FROM orders "
                        + ("WHERE book_id=? " if args.book else "") + "ORDER BY created_at DESC LIMIT ?",
                        ((args.book, args.limit) if args.book else (args.limit,)))
    for r in rows:
        print(" ".join(str(r[k]) for k in r.keys()))
    return 0


# ---------------------------------------------------------------------- 연결 점검
def cmd_kiwoom_check(args: argparse.Namespace) -> int:
    """키움 조회 전용 연결 점검(현재가·일봉, 선택 시 국내 계좌). 주문 API는 허용 목록에 없다."""
    from aifund.brokers.base import BrokerError
    from aifund.brokers.kiwoom import KiwoomMarketData, KiwoomReadClient, KiwoomUSMarketData
    from aifund.core.secrets import load_mode_secrets

    creds = load_mode_secrets(_mode(args)).kiwoom_data
    if creds is None:
        print("키움 조회 키 미설정 또는 offline_demo 모드")
        return 1
    us = ":" in args.symbol
    if us and args.account:
        print("미국주식 계좌 조회는 아직 미지원입니다. --account 없이 시세를 점검하세요.")
        return 1

    async def check():
        client = KiwoomReadClient(creds)
        try:
            data = KiwoomUSMarketData(client) if us else KiwoomMarketData(client)
            instruments = await data.instruments("us_stock" if us else "kr_stock", [args.symbol])
            quotes = await data.quotes(instruments)
            candles = await data.candles(instruments[0], "1d", 5)
            result = {"environment": creds.env, "read_only": True, "symbol": args.symbol,
                      "last": quotes[0].last, "daily_candles": len(candles)}
            if args.account:
                result["account"] = await client.account_summary()
            _print(result)
        finally:
            await client.close()
    try:
        asyncio.run(check())
    except BrokerError as exc:
        print(str(exc))
        return 1
    return 0


LAB_MIN_BARS = 120  # 지표·구간이 자리 잡을 최소 봉 수


def _lab_source(mode: str, market: str) -> tuple[Any, str, bool]:
    """연구소 수집 원천(조회 전용). 장부 DB·설정을 건드리지 않도록 운용 구성 없이 직접 만든다."""
    if mode == "offline_demo":
        from aifund.data.demo import DemoMarketData

        return DemoMarketData(), "DEMO(가짜 데이터)", True
    if market == "crypto":
        from aifund.brokers.upbit import UpbitMarketData

        return UpbitMarketData(), "upbit_public", False
    from aifund.core.secrets import load_mode_secrets

    secrets = load_mode_secrets(mode)
    if secrets.kiwoom_data is not None:
        from aifund.brokers.kiwoom import KiwoomMarketData, KiwoomReadClient, KiwoomUSMarketData

        client = KiwoomReadClient(secrets.kiwoom_data)
        cls = KiwoomUSMarketData if market == "us_stock" else KiwoomMarketData
        return cls(client), f"kiwoom({secrets.kiwoom_data.env})", False
    if secrets.kis_data is not None:
        from aifund.brokers.kis import KisClient, KisMarketData

        client = KisClient(secrets.kis_data, token_cache_dir=mode_paths(mode).ensure().secrets_dir)
        return KisMarketData(client), "kis(최근 약 100봉만 제공)", False
    raise SystemExit("주식 시세 키가 없습니다: .env에 KIWOOM_DATA_*(권장, 긴 과거 일봉) 또는 KIS_DATA_*를 설정하세요.")


def cmd_lab(args: argparse.Namespace) -> int:
    from aifund.lab import data as labdata
    from aifund.lab.report import catalog_text

    if args.lab_cmd == "catalog":
        print(catalog_text(args.market))
        return 0
    if args.lab_cmd == "judge":
        from aifund.lab import judge as labjudge

        sa, sb = labjudge.load(Path(args.report_a)), labjudge.load(Path(args.report_b))
        print(labjudge.render(sa, sb, labjudge.judge(sa, sb), top=args.top))
        return 0
    mode = _mode(args)
    market = args.market
    interval = args.candle or ("240m" if market == "crypto" else "1d")
    if interval == "1w" and args.lab_cmd == "fetch":
        raise SystemExit("주봉은 받은 일봉으로 만듭니다: `lab fetch --candle 1d` 뒤 `lab run --candle 1w`")
    if market != "crypto" and interval not in ("1d", "1w"):
        raise SystemExit("주식은 일봉(1d)·주봉(1w)만 지원합니다")
    if interval not in ("1d", "1w") and not (interval.endswith("m") and interval[:-1] in ("15", "30", "60", "240")):
        raise SystemExit("--candle은 1d·1w 또는 15m/30m/60m/240m 이어야 합니다")
    paths = mode_paths(mode).ensure()
    store = labdata.LabStore(paths.data_dir / "lab")
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    if args.lab_cmd == "fetch":
        return _lab_fetch(args, mode, store, market, interval, symbols)
    return _lab_run(args, mode, store, market, interval, symbols)


def _lab_fetch(args: argparse.Namespace, mode: str, store: Any, market: str, interval: str, symbols: list[str] | None) -> int:
    from aifund.lab import data as labdata

    days = args.days or (1095 if market == "crypto" else 3650)
    source, label, demo = _lab_source(mode, market)

    async def go() -> dict[str, int]:
        syms = symbols
        try:
            if syms is None:
                if market != "crypto":
                    syms = labdata.default_symbols(market)[: args.top or None]
                elif demo:
                    syms = ["KRW-BTC", "KRW-ETH", "KRW-XRP"][: args.top or None]
                else:
                    syms = await labdata.upbit_universe(args.top or 30)
            bars = labdata.bars_for_days(market, interval, days)
            print(f"{label}에서 {len(syms)}종목 × 최대 {bars:,}봉({days}일) 수집 → {store.root}", flush=True)
            return await labdata.fetch(store, source, market, interval, syms, bars, source_label=label, demo=demo,
                                       progress=lambda m: print(m, flush=True))
        finally:
            await source.close()

    res = asyncio.run(go())
    ok = sum(1 for n in res.values() if n > 0)
    print(f"완료: {ok}/{len(res)}종목 저장" + (" (일부 실패: 위 로그 확인)" if ok < len(res) else ""))
    return 0 if ok else 1


def _lab_run(args: argparse.Namespace, mode: str, store: Any, market: str, interval: str, symbols: list[str] | None) -> int:
    from aifund.config.settings import PaperSettings, load_settings_file
    from aifund.core.timeutil import UTC
    from aifund.evaluation.backtest import side_fee_rate
    from aifund.lab.bars import weekly
    from aifund.lab.catalog import variants
    from aifund.lab.engine import MODELS
    from aifund.lab.exits import Costs
    from aifund.lab.report import render, save, summarize
    from aifund.lab.runner import run_lab

    source_interval = "1d" if interval == "1w" else interval  # 주봉은 저장된 일봉을 묶어 만든다
    syms = symbols or store.symbols(market, source_interval)
    if not syms:
        print(f"저장된 {market} {source_interval} 데이터가 없습니다. 먼저 `aifund lab fetch --market {market}`를 실행하세요.")
        return 1
    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC) if args.since else None
    bars_list, short = [], []
    for s in syms:
        b = store.load(market, source_interval, s, since=since)
        if b is not None and interval == "1w":
            b = weekly(b)
        if b is None or len(b) < LAB_MIN_BARS:
            short.append(s)
        else:
            bars_list.append(b)
    if short:
        print(f"제외(데이터 없음 또는 {LAB_MIN_BARS}봉 미만): {', '.join(short)}")
    if not bars_list:
        return 1
    cfg = _config_path(mode)
    paper = load_settings_file(cfg).execution.paper if cfg else PaperSettings()
    costs = Costs(float(side_fee_rate(paper, market)), float(paper.slippage_bps) / 10000)
    split = datetime.fromisoformat(args.split).replace(tzinfo=UTC) if args.split else None
    vs = variants(market, args.entries.split(",") if args.entries else None, args.exits.split(",") if args.exits else None,
                  args.families.split(",") if args.families else None,
                  args.interpretations.split(",") if args.interpretations else None)
    models = list(MODELS) if args.model == "both" else [args.model]
    started = time.time()
    print(f"{len(bars_list)}종목 · 변형 {len(vs)}개 시험 중(체결 가정: {', '.join(models)})…", flush=True)
    run = run_lab(bars_list, market, vs, costs, models=models, split=split, progress=lambda m: print(m, flush=True))
    summary = summarize(run)
    demo = bool(store.read_meta(market, source_interval).get("demo", mode == "offline_demo"))
    print()
    print(render(summary, demo))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = save(summary, store.root / "reports" / f"{stamp}-{market}-{interval}", demo)
    print(f"\n보고서: {out / 'report.md'} (거래 내역 trades.csv, 요약 summary.json) · {time.time() - started:.0f}초")
    return 0


# ---------------------------------------------------------------------- 파서
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aifund", description="빅터홍컴퍼니 AI 투자회사")
    p.add_argument("--mode", choices=MODES, help="운용 모드(기본 internal_paper 또는 AIFUND_MODE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn: Any, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--mode", choices=MODES, default=argparse.SUPPRESS)
        sp.set_defaults(fn=fn)
        return sp

    add("setup", cmd_setup, "초기 설정(.env·관리자 토큰·DB·자체검증)")
    add("doctor", cmd_doctor, "환경 점검")
    sp = add("kiwoom-check", cmd_kiwoom_check, "키움 현재가·일봉 조회 점검(주문 없음)")
    sp.add_argument("--symbol", default="005930")
    sp.add_argument("--account", action="store_true", help="예수금·보유종목도 조회")
    sp = add("run", cmd_run, "서비스 실행(대시보드 포함)")
    sp.add_argument("--no-web", action="store_true")
    sp.add_argument("--replay", help="명시적 재생 데이터 JSON")
    add("status", cmd_status, "상태")
    sp = add("stop", cmd_stop, "정상 종료")
    sp.add_argument("--timeout", type=int, default=90)
    sp = add("backup", cmd_backup, "DB 백업")
    sp.add_argument("--keep", type=int, default=14)
    sp = add("restore", cmd_restore, "DB 복구(서비스 정지 상태에서)")
    sp.add_argument("file")
    sp.add_argument("--yes", action="store_true")
    add("selftest", cmd_selftest, "실행무결성 자체검증")
    sp = add("demo", cmd_demo, "오프라인 데모 시뮬레이션")
    sp.add_argument("--hours", type=int, default=72)
    sp = add("halt", cmd_halt, "신규 매수 중지")
    sp.add_argument("scope", help="all 또는 crypto/kr_stock/us_stock")
    sp.add_argument("--reason")
    sp = add("resume", cmd_resume, "신규 매수 재개")
    sp.add_argument("scope")
    sp.add_argument("--reason")
    sp = add("cancel-open", cmd_cancel_open, "봇 미체결 주문 취소")
    sp.add_argument("--market")
    sp = add("liquidate", cmd_liquidate, "보유분 청산(미리보기/확인)")
    sp.add_argument("market")
    sp.add_argument("--confirm")
    add("reconcile", cmd_reconcile, "계좌 대사")
    sp = add("live", cmd_live, "LIVE 실거래 관리")
    lsub = sp.add_subparsers(dest="live_cmd", required=True)
    lsub.add_parser("status")
    for n in ("check", "enable", "disable", "baseline"):
        x = lsub.add_parser(n)
        x.add_argument("market")
        x.add_argument("--confirm")
        x.add_argument("--ack-no-withdraw", action="store_true")
        x.add_argument("--reason")
    sp = add("settings", cmd_settings, "설정 조회·변경")
    ssub = sp.add_subparsers(dest="settings_cmd", required=True)
    ssub.add_parser("show")
    ssub.add_parser("history")
    x = ssub.add_parser("import")
    x.add_argument("file")
    x = ssub.add_parser("set")
    x.add_argument("pairs", nargs="+", help="예: risk.max_order_notional_krw=80000")
    x.add_argument("--reason")
    add("report", cmd_report, "A/B/C 비교 보고")
    sp = add("backtest", cmd_backtest, "규칙 전략 백테스트(개발/평가 구간)")
    sp.add_argument("--market", default="crypto")
    sp.add_argument("--days", type=int, default=60)
    sp.add_argument("--split", help="개발/평가 구간 경계 YYYY-MM-DD")
    sp.add_argument("--fetch", action="store_true")
    sp = add("lab", cmd_lab, "전략 연구소: 진입 패턴 × 청산 방식 변형을 과거 데이터로 검증(운용과 분리)")
    labsub = sp.add_subparsers(dest="lab_cmd", required=True)
    markets = ["kr_stock", "us_stock", "crypto"]
    x = labsub.add_parser("judge", help="두 시장 보고서를 사전 등록 기준으로 함께 판정")
    x.add_argument("report_a", help="보고서 폴더(summary.json이 있는 곳)")
    x.add_argument("report_b")
    x.add_argument("--top", type=int, default=15)
    x = labsub.add_parser("catalog", help="변형 목록과 규칙")
    x.add_argument("--market", choices=markets)
    x = labsub.add_parser("fetch", help="과거 캔들 수집(조회 전용)")
    x.add_argument("--market", choices=markets, default="kr_stock")
    x.add_argument("--candle", help="코인: 1d/240m/60m…(기본 240m), 주식: 1d")
    x.add_argument("--days", type=int, help="수집 기간(달력 일수, 기본 주식 3650·코인 1095)")
    x.add_argument("--top", type=int, help="기본 종목 중 앞에서 N개(코인은 거래대금 상위 N, 기본 30)")
    x.add_argument("--symbols", help="쉼표로 구분한 종목(기본 목록 대신)")
    x = labsub.add_parser("run", help="저장된 데이터로 변형 전체 시험·보고서 저장")
    x.add_argument("--market", choices=markets, default="kr_stock")
    x.add_argument("--candle", help="1d(기본·주식)·1w(저장된 일봉을 주봉으로)·240m 등")
    x.add_argument("--families", help="계열만 골라 시험(쉼표: pattern,trend,rotation)")
    x.add_argument("--interpretations", help="책 패턴 해석(쉼표: v1,loose,strict,regime — docs/lab-preregistration.md)")
    x.add_argument("--symbols")
    x.add_argument("--since", help="이 날짜(YYYY-MM-DD) 이후 데이터만")
    x.add_argument("--split", help="개발/평가 구간 경계 YYYY-MM-DD(기본: 기간의 70% 지점)")
    x.add_argument("--model", choices=["both", "bar_close", "intrabar"], default="both",
                   help="체결 가정: 현 시스템 방식(bar_close)·책 방식(intrabar)")
    x.add_argument("--entries", help="진입 패턴만 골라 시험(쉼표, `aifund lab catalog` 참고)")
    x.add_argument("--exits", help="청산 방식만 골라 시험(쉼표)")
    sp = add("candidates", cmd_candidates, "전략 개선 후보")
    csub = sp.add_subparsers(dest="cand_cmd", required=True)
    csub.add_parser("list")
    x = csub.add_parser("add")
    x.add_argument("strategy")
    x.add_argument("pairs", nargs="+")
    x.add_argument("--reason")
    for n in ("backtest", "promote", "rollback"):
        csub.add_parser(n).add_argument("id")
    sp = add("ai", cmd_ai, "AI 상태·수동 연구")
    asub = sp.add_subparsers(dest="ai_cmd", required=True)
    asub.add_parser("status")
    asub.add_parser("research").add_argument("market")
    sp = add("orders", cmd_orders, "주문 목록")
    sp.add_argument("--book")
    sp.add_argument("--limit", type=int, default=30)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv(project_root() / ".env")
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
