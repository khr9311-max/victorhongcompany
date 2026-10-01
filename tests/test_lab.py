"""전략 연구소: 책 패턴 규칙, 미래정보 차단, 체결 가정, 보고서·명령."""

from __future__ import annotations

import json
import math
import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from aifund.domain.models import Candle
from aifund.lab.bars import Bars, bars_per_year, prior_max, regimes, sma_series, weekly
from aifund.lab.catalog import variants
from aifund.lab.data import LabStore, bars_for_days
from aifund.lab.engine import MODELS, simulate
from aifund.lab.exits import Costs
from aifund.lab.judge import judge
from aifund.lab.judge import render as render_judge
from aifund.lab.report import render, save, summarize
from aifund.lab.rotation import ROTATIONS, simulate_rotation
from aifund.lab.runner import run_lab
from aifund.lab.setups import ENTRIES, INTERPRETATIONS, Context, Rules, Setup, entries_for, scan
from aifund.lab.zones import close_pivots

T0 = datetime(2020, 1, 1, tzinfo=timezone.utc)
FREE = Costs(0.0, 0.0)


def make(rows: list[tuple[float, float, float, float]], iid: str = "kr_stock:TEST") -> Bars:
    """(시가, 고가, 저가, 종가) 목록 → 일봉."""
    n = len(rows)
    return Bars(iid, "1d", [T0 + timedelta(days=i) for i in range(n)], [T0 + timedelta(days=i, hours=6) for i in range(n)],
                [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], [r[3] for r in rows])


def from_closes(closes: list[float], wick: float = 0.5) -> list[tuple[float, float, float, float]]:
    rows, prev = [], closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + wick, min(prev, c) - wick, c))
        prev = c
    return rows


def line(a: float, b: float, n: int) -> list[float]:
    """a에서 b까지 n칸(시작 제외, 끝 포함)."""
    return [a + (b - a) * k / n for k in range(1, n + 1)]


def box(second_low: float = 100.0) -> list[float]:
    """100 지지·110 저항을 두 번씩 터치(번호 10·30이 저점, 20·40이 고점)."""
    return [120.0] + line(120, 100, 10) + line(100, 110, 10) + line(110, second_low, 10) + line(second_low, 110, 10)


def at_support(last: tuple[float, float, float, float]) -> Bars:
    """박스 뒤 103 근처까지 내려온 다음(마지막 번호 48) 49번 봉에 시험할 캔들을 놓는다."""
    rows = from_closes(box() + line(110, 102.5, 8))
    return make(rows + [last])


def walk(n: int, seed: int) -> list[tuple[float, float, float, float]]:
    r = random.Random(seed)
    rows, c = [], 100.0
    for _ in range(n):
        o = c * math.exp(r.gauss(0, 0.004))
        c = o * math.exp(r.gauss(0, 0.02))
        rows.append((o, max(o, c) * math.exp(abs(r.gauss(0, 0.012))), min(o, c) * math.exp(-abs(r.gauss(0, 0.012))), c))
    return rows


# ----------------------------------------------------------------------------- 지표·구간


def test_pivots_need_k_bars_on_both_sides_and_zones_use_confirmed_only():
    x = Context(make(from_closes(box() + line(110, 102.5, 8))))
    lows = [p.i for p in close_pivots(x.b.c, 5) if not p.high]
    highs = [p.i for p in close_pivots(x.b.c, 5) if p.high]
    assert lows[:2] == [10, 30] and highs[:2] == [20, 40]
    centers = [round(z.center) for z in x.zones.at(48)]
    assert 100 in centers and 110 in centers
    z100 = next(z for z in x.zones.at(48) if round(z.center) == 100)
    assert z100.touches == 2 and z100.lo < 100 < z100.hi
    # 번호 40 고점은 45번 봉이 끝나야 확정된다
    assert not [p for p in x.zones.pivots_between(0, 48, 44) if p.i == 40]
    assert [p for p in x.zones.pivots_between(0, 48, 45) if p.i == 40]


def test_regime_efficiency_ratio():
    up = [100 + i for i in range(40)]
    chop = [100 + (i % 2) for i in range(40)]
    assert regimes(up)[-1] == "up" and regimes(list(reversed(up)))[-1] == "down" and regimes(chop)[-1] == "range"
    assert regimes(up)[29] is None


# ----------------------------------------------------------------------------- 진입 패턴(책 규칙)


def test_kangaroo_tail_on_support():
    x = Context(at_support((102.6, 103.2, 97.0, 103.0)))  # 꼬리가 100 구간을 찌르고 위쪽 1/3에서 마감
    found = scan(x, ENTRIES["kangaroo_tail"])
    assert list(found) == [49]
    s = found[49]
    assert s.trigger == pytest.approx(103.2 + x.buf(49)) and s.stop == pytest.approx(97.0 - x.buf(49))
    assert s.targets and 109 < s.targets[0] < 110  # 위쪽 110 구간 하단이 목표
    # 시가·종가가 위쪽 1/3 밖이면 캥거루 꼬리가 아니다
    assert not scan(Context(at_support((102.6, 103.2, 97.0, 100.0))), ENTRIES["kangaroo_tail"])
    # 구간에 닿지 않으면(꼬리가 짧아 100 구간 위) 아니다
    assert not scan(Context(at_support((102.6, 103.2, 101.2, 103.0))), ENTRIES["kangaroo_tail"])


def test_big_shadow_and_big_belt():
    shadow = Context(at_support((102.4, 104.2, 98.5, 104.0)))  # 직전 봉을 감싼 큰 양봉, 종가는 고가 근처
    assert list(scan(shadow, ENTRIES["big_shadow"])) == [49]
    assert not scan(shadow, ENTRIES["kangaroo_tail"])  # 종가가 직전 봉 범위 밖 → 캥거루 꼬리는 아님
    belt = Context(at_support((100.2, 103.4, 99.8, 103.2)))  # 갭 하락 출발·저가에서 시작해 고가에서 마감
    assert list(scan(belt, ENTRIES["big_belt"])) == [49]
    assert "big_belt" not in [e.kind for e in entries_for("crypto")]  # 24시간 코인은 갭이 없어 제외
    assert "big_belt" in [e.kind for e in entries_for("kr_stock")]


def test_wammie_higher_second_touch():
    closes = box(second_low=99.5) + line(110, 100.8, 8)
    x = Context(make(from_closes(closes) + [(100.9, 102.4, 100.6, 102.2)]))
    found = scan(x, ENTRIES["wammie"])
    assert 49 in found
    assert found[49].stop == pytest.approx(99.0 - x.buf(49))  # 손절은 첫 터치(더 낮은 저점) 아래
    # 두 번째 저점이 첫 저점보다 낮으면 와미가 아니다
    lower = Context(make(from_closes(box(second_low=99.5) + line(110, 99.2, 8)) + [(99.3, 101.0, 98.6, 100.8)]))
    assert 49 not in scan(lower, ENTRIES["wammie"])


def test_last_kiss_after_breakout():
    closes = box() + line(110, 106, 5) + line(106, 109, 4) + [113.0, 115.0, 112.0]
    x = Context(make(from_closes(closes) + [(111.2, 114.5, 110.6, 114.2)]))
    found = scan(x, ENTRIES["last_kiss"])
    assert list(found) == [53]
    s = found[53]
    assert s.box_top == pytest.approx(110) and s.stop == pytest.approx(105)  # 손절 = 박스 중간
    # 키스 봉이 박스 안에서 마감하면(가짜 돌파) 아니다
    fail = Context(make(from_closes(closes) + [(111.2, 112.0, 108.0, 109.5)]))
    assert not scan(fail, ENTRIES["last_kiss"])


def test_trendy_kangaroo_in_uptrend_pause():
    rows = from_closes([100.0] + line(100, 140, 40))
    rows += [(140.0, 140.6, 139.6, 140.3), (140.3, 140.7, 139.8, 140.1), (140.1, 140.5, 139.7, 140.4),
             (140.4, 140.8, 139.9, 140.2), (140.2, 140.6, 139.8, 140.3)]
    x = Context(make(rows + [(140.3, 140.7, 137.5, 140.6)]))
    assert list(scan(x, ENTRIES["trendy_kangaroo"])) == [46]
    # 추세 없이(횡보) 같은 모양이면 아니다
    flat = from_closes([140.0 + (i % 2) * 0.5 for i in range(41)]) + rows[41:]
    assert not scan(Context(make(flat + [(140.3, 140.7, 137.5, 140.6)])), ENTRIES["trendy_kangaroo"])


@pytest.mark.parametrize("seed", [1, 2])
def test_no_lookahead_setups_and_trades(seed):
    """잘라낸 데이터로 다시 돌려도 자른 시점 전의 패턴·거래가 같아야 한다(미래정보 없음)."""
    rows = walk(700, seed)
    full = Context(make(rows))
    found = {k: scan(full, spec) for k, spec in ENTRIES.items()}
    assert sum(len(v) for k, v in found.items() if k != "random") >= 5
    costs = Costs(0.001, 0.0005)
    for cut in (350, 520):
        part = Context(make(rows[:cut]))
        limit = part.b.open_time[cut - 1]
        for kind, spec in ENTRIES.items():
            assert scan(part, spec) == {t: s for t, s in found[kind].items() if t < cut}, kind
        for v in variants("kr_stock", families=["pattern", "trend"]):
            assert v.entry is not None and v.exit is not None
            for model in v.models:
                a = simulate(full, found[v.entry.kind], v.exit.kind, model, costs, variant=v.id).trades
                b = simulate(part, scan(part, v.entry), v.exit.kind, model, costs, variant=v.id).trades
                assert [t for t in a if t.exit_time < limit] == [t for t in b if t.exit_time < limit], (v.id, model)


# ----------------------------------------------------------------------------- 체결 가정·청산


def bars_after(setup_rows: list[tuple[float, float, float, float]]) -> Context:
    warm = [(100.0, 100.5, 99.5, 100.0)] * 21  # 0~20번(20번이 패턴 봉)
    return Context(make(warm + setup_rows + [(118.0, 118.5, 117.5, 118.0)] * 3))


def test_entry_and_target_by_model():
    x = bars_after([(100, 106, 99, 105.5), (106, 112, 104, 110), (110, 121, 109, 118), (119, 119.5, 117, 118)])
    setups = {20: Setup("t", 20, 105.0, 95.0, (120.0,))}
    intra = simulate(x, setups, "zone", "intrabar", FREE, variant="v").trades
    assert len(intra) == 1 and intra[0].entry_px == 105 and intra[0].exit_px == pytest.approx(120)  # 장중 역지정가·지정가
    assert intra[0].ret == pytest.approx(120 / 105 - 1) and intra[0].reason == "목표 구간"
    close = simulate(x, setups, "zone", "bar_close", FREE, variant="v").trades
    # 21번 종가가 진입가 위 → 22번 시가 106 매수, 23번 고가가 목표에 닿음 → 24번 시가 119 매도
    assert close[0].entry_px == 106 and close[0].exit_px == pytest.approx(119)
    assert close[0].ret == pytest.approx(119 / 106 - 1)
    fee = Costs(0.001, 0.0005)
    paid = simulate(x, setups, "zone", "intrabar", fee, variant="v").trades[0]
    assert paid.ret == pytest.approx(120 * 0.9995 * 0.999 / (105 * 1.0005 * 1.001) - 1)


def test_intrabar_stop_wins_when_both_hit_and_gap_fills_at_open():
    x = bars_after([(100, 106, 99, 105.5), (106, 121, 94, 100)])
    t = simulate(x, {20: Setup("t", 20, 105.0, 95.0, (120.0,))}, "zone", "intrabar", FREE, variant="v").trades[0]
    assert t.exit_px == pytest.approx(95) and t.reason == "손절"
    gap = bars_after([(100, 106, 99, 105.5), (90, 92, 89, 91)])
    t = simulate(gap, {20: Setup("t", 20, 105.0, 95.0, (120.0,))}, "zone", "intrabar", FREE, variant="v").trades[0]
    assert t.exit_px == pytest.approx(90)  # 갭 하락이면 손절가가 아니라 시가
    # 진입 전에 손절가를 깨면 진입하지 않는다
    miss = bars_after([(100, 101, 94, 96), (96, 106, 95.5, 105)])
    assert not simulate(miss, {20: Setup("t", 20, 105.0, 95.0, ())}, "zone", "intrabar", FREE, variant="v").trades


def test_split_exit_moves_stop_to_breakeven():
    x = bars_after([(100, 106, 99, 105.5), (106, 116, 104, 112), (111, 112, 104, 106)])
    t = simulate(x, {20: Setup("t", 20, 105.0, 95.0, (115.0, 125.0))}, "split", "intrabar", FREE, variant="v").trades[0]
    assert t.ret == pytest.approx(0.5 * 115 / 105 + 0.5 - 1) and t.reason == "1차 목표+손절"


def test_three_bar_trailing_stop_only_rises():
    x = bars_after([(100, 106, 99, 105.5), (105.5, 108, 104, 107.5), (107.5, 110, 106, 109.5), (109.5, 112, 108, 111),
                    (111, 111.5, 103, 104)])
    t = simulate(x, {20: Setup("t", 20, 105.0, 95.0, ())}, "three_bar", "intrabar", FREE, variant="v").trades[0]
    assert t.exit_px == pytest.approx(104 - x.buf(24))  # 24번 종가 뒤 최근 3봉 최저(104) 아래
    assert t.ret == pytest.approx((104 - x.buf(24)) / 105 - 1)


def test_ladder_uses_zones_known_at_each_bar():
    rows = from_closes(box() + line(110, 102.5, 8)) + [(102.6, 103.2, 97.0, 103.0)]  # 49번 캥거루 꼬리
    rows += [(103.0, 105.0, 102.8, 104.8), (104.8, 108.0, 104.5, 107.8), (107.8, 110.6, 107.5, 109.0),
             (109.0, 109.2, 102.0, 102.5), (102.5, 103.0, 101.0, 102.0)]
    x = Context(make(rows))
    setups = scan(x, ENTRIES["kangaroo_tail"])
    t = simulate(x, setups, "ladder", "intrabar", FREE, variant="v").trades[0]
    # 52번 봉이 110 구간에 닿음 → 손절을 본전으로 → 53번 봉에서 본전 청산
    assert t.exit_px == pytest.approx(t.entry_px) and t.ret == pytest.approx(0) and t.reason == "손절"
    three = simulate(x, setups, "three_bar", "intrabar", FREE, variant="v").trades[0]
    assert three.exit_px == pytest.approx(102.8 - x.buf(52))  # 3봉 추적: 52번 종가 뒤 50~52번 최저(102.8) 아래


def test_last_kiss_exits_when_close_back_inside_box():
    x = bars_after([(100, 106, 99, 105.5), (106, 107, 103.5, 103.8)])
    t = simulate(x, {20: Setup("t", 20, 105.0, 95.0, (), box_top=104.0)}, "zone", "intrabar", FREE, variant="v").trades[0]
    assert t.reason == "박스 안으로 마감" and t.exit_px == pytest.approx(103.8)


def test_equity_matches_trades_and_report_files(tmp_path):
    a, b = make(walk(500, 3), "kr_stock:AAA"), make(walk(450, 4), "kr_stock:BBB")
    vs = variants("kr_stock")
    run = run_lab([a], "kr_stock", vs, Costs(0.001, 0.0005))
    for v in vs:
        if v.rotation is not None:
            continue
        for m in v.models:  # 슬리브 전액 매매라 거래 수익률의 곱 = 최종 자산
            prod = math.prod(1 + t.ret for t in run.trades[(v.id, m)])
            assert run.curves[(v.id, m)][-1] == pytest.approx(prod, rel=1e-9), (v.id, m)
    run2 = run_lab([a, b], "kr_stock", vs, Costs(0.001, 0.0005))
    s = summarize(run2)
    combos = sum(len(v.models) for v in vs)
    assert len(s.rows) == combos and s.tests == combos - len(ROTATIONS)  # 순환은 포트폴리오로 따로 판정
    assert {v.family for v in vs} == {"pattern", "trend", "rotation"}
    assert set(s.rotation) == {"rs_top", "dual_momentum"} and all(set(a) == {"control", "bh"} for a in s.rotation.values())
    kt = next(r for r in s.rows if r.variant.id == "kangaroo_tail.zone" and r.model == "bar_close")
    ctl = next(r for r in s.rows if r.variant.id == "random.zone" and r.model == "bar_close")
    if kt.all.n >= 2:  # 같은 청산의 무작위 대조군과 평균 차
        assert kt.vs_control is not None and kt.vs_control[0] == pytest.approx(kt.all.mean - ctl.all.mean)
    assert len(run2.bh_curve) == len(run2.timeline) == 500
    assert all(len(c) == 500 for c in run2.curves.values())
    text = render(s)
    assert "다중검정" in text and "국면별 선택 시험" in text and "매수보유(기준선)" in text and "샤프" in text
    out = save(s, tmp_path / "r")
    data = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert data["tests"] == s.tests and len(data["rows"]) == len(s.rows)
    trades = sum(len(ts) for ts in run2.trades.values())
    assert len((out / "trades.csv").read_text(encoding="utf-8-sig").strip().splitlines()) == trades + 1
    assert "## 한계" in (out / "report.md").read_text(encoding="utf-8")


# ----------------------------------------------------------------------------- 책 패턴 해석(사전 등록)


def test_loose_drops_optimizers_strict_adds_them():
    tail = (102.6, 103.2, 97.0, 103.0)
    rows = from_closes(box() + line(110, 102.5, 8))
    rows[46] = (rows[46][0], rows[46][1], 96.0, rows[46][3])  # 꼬리보다 낮은 저가가 왼쪽에 있음 → 왼쪽 공간 없음
    bars = make(rows + [tail])
    assert not scan(Context.for_interp(bars, INTERPRETATIONS["v1"]), ENTRIES["kangaroo_tail"])
    assert 49 in scan(Context.for_interp(bars, INTERPRETATIONS["loose"]), ENTRIES["kangaroo_tail"])  # 최적 조건은 안 따짐
    strict = Context.for_interp(at_support(tail), INTERPRETATIONS["strict"])
    assert strict.zones.k == 10 and strict.zones.lookback == 2000  # 큰 구간만
    found = scan(strict, ENTRIES["kangaroo_tail"])
    assert list(found) == [49] and found[49].window == 1  # 다음 1봉 안에 진입해야 함
    bearish = Context.for_interp(at_support((103.0, 103.2, 97.0, 102.6)), INTERPRETATIONS["strict"])
    assert not scan(bearish, ENTRIES["kangaroo_tail"])  # 엄격: 종가 > 시가
    assert 49 in scan(Context(at_support((103.0, 103.2, 97.0, 102.6))), ENTRIES["kangaroo_tail"])


def test_regime_interpretation_limits_reversals_and_control_to_range():
    x = Context.for_interp(at_support((102.6, 103.2, 97.0, 103.0)), INTERPRETATIONS["regime"])
    x.regime = ["down"] * len(x.b)
    assert not scan(x, ENTRIES["kangaroo_tail"]) and not scan(x, ENTRIES["random"])
    x.regime = ["range"] * len(x.b)
    assert list(scan(x, ENTRIES["kangaroo_tail"])) == [49]


def test_interpretation_exit_rules():
    rows = [(100, 106, 99, 105.5), (105.5, 106.0, 96.0, 97.0)]  # 진입 뒤 손절(95) 전 75% 지점(97.5) 아래로 마감
    cut = Context(make([(100.0, 100.5, 99.5, 100.0)] * 21 + rows + [(97.0, 97.5, 96.5, 97.0)] * 3), Rules(kt_cut75=True))
    t = simulate(cut, {20: Setup("kangaroo_tail", 20, 105.0, 95.0, ())}, "three_bar", "intrabar", FREE, variant="v").trades[0]
    assert t.reason == "75% 컷" and t.exit_px == pytest.approx(97.0)
    near = [(100, 106, 99, 105.5), (105.5, 109.0, 105.0, 108.5)]  # 가장 가까운 구간(108)은 위험(10)보다 가깝다
    for rules, exit_px in ((Rules(min_rr=0.0), 108.0), (Rules(), None)):
        x = Context(make([(100.0, 100.5, 99.5, 100.0)] * 21 + near + [(108.5, 108.6, 108.4, 108.5)] * 3), rules)
        t = simulate(x, {20: Setup("t", 20, 105.0, 95.0, (108.0,))}, "zone", "intrabar", FREE, variant="v").trades[0]
        assert (t.exit_px == pytest.approx(exit_px)) if exit_px else t.reason == "데이터 끝"  # v1은 위험의 1배 이상만 목표


def test_variants_per_interpretation_pair_with_same_interpretation_control():
    vs = variants("kr_stock", families=["pattern"], interpretations=["v1", "strict"])
    assert len(vs) == 2 * 7 * 4 and {v.interp for v in vs} == {"v1", "strict"}
    v = next(v for v in vs if v.id == "strict:kangaroo_tail.zone")
    assert v.label.startswith("[엄격]") and v.prefix == "strict:"
    with pytest.raises(ValueError):
        variants("kr_stock", interpretations=["nope"])
    bars = [make(walk(500, 21), "kr_stock:AAA"), make(walk(500, 22), "kr_stock:BBB")]
    s = summarize(run_lab(bars, "kr_stock", vs, Costs(0.001, 0.0005)))
    row = next(r for r in s.rows if r.variant.id == "strict:random.zone" and r.model == "bar_close")
    kt = next(r for r in s.rows if r.variant.id == "strict:kangaroo_tail.zone" and r.model == "bar_close")
    if kt.all.n >= 2 and row.all.n >= 2:
        assert kt.vs_control is not None and kt.vs_control[0] == pytest.approx(kt.all.mean - row.all.mean)
    assert "해석별 요약" in render(s) and any(k.startswith("strict:") for k in s.run.setup_counts)


def test_judge_applies_preregistered_rule():
    def row(variant, vs, eval_mean, n=50, control=False, family="pattern"):
        return {"variant": variant, "label": variant, "model": "bar_close", "family": family, "control": control,
                "vs_control": vs, "all": {"n": n}, "eval": {"mean": eval_mean, "n": n // 3}}

    a = {"market": "kr_stock", "interval": "1d", "tests": 224, "z_threshold": 3.5, "rows": [
        row("sure", (0.01, 3.6), 0.01), row("maybe", (0.01, 2.0), 0.01), row("few", (0.05, 9.0), 0.02, n=10),
        row("neg_eval", (0.01, 4.0), -0.01), row("weak", (0.01, 1.0), 0.01), row("random.zone", None, 0.0, control=True)]}
    b = {"market": "us_stock", "interval": "1d", "tests": 224, "z_threshold": 3.5, "rows": [
        row("sure", (0.02, 1.7), 0.02), row("maybe", (0.01, 1.8), 0.01), row("few", (0.05, 9.0), 0.02),
        row("neg_eval", (0.01, 4.0), 0.01), row("weak", (0.01, 4.0), 0.01), row("random.zone", None, 0.0, control=True)]}
    got = {v.variant: v.verdict for v in judge(a, b)}
    assert got == {"sure": "확실", "maybe": "유망", "few": "판단 불가", "neg_eval": "실패", "weak": "실패"}
    assert "확실 1" in render_judge(a, b, judge(a, b))


# ----------------------------------------------------------------------------- 추세·순환·주봉


def test_weekly_aggregation_drops_unfinished_week():
    # 2020-01-06(월)부터 월~금 2주 + 월·화 → 완성된 2주만(마지막 주는 아직 진행 중)
    days = [datetime(2020, 1, 6, tzinfo=timezone.utc) + timedelta(days=d) for d in (0, 1, 2, 3, 4, 7, 8, 9, 10, 11, 14, 15)]
    n = len(days)
    b = Bars("kr_stock:TEST", "1d", days, [d + timedelta(hours=6) for d in days], [float(i) for i in range(n)],
             [i + 10.0 for i in range(n)], [i - 10.0 for i in range(n)], [i + 0.5 for i in range(n)])
    w = weekly(b)
    assert len(w) == 2 and w.interval == "1w"
    assert w.o == [0.0, 5.0] and w.c == [4.5, 9.5] and w.h == [14.0, 19.0] and w.l == [-10.0, -5.0]
    assert w.open_time[0] == days[0] and w.close_time[1] == days[9] + timedelta(hours=6)
    assert bars_per_year("kr_stock", "1w") == 52 and bars_per_year("kr_stock", "1d") == 252


def test_rolling_helpers():
    c = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]
    assert prior_max(c, 3) == [None, None, None, 4.0, 4.0, 5.0, 9.0, 9.0]  # 봉 t를 뺀 직전 3봉 최고
    assert sma_series(c, 2)[:3] == [None, 2.0, 2.5]


def trend_series() -> list[float]:
    """1년 넘게 횡보 → 300번에서 신고가 → 130까지 상승 → 110까지 하락."""
    return [100.0 + (i % 5) * 0.2 for i in range(300)] + line(100.8, 130, 30) + line(130, 110, 30)


def test_52_week_high_enters_next_open_and_exits_on_trend_break():
    closes = trend_series()
    x = Context(make(from_closes(closes)))
    found = scan(x, ENTRIES["high_52w"])
    assert min(found) == 300 and found[300].at_open
    assert found[300].stop == pytest.approx(closes[300] - 3 * x.atr[300])
    for model in MODELS:
        ma = simulate(x, {300: found[300]}, "ma_exit", model, FREE, variant="v").trades[0]
        assert ma.entry_px == x.b.o[301] and ma.reason == "이평 이탈"  # 확인 없이 다음 봉 시가 진입
        trail = simulate(x, {300: found[300]}, "atr_trail", model, FREE, variant="v").trades[0]
        assert trail.reason == "손절" and trail.exit_px > trail.entry_px  # 고점 − 3 ATR로 올라간 손절이 이익을 지킨다
    m = x.sma(x.window(200 / 252))
    crosses = scan(x, ENTRIES["ma_cross"])
    assert crosses and all(x.b.c[t] > m[t] and x.b.c[t - 1] <= m[t - 1] for t in crosses)  # type: ignore[operator]


def rotation_bars(paths: dict[str, list[float]]) -> list[Bars]:
    return [make(from_closes(cs, wick=0.1), iid) for iid, cs in paths.items()]


def timeline_of(bars: list[Bars]) -> list[datetime]:
    return sorted({t for b in bars for t in b.close_time})


def test_rotation_picks_strongest_and_dual_momentum_stays_in_cash():
    n = 400
    up, flat, down = [100 * 1.002 ** i for i in range(n)], [100.0] * n, [100 * 0.999 ** i for i in range(n)]
    bars = rotation_bars({"kr_stock:UP": up, "kr_stock:FLAT": flat, "kr_stock:D1": down, "kr_stock:D2": down})
    trades, curve, exposure = simulate_rotation(bars, ROTATIONS["rs_top"], FREE, timeline_of(bars), variant="rs")
    assert {t.instrument_id for t in trades} == {"kr_stock:UP"}  # 4종목의 상위 1/4 = 1종목
    assert curve[-1] > 1.1 and 0 < exposure < 1
    falling = rotation_bars({"kr_stock:A": down, "kr_stock:B": [100 * 0.998 ** i for i in range(n)]})
    tr, curve2, exp2 = simulate_rotation(falling, ROTATIONS["dual_momentum"], FREE, timeline_of(falling), variant="dm")
    assert tr == [] and curve2[-1] == 1.0 and exp2 == 0.0  # 절대 모멘텀이 음수면 현금
    tr, _, _ = simulate_rotation(falling, ROTATIONS["rs_top"], FREE, timeline_of(falling), variant="rs")
    assert {t.instrument_id for t in tr} == {"kr_stock:A"}  # 상대강도만 보면 덜 떨어진 종목을 산다


def test_rotation_costs_only_on_traded_value():
    const = rotation_bars({"kr_stock:X": [50.0] * 300, "kr_stock:Y": [80.0] * 300})
    costs = Costs(0.001, 0.0005)
    tr, curve, _ = simulate_rotation(const, ROTATIONS["equal_monthly"], costs, timeline_of(const), variant="eq")
    assert {t.instrument_id for t in tr} == {"kr_stock:X", "kr_stock:Y"}
    assert curve[-1] == pytest.approx(0.999 * 0.9995 / (1.001 * 1.0005), rel=1e-9)  # 첫 매수·마지막 정리 비용만


def test_rotation_no_lookahead():
    rows = {f"kr_stock:S{k}": [r[3] for r in walk(700, 10 + k)] for k in range(6)}
    costs = Costs(0.001, 0.0005)
    full = rotation_bars(rows)
    a, _, _ = simulate_rotation(full, ROTATIONS["rs_top"], costs, timeline_of(full), variant="r")
    cut = 600
    part = rotation_bars({k: v[:cut] for k, v in rows.items()})
    b, _, _ = simulate_rotation(part, ROTATIONS["rs_top"], costs, timeline_of(part), variant="r")
    limit = part[0].open_time[cut - 1]
    early = [t for t in a if t.exit_time < limit]
    assert len(early) >= 2 and early == [t for t in b if t.exit_time < limit]


# ----------------------------------------------------------------------------- 저장소·명령


def test_lab_store_merge_and_symbol_names(tmp_path):
    store = LabStore(tmp_path)

    def candle(day: int, close: str, high: str = "11", low: str = "9") -> Candle:
        t = T0 + timedelta(days=day)
        return Candle("us_stock:NASD:AAPL", "1d", t, t + timedelta(hours=6), Decimal("10"), Decimal(high), Decimal(low),
                      Decimal(close), Decimal(1))

    assert store.save([candle(0, "10"), candle(1, "10.5")], "us_stock", "1d", "NASD:AAPL") == 2
    assert store.save([candle(1, "10.6"), candle(2, "10.2", high="9", low="11")], "us_stock", "1d", "NASD:AAPL") == 3
    assert store.symbols("us_stock", "1d") == ["NASD:AAPL"]
    b = store.load("us_stock", "1d", "NASD:AAPL")
    assert b is not None and b.c == [10.0, 10.6]  # 덮어쓰기 + 깨진 봉(고가<저가) 제외
    assert bars_for_days("kr_stock", "1d", 365) == int(252 * 1.05) + 5 and bars_for_days("crypto", "240m", 10) == 60


def test_fetch_caps_bars_to_source_limit(tmp_path):
    import asyncio

    from aifund.brokers.base import MarketData
    from aifund.domain.models import Instrument
    from aifund.lab.data import fetch

    class FakeKiwoomUS(MarketData):
        source_name = "kiwoom_us"
        asked: list[int] = []

        async def instruments(self, market, symbols):
            return [Instrument(f"{market}:{s}", market, s, "NASD", s, "USD", s, "us", None, Decimal(1), Decimal(1), None)
                    for s in symbols]

        async def candles(self, instrument, interval, count):
            self.asked.append(count)
            t = T0
            return [Candle(instrument.instrument_id, "1d", t, t + timedelta(hours=6), Decimal(1), Decimal(1), Decimal(1),
                           Decimal(1), Decimal(1))]

        async def quotes(self, instruments):
            return []

    src = FakeKiwoomUS()
    res = asyncio.run(fetch(LabStore(tmp_path), src, "us_stock", "1d", ["NASD:AAPL"], 2651, source_label="kiwoom", demo=False))
    assert src.asked == [1990] and res == {"NASD:AAPL": 1}  # 연속조회 한도(100봉 × 20페이지) 안으로 줄여 요청


def test_lab_cli_offline_demo(home, capsys):
    from aifund.cli import main

    assert main(["lab", "catalog", "--market", "kr_stock"]) == 0
    assert "캥거루 꼬리" in capsys.readouterr().out
    assert main(["--mode", "offline_demo", "lab", "fetch", "--market", "kr_stock", "--days", "1200",
                 "--symbols", "005930,000660"]) == 0
    assert main(["--mode", "offline_demo", "lab", "run", "--market", "kr_stock", "--model", "bar_close"]) == 0
    out = capsys.readouterr().out
    assert "가짜 데이터" in out and "다중검정" in out and "상대강도 상위 1/4" in out
    assert main(["--mode", "offline_demo", "lab", "run", "--market", "kr_stock", "--candle", "1w",
                 "--families", "trend,rotation"]) == 0
    out = capsys.readouterr().out
    assert "주봉" in out and "52주 신고가" in out and "캥거루" not in out
    reports = sorted((home / "var" / "offline_demo" / "lab" / "reports").glob("*/report.md"))
    assert len(reports) == 2 and reports[-1].parent.name.endswith("kr_stock-1w")
    assert main(["--mode", "offline_demo", "lab", "run", "--market", "kr_stock", "--families", "pattern",
                 "--interpretations", "v1,strict", "--model", "bar_close"]) == 0
    assert "해석별 요약" in capsys.readouterr().out
    latest = max((home / "var" / "offline_demo" / "lab" / "reports").glob("*/summary.json"), key=lambda p: p.stat().st_mtime)
    assert main(["lab", "judge", str(latest.parent), str(latest.parent)]) == 0
    assert "사전 등록 판정" in capsys.readouterr().out
