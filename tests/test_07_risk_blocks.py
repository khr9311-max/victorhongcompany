"""검증 7: 일손실·낙폭 한도·시세 지연·환율 지연·인증 오류·AI 장애에서 신규 위험 증가가 차단되는가(매도는 유지)."""

from datetime import timedelta
from decimal import Decimal

from aifund.core.money import D
from aifund.domain.models import Quote, Side
from aifund.ledger.valuation import BookValuation, EquityTracker, RiskStateView
from aifund.risk.engine import RiskInputs, evaluate
from helpers import inst, make_env


def _inputs(env, **kw):
    q = kw.pop("quote", None) or env.quote()
    base = dict(mode="internal_paper", market="crypto", book_kind="operating", account_id="acct", instrument=env.inst,
                side=Side.BUY, qty=D(2), limit_price=D(10000), notional_krw=D(20000), risk_increasing=True, quote=q,
                now=env.clock.now(), sellable_qty=D(10))
    base.update(kw)
    return RiskInputs(**base)


def _blocked(env, reason_part, **kw):
    d = evaluate(_inputs(env, **kw), env.settings.risk)
    assert not d.approved, d.checks
    assert any(reason_part in r for r in d.reasons), d.reasons
    # 같은 조건에서 보유분 매도(위험 감소)는 허용
    sell = evaluate(_inputs(env, **{**kw, "side": Side.SELL, "risk_increasing": False}), env.settings.risk)
    return sell


def test_baseline_approved(tmp_path):
    env = make_env(tmp_path)
    assert evaluate(_inputs(env), env.settings.risk).approved


def test_daily_loss_and_drawdown(tmp_path):
    env = make_env(tmp_path)
    rs = RiskStateView("2026-03-02", D(300000), D(300000), D(-31000), D(10), True, False, "x", None)
    assert _blocked(env, "일손실", risk_state=rs).approved
    rs2 = RiskStateView("2026-03-02", D(300000), D(400000), D(0), D(31), False, True, None, "x")
    assert _blocked(env, "최대 낙폭", risk_state=rs2).approved


def test_equity_tracker_triggers_and_persists(tmp_path):
    env = make_env(tmp_path)
    tr = EquityTracker(env.db, env.clock)

    def v(eq):
        return BookValuation(book_id="b", cash={"KRW": eq}, cash_krw=eq, positions_krw=D(0), reserved_krw=D(0),
                             reserved_buy_krw=D(0), equity_krw=eq, realized_krw=D(0), unrealized_krw=D(0), fees_krw=D(0),
                             exposure_krw=D(0))

    tr.record(v(D(300000)), D(0), env.settings.risk)
    s = tr.record(v(D(268000)), D(0), env.settings.risk)  # -32,000원
    assert s.daily_stop_active
    s = tr.record(v(D(200000)), D(0), env.settings.risk)  # 낙폭 33%
    assert s.drawdown_stop_active
    # 재시작(새 추적기)해도 유지
    assert EquityTracker(env.db, env.clock).state("b").drawdown_stop_active
    # 다음 날: 일손실 정지는 풀리고 낙폭 정지는 유지
    env.clock.advance(hours=24)
    s = tr.record(v(D(200000)), D(0), env.settings.risk)
    assert not s.daily_stop_active and s.drawdown_stop_active


def test_stale_quote(tmp_path):
    env = make_env(tmp_path)
    q = env.quote()
    env.clock.advance(120)
    d = evaluate(_inputs(env, quote=q, now=env.clock.now()), env.settings.risk)
    assert any("호가 신선도" in r for r in d.reasons)


def test_stale_fx_for_foreign(tmp_path):
    env = make_env(tmp_path)
    us = inst("AAPL", market="us_stock", ccy="USD", tick_policy="us", step=D(1), min_notional=D("0.01"))
    q = Quote(us.instrument_id, D("199.99"), D("200"), D(10), D(10), None, None, None, env.clock.now(), "t")
    assert _blocked(env, "환율", instrument=us, quote=q, limit_price=D("200"), qty=D(1), fx_ok=False,
                    fx_reason="환율 기준시각이 120시간 지남").approved


def test_auth_error_and_recon_block(tmp_path):
    env = make_env(tmp_path)
    d = evaluate(_inputs(env, broker_auth_ok=False), env.settings.risk)
    assert any("계좌 인증" in r for r in d.reasons)
    d = evaluate(_inputs(env, account_blocks=["auth_error:acct: 401"]), env.settings.risk)
    assert not d.approved
    d = evaluate(_inputs(env, startup_reconciled=False), env.settings.risk)
    assert any("대사" in r for r in d.reasons)


def test_ai_unavailable_hold(tmp_path):
    env = make_env(tmp_path)
    assert _blocked(env, "AI", ai_hold_reason="AI 사용 불가 + 설정: 신규 위험 보류").approved


def test_unknown_order_blocks_risk_increase(tmp_path):
    env = make_env(tmp_path)
    assert _blocked(env, "상태불명", unknown_orders_account=1).approved


def test_halt_and_session_and_data_quality(tmp_path):
    env = make_env(tmp_path)
    assert _blocked(env, "신규 매수 중지", halted_reason="신규 매수 중지(전체): 테스트").approved
    assert _blocked(env, "장 운영", session_open=False, session_reason="휴장(공휴일)").approved
    assert _blocked(env, "데이터 품질", snapshot_issues=["최근 완성봉 누락/지연(90분)"]).approved


def test_max_order_and_positions(tmp_path):
    env = make_env(tmp_path)
    d = evaluate(_inputs(env, qty=D(11), notional_krw=D(110000)), env.settings.risk)
    assert any("1회 주문 한도" in r for r in d.reasons)
    d = evaluate(_inputs(env, held_instruments={"crypto:A", "crypto:B", "crypto:C"}), env.settings.risk)
    assert any("최대 보유 종목" in r for r in d.reasons)


def test_sell_limited_to_bot_qty(tmp_path):
    env = make_env(tmp_path)
    d = evaluate(_inputs(env, side=Side.SELL, risk_increasing=False, qty=D(5), sellable_qty=D(3)), env.settings.risk)
    assert any("봇 보유분" in r for r in d.reasons)


def test_ai_failures_hold_new_proposals(tmp_path):
    """AI 결과 검증 실패(미지원 종목·근거 없는 숫자)는 제안을 만들지 않는다."""
    from aifund.ai import schemas as S

    r = S.ResearchReport.model_validate({
        "market_summary": "요약", "data_status": "정상",
        "instrument_views": [{"instrument_id": "crypto:KRW-DOGE", "stance": "favorable", "summary": "3% 상승",
                              "claims": [{"text": "24시간 12% 상승", "source_ids": []}], "data_gaps": []}],
        "strategy_conditions": [],
        "proposals": [{"proposal_ref": "P1", "instrument_id": "crypto:KRW-DOGE", "action": "buy", "target_weight": 0.5,
                       "rationale": "강세 10%", "source_ids": ["px:fake"], "counterarguments": [], "invalidation": "하락",
                       "horizon_hours": 24, "cost_considered": "수수료"}],
        "improvement_ideas": [],
    })
    errs = S.validate_research(r, {"px:real"}, {"crypto:KRW-BTC"})
    assert any("미지원 종목" in e for e in errs)
    assert any("근거 없는 숫자" in e for e in errs)
    assert any("번들에 없는 source_id" in e for e in errs)
    _ = Decimal
    _ = timedelta
