"""검증 8: LIVE 활성화 조건이 누락되면 실제 주문 어댑터에 도달하지 않는가. 모드별 데이터가 격리되는가."""

import asyncio
import os

import pytest

from aifund.brokers.fake import FakeBroker
from aifund.config.settings import Settings
from aifund.control.live import LiveActivations, LiveGuardedBroker, LiveNotAuthorized, confirm_phrase
from aifund.core.paths import mode_paths
from aifund.core.secrets import load_mode_secrets
from aifund.db.database import Database
from aifund.service.context import build_context
from helpers import make_env


def _guarded(env, mode="live"):
    inner = FakeBroker("upbit-main", live_money=True)
    s = env.settings
    s.markets["crypto"].instruments = ["KRW-TEST"]
    g = LiveGuardedBroker(inner, "crypto", mode, LiveActivations(env.db, env.clock), lambda: s)
    env.executor.broker = g
    env.executor.account_id = "upbit-main"
    return inner, g


def test_no_activation_never_reaches_adapter(tmp_path):
    env = make_env(tmp_path)
    inner, g = _guarded(env)
    oid, st = asyncio.run(env.executor.execute(env.intent("2")))
    assert st == "rejected" and inner.submit_calls == 0
    assert "LIVE 미활성" in env.executor.order(oid)["last_error"]


def test_activation_scope_change_blocks_again(tmp_path):
    env = make_env(tmp_path)
    inner, g = _guarded(env)
    acts = LiveActivations(env.db, env.clock)
    with pytest.raises(LiveNotAuthorized):
        acts.activate("crypto", env.settings, 1, "test", "LIVE crypto wrong")
    acts.activate("crypto", env.settings, 1, "test", confirm_phrase("crypto", "upbit-main"))
    oid, st = asyncio.run(env.executor.execute(env.intent("2")))
    assert inner.submit_calls == 1 and st == "submitted"
    env.settings.risk.max_order_notional_krw = env.settings.risk.max_order_notional_krw - 1  # 범위 변경
    oid2, st2 = asyncio.run(env.executor.execute(env.intent("2")))
    assert st2 == "rejected" and inner.submit_calls == 1
    assert "재확인" in env.executor.order(oid2)["last_error"]


def test_non_live_mode_guard_blocks(tmp_path):
    env = make_env(tmp_path)
    inner, g = _guarded(env, mode="internal_paper")
    LiveActivations(env.db, env.clock).activate("crypto", env.settings, 1, "t", confirm_phrase("crypto", "upbit-main"))
    _, st = asyncio.run(env.executor.execute(env.intent("2")))
    assert st == "rejected" and inner.submit_calls == 0


def test_keys_alone_do_not_enable_live(home):
    os.environ["UPBIT_LIVE_ACCESS_KEY"] = "a" * 20
    os.environ["UPBIT_LIVE_SECRET_KEY"] = "b" * 20

    def fake_factory(market, ms):
        return FakeBroker(ms.account_id, live_money=True)

    ctx = build_context(mode_paths("live", home).ensure(), live_broker_factory=fake_factory)
    mr = ctx.markets["crypto"]
    assert isinstance(mr.operating_broker, LiveGuardedBroker)
    assert ctx.activations.active("crypto") is None
    ok, why = ctx.activations.authorized("live", "crypto", "upbit-main", "crypto:KRW-BTC", ctx.settings)
    assert not ok and "미활성" in why


def test_mode_secret_isolation():
    os.environ["UPBIT_LIVE_ACCESS_KEY"] = "a" * 20
    os.environ["UPBIT_LIVE_SECRET_KEY"] = "b" * 20
    os.environ["KIS_SANDBOX_APP_KEY"] = "c" * 20
    os.environ["KIS_SANDBOX_APP_SECRET"] = "d" * 20
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    assert load_mode_secrets("internal_paper").upbit is None
    assert load_mode_secrets("internal_paper").kis_trade is None
    assert load_mode_secrets("broker_sandbox").upbit is None
    assert load_mode_secrets("broker_sandbox").kis_trade.env == "demo"
    assert load_mode_secrets("live").kis_trade is None  # 모의 키는 live에서 읽히지 않음
    assert load_mode_secrets("live").upbit is not None
    assert load_mode_secrets("offline_demo").anthropic_api_key is None  # 데모는 유료 AI를 쓰지 않음


def test_mode_databases_are_separate(home):
    a = build_context(mode_paths("offline_demo", home).ensure())
    b = build_context(mode_paths("internal_paper", home).ensure())
    assert a.paths.db_path != b.paths.db_path
    a.ledger.create_book("x", kind="shadow", setting="A", principal_krw=__import__("decimal").Decimal(1), virtual=True,
                         account_id="p", description="")
    assert b.ledger.book("x") is None
    # 다른 모드 DB를 잘못 열면 거부
    db = Database(mode_paths("internal_paper", home).db_path)
    db.set_meta("mode", "live")
    with pytest.raises(RuntimeError, match="모드 불일치"):
        build_context(mode_paths("internal_paper", home))
    _ = Settings
