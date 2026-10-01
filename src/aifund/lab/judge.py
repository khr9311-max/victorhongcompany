"""사전 등록한 판정 기준(docs/lab-preregistration.md)으로 두 시장의 연구소 보고서를 함께 판정한다.

대조군 = 같은 해석·같은 청산·같은 체결 가정의 무작위 진입. 비교는 거래 1회 평균 수익률의 웰치 t.
- 확실: 두 시장 모두 대조군보다 평균이 높고, 한 시장에서 t ≥ 그 실행의 본페로니 기준, 다른 시장에서 t ≥ 1.65,
        두 시장 모두 평가 구간 평균 > 0, 거래 30개 이상.
- 유망: 두 시장 모두 대조군 대비 t ≥ 1.65, 평가 구간 평균 > 0, 거래 30개 이상.
- 판단 불가: 어느 시장이든 거래 30개 미만.
- 실패: 그 밖.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MIN_TRADES = 30
T_CONSISTENT = 1.65
ORDER = ("확실", "유망", "판단 불가", "실패")


@dataclass
class Verdict:
    variant: str
    label: str
    model: str
    verdict: str
    a: dict[str, Any]  # 시장 A의 보고서 행
    b: dict[str, Any]


def load(path: Path) -> dict[str, Any]:
    p = path / "summary.json" if path.is_dir() else path
    return json.loads(p.read_text(encoding="utf-8"))


def _vs(row: dict[str, Any]) -> tuple[float, float] | None:
    vs = row.get("vs_control")
    if not vs or vs[0] is None or vs[1] is None:
        return None
    return float(vs[0]), float(vs[1])


def verdict(ra: dict[str, Any], rb: dict[str, Any], za: float, zb: float) -> str:
    if ra["all"]["n"] < MIN_TRADES or rb["all"]["n"] < MIN_TRADES:
        return "판단 불가"
    va, vb = _vs(ra), _vs(rb)
    ea, eb = ra["eval"]["mean"], rb["eval"]["mean"]
    if va is None or vb is None or ea is None or eb is None or ea <= 0 or eb <= 0:
        return "실패"
    (da, ta), (db, tb) = va, vb
    if da > 0 and db > 0 and ((ta >= za and tb >= T_CONSISTENT) or (tb >= zb and ta >= T_CONSISTENT)):
        return "확실"
    if ta >= T_CONSISTENT and tb >= T_CONSISTENT:
        return "유망"
    return "실패"


def judge(sa: dict[str, Any], sb: dict[str, Any]) -> list[Verdict]:
    """대조군 짝이 있는 변형(책 패턴·추세, 대조군 제외)을 두 시장에서 함께 판정한다."""
    rows_b = {(r["variant"], r["model"]): r for r in sb["rows"]}
    out = []
    for ra in sa["rows"]:
        rb = rows_b.get((ra["variant"], ra["model"]))
        if rb is None or ra["family"] == "rotation" or ra.get("control"):
            continue  # 대조군 자신·순환 계열은 판정 대상이 아님
        out.append(Verdict(ra["variant"], ra["label"], ra["model"],
                           verdict(ra, rb, float(sa["z_threshold"]), float(sb["z_threshold"])), ra, rb))
    out.sort(key=lambda v: (ORDER.index(v.verdict), -min((_vs(v.a) or (0, -9))[1], (_vs(v.b) or (0, -9))[1])))
    return out


def render(sa: dict[str, Any], sb: dict[str, Any], verdicts: list[Verdict], top: int = 15) -> str:
    from aifund.lab.engine import MODEL_LABELS
    from aifund.lab.report import MARKET_LABELS, num, pct, table

    ma, mb = MARKET_LABELS.get(sa["market"], sa["market"]), MARKET_LABELS.get(sb["market"], sb["market"])
    counts = {k: sum(1 for v in verdicts if v.verdict == k) for k in ORDER}
    lines = [f"사전 등록 판정: {ma} {sa['interval']}(시험 {sa['tests']}개, 기준 t ≥ {sa['z_threshold']:.2f}) × "
             f"{mb} {sb['interval']}(시험 {sb['tests']}개, 기준 t ≥ {sb['z_threshold']:.2f})",
             "판정 대상 " + str(len(verdicts)) + "개: " + ", ".join(f"{k} {n}" for k, n in counts.items()), ""]

    def cell(row: dict[str, Any]) -> str:
        vs = _vs(row)
        return "-" if vs is None else f"{pct(vs[0])} ({num(vs[1], 1)})"

    head = ["전략", "체결", "판정", f"{ma} 대조군 대비(t)", f"{mb} 대조군 대비(t)", f"{ma} 평가 평균(거래)",
            f"{mb} 평가 평균(거래)", f"{ma} 거래", f"{mb} 거래"]
    rows = [[v.label, MODEL_LABELS[v.model], v.verdict, cell(v.a), cell(v.b),
             f"{pct(v.a['eval']['mean'])} ({v.a['eval']['n']})", f"{pct(v.b['eval']['mean'])} ({v.b['eval']['n']})",
             str(v.a["all"]["n"]), str(v.b["all"]["n"])] for v in verdicts[:top]]
    lines += [f"[상위 {min(top, len(verdicts))}개 — 판정 순, 같은 판정 안에서는 두 시장 중 낮은 t 순]"] + table(head, rows, left=3)
    return "\n".join(lines)
