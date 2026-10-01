"""연구소 결과 요약·판정·보고서(터미널·마크다운·JSON·거래 CSV)."""

from __future__ import annotations

import csv
import json
import math
import unicodedata
from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from statistics import NormalDist, mean, median, stdev, variance
from typing import Any

from aifund.lab.bars import REGIMES
from aifund.lab.catalog import FAMILIES, Variant
from aifund.lab.engine import MODEL_LABELS, Trade
from aifund.lab.exits import EXITS, FALLBACK_R, MIN_RR
from aifund.lab.rotation import ROTATIONS
from aifund.lab.runner import LabRun
from aifund.lab.setups import ENTRIES, INTERPRETATIONS, RULES_VERSION

MARKET_LABELS = {"crypto": "코인", "kr_stock": "국내주식", "us_stock": "미국주식"}
INTERVAL_LABELS = {"1d": "일봉", "1w": "주봉", "240m": "4시간봉", "60m": "1시간봉", "30m": "30분봉", "15m": "15분봉"}
REGIME_LABELS = {"up": "상승", "range": "횡보", "down": "하락"}
ALPHA = 0.05
MIN_N = 10  # 국면별 선택에서 평균을 믿을 최소 거래 수
TOP = 3
CONTROL_ENTRY = {"pattern": "random", "trend": "random_hold"}  # 계열별 무작위 대조군 진입
ROTATION_CONTROL = "equal_monthly"


@dataclass
class Stats:
    n: int
    win: float | None = None
    mean: float | None = None
    median: float | None = None
    pf: float | None = None  # 이익 합 ÷ 손실 합
    mean_r: float | None = None
    t: float | None = None  # 평균 ÷ 표준오차
    bars: float | None = None  # 평균 보유 봉 수


@dataclass
class CurveStats:
    total: float
    cagr: float | None  # 연환산 수익률
    mdd: float
    sharpe: float | None  # 연환산 샤프(무위험수익 0)
    dev: float
    eval: float


@dataclass
class Row:
    variant: Variant
    model: str
    all: Stats
    dev: Stats
    eval: Stats
    regimes: dict[str, Stats]
    curve: CurveStats
    exposure: float
    significant: bool
    vs_control: tuple[float, float | None] | None = None  # (거래 평균 차, 웰치 t) — 같은 청산의 무작위 대조군 대비


@dataclass
class Active:
    """포트폴리오 초과수익: 연환산 차와 매 봉 수익률 차의 t값."""

    cagr_diff: float | None
    t: float | None


@dataclass
class Summary:
    run: LabRun
    rows: list[Row]
    bh: CurveStats
    tests: int  # 거래 단위로 판정한 (변형, 체결 가정) 조합 수(순환 계열은 포트폴리오로 따로 판정)
    z_threshold: float  # 본페로니 보정 단측 기준
    selection: dict[str, Any] | None
    rotation: dict[str, dict[str, Active]]  # 순환 전략 → {"control": 동일비중 대비, "bh": 매수보유 대비}


def trade_stats(trades: list[Trade]) -> Stats:
    n = len(trades)
    if n == 0:
        return Stats(0)
    rs = [t.ret for t in trades]
    m = sum(rs) / n
    gains = sum(r for r in rs if r > 0)
    losses = -sum(r for r in rs if r < 0)
    sd = stdev(rs) if n >= 2 else 0.0
    rr = [t.r_mult for t in trades if t.r_mult is not None]
    return Stats(n, sum(r > 0 for r in rs) / n, m, median(rs), gains / losses if losses > 0 else None,
                 sum(rr) / len(rr) if rr else None, m / (sd / math.sqrt(n)) if sd > 0 else None,
                 sum(t.bars for t in trades) / n)


def curve_stats(curve: list[float], timeline: list[datetime], split: datetime) -> CurveStats:
    peak, mdd = 1.0, 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak > 0 else 0.0)
    g = bisect_left(timeline, split)
    at_split = curve[g - 1] if g > 0 else 1.0
    years = (timeline[-1] - timeline[0]).total_seconds() / (365.25 * 86400)
    cagr = curve[-1] ** (1 / years) - 1 if years > 0 and curve[-1] > 0 else None
    rets = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve)) if curve[i - 1] > 0]
    sharpe = None
    if years > 0 and len(rets) >= 2 and (sd := stdev(rets)) > 0:
        sharpe = sum(rets) / len(rets) / sd * math.sqrt(len(rets) / years)
    return CurveStats(curve[-1] - 1, cagr, mdd, sharpe, at_split - 1, curve[-1] / at_split - 1 if at_split > 0 else 0.0)


def welch(a: list[float], b: list[float]) -> tuple[float, float | None] | None:
    """평균 차와 웰치 t."""
    if len(a) < 2 or len(b) < 2:
        return None
    diff = mean(a) - mean(b)
    se = math.sqrt(variance(a) / len(a) + variance(b) / len(b))
    return diff, diff / se if se > 0 else None


def active(curve: list[float], base: list[float], timeline: list[datetime]) -> Active:
    a = [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve))]
    b = [base[i] / base[i - 1] - 1 for i in range(1, len(base))]
    d = [x - y for x, y in zip(a, b)]
    years = (timeline[-1] - timeline[0]).total_seconds() / (365.25 * 86400)
    t = mean(d) / stdev(d) * math.sqrt(len(d)) if len(d) >= 2 and stdev(d) > 0 else None
    ca = curve[-1] ** (1 / years) - 1 if years > 0 and curve[-1] > 0 else None
    cb = base[-1] ** (1 / years) - 1 if years > 0 and base[-1] > 0 else None
    return Active(None if ca is None or cb is None else ca - cb, t)


def summarize(run: LabRun) -> Summary:
    keys = list(run.trades)
    by_id = {v.id: v for v in run.variants}
    tested = [k for k in keys if by_id[k[0]].family != "rotation"]
    z = NormalDist().inv_cdf(1 - ALPHA / max(1, len(tested)))
    rows = []
    for vid, m in keys:
        v = by_id[vid]
        ts = run.trades[(vid, m)]
        st = trade_stats(ts)
        vs = None
        if v.family in CONTROL_ENTRY and not v.control and v.exit is not None:
            ctl = run.trades.get((f"{v.prefix}{CONTROL_ENTRY[v.family]}.{v.exit.kind}", m))  # 같은 해석의 대조군
            vs = welch([t.ret for t in ts], [t.ret for t in ctl]) if ctl is not None else None
        rows.append(Row(v, m, st, trade_stats([t for t in ts if t.entry_time < run.split]),
                        trade_stats([t for t in ts if t.entry_time >= run.split]),
                        {r: trade_stats([t for t in ts if t.regime == r]) for r in REGIMES},
                        curve_stats(run.curves[(vid, m)], run.timeline, run.split), run.exposure[(vid, m)],
                        v.family != "rotation" and st.t is not None and st.t >= z, vs))
    rotation: dict[str, dict[str, Active]] = {}
    control_curve = run.curves.get((ROTATION_CONTROL, "bar_close"))
    for v in run.variants:
        if v.family == "rotation" and not v.control and (v.id, "bar_close") in run.curves:
            cur = run.curves[(v.id, "bar_close")]
            rotation[v.id] = {"bh": active(cur, run.bh_curve, run.timeline)}
            if control_curve is not None:
                rotation[v.id]["control"] = active(cur, control_curve, run.timeline)
    model = "bar_close" if "bar_close" in run.models else run.models[0]
    return Summary(run, rows, curve_stats(run.bh_curve, run.timeline, run.split), len(tested), z,
                   regime_selection(run, model), rotation)


def regime_selection(run: LabRun, model: str, *, min_n: int = MIN_N, top: int = TOP) -> dict[str, Any] | None:
    """사용자 아이디어(국면 판단 → 맞는 전략 선택)를 과거 데이터로 시험한다(AI 대신 코드 국면 판정).

    후보는 종목별 타이밍 전략(책 패턴·추세, 대조군 제외). 개발 구간에서 국면마다 평균 거래 수익이 높은 상위 top개를
    고르고(거래 min_n개 이상·평균 > 0), 평가 구간에서는 신호 봉의 국면이 같을 때 그 전략의 거래만 받는다.
    비교: 후보 전략 전체, 개발 구간 1위 단일 전략. 거래 단위 비교이며 자금 배분(동시 보유)은 반영하지 않는다.
    """
    pool = [v for v in run.variants if v.family in ("pattern", "trend") and not v.control and (v.id, model) in run.trades]
    if not pool:
        return None
    dev = {v.id: [t for t in run.trades[(v.id, model)] if t.entry_time < run.split] for v in pool}
    ev = {v.id: [t for t in run.trades[(v.id, model)] if t.entry_time >= run.split] for v in pool}
    chosen: dict[str, list[str]] = {}
    for r in REGIMES:
        cands = []
        for vid, ts in dev.items():
            rs = [t.ret for t in ts if t.regime == r]
            if len(rs) >= min_n and sum(rs) / len(rs) > 0:
                cands.append((sum(rs) / len(rs), vid))
        chosen[r] = [vid for _, vid in sorted(cands, reverse=True)[:top]]
    union = {vid for vids in chosen.values() for vid in vids}
    selected, mismatched = [], []  # 고른 전략의 평가 구간 거래를 국면이 맞는 것·안 맞는 것으로 나눈다
    for vid in union:
        for t in ev[vid]:
            (selected if t.regime is not None and vid in chosen[t.regime] else mismatched).append(t)
    ranked = sorted(((st.mean, vid) for vid, ts in dev.items() if (st := trade_stats(ts)).n >= min_n and st.mean is not None),
                    reverse=True)
    best = ranked[0][1] if ranked else None
    return {"model": model, "chosen": chosen, "selected": trade_stats(selected),
            # 국면 판단이 보탠 몫: 같은 전략을 국면 구분 없이 모두 쓴 경우, 국면이 안 맞는 거래와 비교한다
            "unfiltered": trade_stats(selected + mismatched),
            "regime_edge": welch([t.ret for t in selected], [t.ret for t in mismatched]),
            "all": trade_stats([t for ts in ev.values() for t in ts]),
            "best_variant": best, "best": trade_stats(ev[best]) if best else Stats(0)}


# ----------------------------------------------------------------------------- 출력 도구


def _w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    gap = " " * max(0, width - _w(s))
    return gap + s if right else s + gap


def table(header: list[str], rows: list[list[str]], left: int = 1) -> list[str]:
    """터미널 표(앞 left개 열은 왼쪽, 나머지는 오른쪽 정렬). 한글 폭을 2칸으로 센다."""
    widths = [max(_w(r[i]) for r in [header, *rows]) for i in range(len(header))]
    return ["  ".join(_pad(c, widths[i], i >= left) for i, c in enumerate(r)) for r in [header, *rows]]


def md_table(header: list[str], rows: list[list[str]]) -> list[str]:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + ["| " + " | ".join(r) + " |" for r in rows]


def pct(x: float | None, nd: int = 2) -> str:
    return "-" if x is None else f"{x * 100:+.{nd}f}%"


def num(x: float | None, nd: int = 2) -> str:
    return "-" if x is None else f"{x:.{nd}f}"


def _date(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def header_lines(s: Summary, demo: bool) -> list[str]:
    run = s.run
    c = run.costs
    lines = [f"전략 연구소 · {MARKET_LABELS.get(run.market, run.market)} {INTERVAL_LABELS.get(run.interval, run.interval)} · "
             f"{len(run.instruments)}종목 · {_date(run.timeline[0])} ~ {_date(run.timeline[-1])} · 봉 {run.bars_total:,}개",
             f"개발 구간 ~ {_date(run.split)} / 평가 구간 {_date(run.split)} ~ (평가 구간은 고르는 데 쓰지 않은 구간)",
             f"비용: 편도 {c.fee * 100:.3f}% + 슬리피지 {c.slip * 100:.3f}% · "
             "체결: 현 시스템 방식(봉 마감 판단 → 다음 봉 시가) / 책 방식(장중 역지정가·지정가)",
             f"기준선 매수보유(동일비중): 전체 {pct(s.bh.total, 1)} · 연 {pct(s.bh.cagr, 1)} · 최대낙폭 {s.bh.mdd * 100:.1f}% · "
             f"샤프 {num(s.bh.sharpe)} · 개발 {pct(s.bh.dev, 1)} · 평가 {pct(s.bh.eval, 1)}"]
    groups: dict[str, list[str]] = {}
    for name, n in run.setup_counts.items():
        interp, _, kind = name.rpartition(":")
        groups.setdefault(interp or "v1", []).append(f"{ENTRIES[kind].label} {n:,}")
    for interp, items in groups.items():
        tag = "" if len(groups) == 1 else f" [{INTERPRETATIONS[interp].label}]"
        lines.append(f"신호 봉 수{tag}: " + ", ".join(items))
    if demo:
        lines.insert(0, "※ 가짜 데이터(offline_demo)로 돌린 결과입니다. 전략 판단에 쓰지 마세요.")
    return lines


def _ranked(s: Summary, model: str) -> list[Row]:
    """거래가 있는 종목별 타이밍 변형(책 패턴·추세)을 t값 순으로. 순환 계열은 포트폴리오 표에서 본다."""
    return sorted((r for r in s.rows if r.model == model and r.all.n and r.variant.family != "rotation"),
                  key=lambda r: (r.all.t is None, -(r.all.t or 0)))


def _no_trades(s: Summary) -> list[str]:
    """어느 체결 가정에서도 거래가 없는 변형."""
    counts: dict[str, int] = {}
    for r in s.rows:
        counts[r.variant.label] = counts.get(r.variant.label, 0) + r.all.n
    return [label for label, n in counts.items() if n == 0]


def portfolio_rows(s: Summary) -> tuple[list[str], list[list[str]]]:
    """현 시스템 방식 포트폴리오를 샤프 순으로(매수보유 기준선 포함)."""
    head = ["전략", "계열", "전체", "연환산", "최대낙폭", "샤프", "투입", "개발 구간", "평가 구간"]
    items: list[tuple[str, str, CurveStats, float | None]] = [
        (("★ " if r.significant else "") + r.variant.label, FAMILIES[r.variant.family], r.curve, r.exposure)
        for r in s.rows if r.model == "bar_close"]
    items.append(("매수보유(기준선)", "-", s.bh, 1.0))
    items.sort(key=lambda it: (it[2].sharpe is None, -(it[2].sharpe or 0)))
    out = [[name, fam, pct(cs.total, 1), pct(cs.cagr, 1), f"{cs.mdd * 100:.1f}%", num(cs.sharpe),
            "-" if exp is None else f"{exp * 100:.0f}%", pct(cs.dev, 1), pct(cs.eval, 1)] for name, fam, cs, exp in items]
    return head, out


def main_rows(s: Summary, model: str) -> tuple[list[str], list[list[str]]]:
    other = next((m for m in s.run.models if m != model), None)
    by_key = {(r.variant.id, r.model): r for r in s.rows}
    head = ["전략", "계열", "거래", "승률", "평균", "중앙값", "PF", "평균R", "t", "대조군 대비(t)", "보유 봉",
            "개발 평균(거래)", "평가 평균(거래)"]
    if other:
        head.append(f"{MODEL_LABELS[other]} 평균")
    out = []
    for r in _ranked(s, model):
        vs = "-" if r.vs_control is None else f"{pct(r.vs_control[0])} ({num(r.vs_control[1], 1)})"
        line = [("★ " if r.significant else "") + r.variant.label, FAMILIES[r.variant.family], str(r.all.n),
                "-" if r.all.win is None else f"{r.all.win * 100:.0f}%", pct(r.all.mean), pct(r.all.median), num(r.all.pf),
                num(r.all.mean_r), num(r.all.t, 1), vs, num(r.all.bars, 0),
                f"{pct(r.dev.mean)} ({r.dev.n})", f"{pct(r.eval.mean)} ({r.eval.n})"]
        if other:
            twin = by_key.get((r.variant.id, other))
            line.append(pct(twin.all.mean) if twin else "-")
        out.append(line)
    return head, out


def interp_rows(s: Summary) -> tuple[list[str], list[list[str]]] | None:
    """책 패턴 해석별 요약(해석이 둘 이상일 때). 대조군 = 같은 해석·같은 청산·같은 체결 가정의 무작위 진입."""
    order = list(INTERPRETATIONS)
    interps = sorted({r.variant.interp for r in s.rows if r.variant.family == "pattern"}, key=order.index)
    if len(interps) < 2:
        return None
    head = ["해석", "체결", "변형", "대조군보다 평균 높음", "대조군 대비 t ≥ 1.65", "대조군 대비 최고(t)"]
    out = []
    for i in interps:
        for m in s.run.models:
            rs = [r for r in s.rows if r.variant.family == "pattern" and r.variant.interp == i and r.model == m
                  and r.vs_control is not None]
            higher = sum(1 for r in rs if r.vs_control is not None and r.vs_control[0] > 0)
            strong = sum(1 for r in rs if r.vs_control is not None and (r.vs_control[1] or 0) >= 1.65)
            best = max(rs, key=lambda r: r.vs_control[1] if r.vs_control and r.vs_control[1] is not None else -1e9,
                       default=None)
            top = "-" if best is None or best.vs_control is None else (
                f"{best.variant.label} {pct(best.vs_control[0])} ({num(best.vs_control[1], 1)})")
            out.append([INTERPRETATIONS[i].label, MODEL_LABELS[m], str(len(rs)), str(higher), str(strong), top])
    return head, out


def regime_rows(s: Summary, model: str, limit: int | None = None) -> tuple[list[str], list[list[str]]]:
    rows = _ranked(s, model)[:limit] if limit else _ranked(s, model)
    head = ["전략"] + [f"{REGIME_LABELS[g]} 평균(거래)" for g in REGIMES]
    return head, [[r.variant.label] + [f"{pct(r.regimes[g].mean)} ({r.regimes[g].n})" for g in REGIMES] for r in rows]


def verdict_lines(s: Summary) -> list[str]:
    passed = [f"{r.variant.label}({MODEL_LABELS[r.model]})" for r in s.rows if r.significant]
    beat = [f"{r.variant.label}({MODEL_LABELS[r.model]}, t {r.vs_control[1]:.1f})" for r in s.rows
            if r.vs_control is not None and r.vs_control[1] is not None and r.vs_control[1] >= s.z_threshold]
    lines = [f"다중검정: 종목별 타이밍 전략(책 패턴·추세)을 체결 가정별로 {s.tests}번 시험 → 우연으로 보기 어려운 기준 "
             f"t ≥ {s.z_threshold:.2f}(본페로니 {ALPHA:.0%}). 기준 통과(★): " + (", ".join(passed) if passed else "없음"),
             "무작위 대조군보다 확실히 나은 진입(같은 청산, 웰치 t가 위 기준 이상): " + (", ".join(beat) if beat else "없음")]
    if empty := _no_trades(s):
        lines.append("거래 없음: " + ", ".join(empty))
    names = {v.id: v.label for v in s.run.variants}
    for vid, act in s.rotation.items():
        parts = [f"{'동일비중 대조군' if k == 'control' else '매수보유'} 대비 연 {pct(a.cagr_diff, 1)}p(t {num(a.t, 1)})"
                 for k, a in sorted(act.items(), key=lambda kv: kv[0] != "control")]
        lines.append(f"순환 '{names[vid]}': " + ", ".join(parts))
    bh_sharpe = s.bh.sharpe or 0.0
    better = sorted(((r.curve.sharpe, r.variant.label) for r in s.rows
                     if r.model == "bar_close" and r.curve.sharpe is not None and r.curve.sharpe > bh_sharpe), reverse=True)
    lines.append(f"샤프가 매수보유({num(s.bh.sharpe)})보다 높은 포트폴리오(현 시스템 방식): "
                 + (", ".join(f"{label}({sh:.2f})" for sh, label in better) if better else "없음"))
    sel = s.selection
    if sel:
        names = {v.id: v.label for v in s.run.variants}
        chosen = " / ".join(f"{REGIME_LABELS[g]}: " + (", ".join(names[v] for v in sel["chosen"][g]) or "없음") for g in REGIMES)
        best = names.get(sel["best_variant"], "없음")
        lines += [f"국면별 선택 시험({MODEL_LABELS[sel['model']]}, 개발 구간에서 국면마다 평균 상위 {TOP}개 → 평가 구간):",
                  f"  고른 전략 — {chosen}",
                  f"  평가 구간 거래 평균: 국면별 선택 {pct(sel['selected'].mean)} ({sel['selected'].n}거래) · "
                  f"같은 전략을 국면 구분 없이 {pct(sel['unfiltered'].mean)} ({sel['unfiltered'].n}) · "
                  f"후보 전략 전체 {pct(sel['all'].mean)} ({sel['all'].n}) · "
                  f"개발 1위 단일 '{best}' {pct(sel['best'].mean)} ({sel['best'].n})"]
        edge = sel.get("regime_edge")
        if edge is not None:
            lines.append(f"  국면 판단의 몫: 국면이 맞는 거래가 안 맞는 거래보다 {pct(edge[0])}p (t {num(edge[1], 1)}"
                         f"{', 우연 범위' if edge[1] is None or abs(edge[1]) < 1.65 else ''})")
    return lines


READING = ("읽는 법: 포트폴리오 표는 종목마다 같은 금액을 배정해(순환 계열은 고른 종목에 배분) 전 기간 운용한 결과이고, "
           "투입은 돈이 들어가 있던 비율입니다. 거래 표의 평균은 거래 1회 평균 수익률(비용 차감, 큰 거래 몇 개에 끌려갈 수 있어 "
           "중앙값과 함께 봄). 평가 구간이 개발 구간과 같은 방향이고 대조군보다 나아야 의미가 있습니다.")


def render(s: Summary, demo: bool = False) -> str:
    lines = header_lines(s, demo)
    lines += ["", "[포트폴리오 — 현 시스템 방식, 샤프 순]"] + table(*portfolio_rows(s), left=2)
    if (ir := interp_rows(s)) is not None:
        lines += ["", "[해석별 요약 — 책 패턴, 대조군 = 같은 해석의 무작위 진입]"] + table(*ir, left=2)
    for m in s.run.models:
        lines += ["", f"[거래 — {MODEL_LABELS[m]}, t값 순(★ = 다중검정 기준 통과)]"] + table(*main_rows(s, m), left=2)
    model = s.selection["model"] if s.selection else s.run.models[0]
    lines += ["", f"[국면별 평균 — {MODEL_LABELS[model]}, t값 상위 10개, 국면은 신호 봉 시점 코드 판정]"]
    lines += table(*regime_rows(s, model, 10))
    lines += [""] + verdict_lines(s) + ["", READING]
    return "\n".join(lines)


def rules_lines() -> list[str]:
    out = [f"규칙 버전 {RULES_VERSION}. 진입은 모두 매수 전용입니다. 책 패턴 해석(사전 등록: docs/lab-preregistration.md):", ""]
    out += [f"- **{i.label}** (`{i.key}`): {i.note}" for i in INTERPRETATIONS.values()] + [""]
    for fam, label in FAMILIES.items():
        out.append(f"### {label}")
        if fam == "rotation":
            out += [f"- **{r.label}**: {r.rule}" for r in ROTATIONS.values()]
        else:
            out += [f"- **{e.label}** ({e.regime}): {e.rule}" for e in ENTRIES.values() if e.family == fam]
            out += [f"- 청산 **{x.label}**: {x.rule}" for x in EXITS.values() if x.family == fam]
        out.append("")
    return out


def markdown(s: Summary, demo: bool = False) -> str:
    run = s.run
    out = ["# 전략 연구소 결과", ""] + [f"- {ln}" for ln in header_lines(s, demo)] + ["", READING, ""]
    out += ["## 포트폴리오 (현 시스템 방식, 샤프 순)", ""] + md_table(*portfolio_rows(s)) + [""]
    if (ir := interp_rows(s)) is not None:
        out += ["## 해석별 요약 (책 패턴, 대조군 = 같은 해석의 무작위 진입)", ""] + md_table(*ir) + [""]
    for m in run.models:
        out += [f"## 거래 — {MODEL_LABELS[m]} (t값 순, ★ = 다중검정 기준 통과)", ""] + md_table(*main_rows(s, m)) + [""]
    model = s.selection["model"] if s.selection else run.models[0]
    out += [f"## 국면별 평균 ({MODEL_LABELS[model]})", ""] + md_table(*regime_rows(s, model)) + [""]
    out += ["## 판정", ""] + [f"- {ln.strip()}" for ln in verdict_lines(s)] + [""]
    out += ["## 규칙", ""] + rules_lines()
    out += ["## 한계", "",
            "- 시험 종목은 현재 기준으로 고른 종목이라 생존편향이 있습니다(사라진 종목 제외). 매수보유와 비교해 읽으세요.",
            "- 같은 데이터로 변형을 여러 개 시험할수록 우연히 좋아 보이는 것이 나옵니다. 다중검정 기준과 평가 구간을 함께 보세요.",
            f"- 책 패턴의 목표 구간은 위험의 {MIN_RR:g}배 이상 떨어진 것만 쓰고, 없으면 위험의 {FALLBACK_R:g}배를 씁니다(책에 없는 보완).",
            "- 수익률은 배정 금액 비율로 계산했고 주문 단위(1주)·최소 주문금액·호가 단위는 반영하지 않았습니다.",
            "- 국면별 선택 시험은 거래 단위 비교입니다(동시 보유·자금 배분 미반영)."]
    return "\n".join(out) + "\n"


def to_json(s: Summary, demo: bool) -> dict[str, Any]:
    run = s.run
    return {
        "market": run.market, "interval": run.interval, "instruments": run.instruments, "demo": demo,
        "start": run.timeline[0].isoformat(), "end": run.timeline[-1].isoformat(), "split": run.split.isoformat(),
        "costs": asdict(run.costs), "rules_version": RULES_VERSION, "tests": s.tests, "z_threshold": s.z_threshold,
        "setup_counts": run.setup_counts, "buy_hold": asdict(s.bh),
        "rows": [{"variant": r.variant.id, "label": r.variant.label, "family": r.variant.family,
                  "interp": r.variant.interp, "control": r.variant.control, "model": r.model,
                  "all": asdict(r.all), "dev": asdict(r.dev), "eval": asdict(r.eval),
                  "regimes": {g: asdict(st) for g, st in r.regimes.items()}, "curve": asdict(r.curve),
                  "exposure": r.exposure, "significant": r.significant, "vs_control": r.vs_control} for r in s.rows],
        "rotation": {vid: {k: asdict(a) for k, a in act.items()} for vid, act in s.rotation.items()},
        "selection": None if s.selection is None else {
            "model": s.selection["model"], "chosen": s.selection["chosen"], "best_variant": s.selection["best_variant"],
            "selected": asdict(s.selection["selected"]), "unfiltered": asdict(s.selection["unfiltered"]),
            "regime_edge": s.selection["regime_edge"],
            "all": asdict(s.selection["all"]), "best": asdict(s.selection["best"])},
    }


def save(s: Summary, out_dir: Path, demo: bool = False) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.md").write_text(markdown(s, demo), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(to_json(s, demo), ensure_ascii=False, indent=2, default=str),
                                          encoding="utf-8")
    with open(out_dir / "trades.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["variant", "model", "instrument_id", "entry", "exit", "setup_time", "entry_time", "exit_time", "entry_px",
                    "exit_px", "stop0", "ret", "r_mult", "bars", "reason", "regime"])
        for ts in s.run.trades.values():
            for t in ts:
                w.writerow([t.variant, t.model, t.instrument_id, t.entry, t.exit, t.setup_time.isoformat(),
                            t.entry_time.isoformat(), t.exit_time.isoformat(), f"{t.entry_px:.6g}", f"{t.exit_px:.6g}",
                            f"{t.stop0:.6g}", f"{t.ret:.6f}", "" if t.r_mult is None else f"{t.r_mult:.4f}", t.bars,
                            t.reason, t.regime or ""])
    return out_dir


def catalog_text(market: str | None = None) -> str:
    from aifund.lab.catalog import variants

    vs = variants(market or "kr_stock")
    counts = {fam: sum(1 for v in vs if v.family == fam) for fam in FAMILIES}
    lines = [f"변형 {len(vs)}개 — " + ", ".join(f"{FAMILIES[f]} {n}" for f, n in counts.items())
             + f" ({MARKET_LABELS.get(market or 'kr_stock')} 기준, 규칙 버전 {RULES_VERSION})", ""]
    for fam, label in FAMILIES.items():
        lines.append(f"[{label}]")
        if fam == "rotation":
            lines += [f"- {r.kind} · {r.label}: {r.rule}" for r in ROTATIONS.values()]
        else:
            for e in ENTRIES.values():
                if e.family == fam:
                    where = "모든 시장" if e.markets is None else ", ".join(MARKET_LABELS[m] for m in e.markets)
                    lines.append(f"- {e.kind} · {e.label} [{e.regime} · {where}]: {e.rule}")
            lines += [f"- 청산 {x.kind} · {x.label}: {x.rule}" for x in EXITS.values() if x.family == fam]
        lines.append("")
    return "\n".join(lines).rstrip()
