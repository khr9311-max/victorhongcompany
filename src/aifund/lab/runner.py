"""여러 종목 × 여러 변형 × 체결 가정 실행과 동일비중 포트폴리오 합산."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from aifund.lab.bars import Bars
from aifund.lab.catalog import Variant
from aifund.lab.engine import MODELS, Trade, buy_hold, simulate
from aifund.lab.exits import Costs
from aifund.lab.rotation import simulate_rotation
from aifund.lab.setups import INTERPRETATIONS, Context, scan


@dataclass
class LabRun:
    market: str
    interval: str
    instruments: list[str]
    timeline: list[datetime]  # 모든 종목 봉 마감 시각의 합집합
    split: datetime  # 개발/평가 구간 경계
    models: list[str]
    costs: Costs
    variants: list[Variant]
    trades: dict[tuple[str, str], list[Trade]] = field(default_factory=dict)  # (변형, 체결 가정) → 거래
    exposure: dict[tuple[str, str], float] = field(default_factory=dict)  # 평균 투입 비율
    curves: dict[tuple[str, str], list[float]] = field(default_factory=dict)  # 포트폴리오 자산(timeline, 시작 1.0)
    bh_curve: list[float] = field(default_factory=list)
    setup_counts: dict[str, int] = field(default_factory=dict)  # 진입 규칙별 신호 봉 수
    bars_total: int = 0


def default_split(timeline: list[datetime], frac: float = 0.7) -> datetime:
    """앞 70%를 개발 구간, 뒤 30%를 평가 구간으로 나눈다."""
    return timeline[min(len(timeline) - 1, int(len(timeline) * frac))]


def run_lab(bars_list: list[Bars], market: str, variants: list[Variant], costs: Costs, *, models: list[str] | None = None,
            split: datetime | None = None, progress: Callable[[str], None] | None = None) -> LabRun:
    if not bars_list:
        raise ValueError("시험할 종목 데이터가 없습니다")
    models = list(models or MODELS)
    timeline = sorted({t for b in bars_list for t in b.close_time})
    run = LabRun(market, bars_list[0].interval, [b.instrument_id for b in bars_list], timeline,
                 split or default_split(timeline), models, costs, variants)
    g_of = {t: g for g, t in enumerate(timeline)}
    n_inst = len(bars_list)
    keys = [(v.id, m) for v in variants for m in v.models if m in models]
    per_inst = [k for k in keys if next(v for v in variants if v.id == k[0]).rotation is None]
    acc: dict[tuple[str, str], list[float]] = {k: [0.0] * len(timeline) for k in per_inst}
    bh_acc = [0.0] * len(timeline)
    run.trades = {k: [] for k in keys}
    run.exposure = {k: 0.0 for k in keys}
    by_id = {v.id: v for v in variants}
    # (해석, 진입) 쌍. 신호 수는 'v1이면 진입 이름, 아니면 해석:진입'으로 센다
    pairs = {(v.interp, v.entry.kind): v.entry for v in variants if v.entry is not None}
    interps = {i for i, _ in pairs} | {"v1"}
    for k, bars in enumerate(bars_list, 1):
        if progress:
            progress(f"[{k}/{n_inst}] {bars.instrument_id} {len(bars)}봉")
        run.bars_total += len(bars)
        ctx = {i: Context.for_interp(bars, INTERPRETATIONS[i]) for i in interps}
        setups = {(i, kind): scan(ctx[i], spec) for (i, kind), spec in pairs.items()}
        for (i, kind), found in setups.items():
            name = kind if i == "v1" else f"{i}:{kind}"
            run.setup_counts[name] = run.setup_counts.get(name, 0) + len(found)
        # 종목 봉 → timeline 구간(다음 봉 직전까지 같은 값으로 채운다. 데이터 시작 전은 현금 1.0)
        idx = [g_of[t] for t in bars.close_time]
        spans = [(idx[i], idx[i + 1] if i + 1 < len(idx) else len(timeline)) for i in range(len(idx))]
        _add(bh_acc, buy_hold(ctx["v1"], costs), spans, idx[0])
        for vid, m in per_inst:
            v = by_id[vid]
            assert v.entry is not None and v.exit is not None
            res = simulate(ctx[v.interp], setups[(v.interp, v.entry.kind)], v.exit.kind, m, costs, variant=vid)
            run.trades[(vid, m)].extend(res.trades)
            run.exposure[(vid, m)] += res.exposure / n_inst
            _add(acc[(vid, m)], res.equity, spans, idx[0])
    run.curves = {key: [s / n_inst for s in a] for key, a in acc.items()}
    for v in variants:
        if v.rotation is not None and "bar_close" in models:
            if progress:
                progress(f"[순환] {v.label}")
            trades, curve, exposure = simulate_rotation(bars_list, v.rotation, costs, timeline, variant=v.id)
            run.trades[(v.id, "bar_close")] = trades
            run.curves[(v.id, "bar_close")] = curve
            run.exposure[(v.id, "bar_close")] = exposure
    run.bh_curve = [s / n_inst for s in bh_acc]
    return run


def _add(acc: list[float], values: list[float], spans: list[tuple[int, int]], first: int) -> None:
    for g in range(first):
        acc[g] += 1.0
    for v, (a, b) in zip(values, spans):
        for g in range(a, b):
            acc[g] += v
