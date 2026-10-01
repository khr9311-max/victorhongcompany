"""대시보드 차트 데이터. 서버는 원장·평가 기록에서 숫자만 계산하고, 그림은 static/app.js가 그린다.

- 평가 기록(equity_snapshots)은 1분마다 쌓이므로 기간을 구간으로 나눠 구간마다 마지막 값만 쓴다(최대 약 240점).
- 원금이 바뀐 시점(모의 원금 조정·LIVE 배정) 이전 기록은 비교 기준이 달라 그리지 않는다.
- 모든 차트에는 같은 값을 담은 표(table view)를 함께 제공한다.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from aifund.core.money import D, ZERO
from aifund.core.timeutil import parse_iso, to_iso
from aifund.web.labels import BOOK_LABELS

MAX_POINTS = 240
TABLE_ROWS = 24
# A/B/C 비교 차트의 색 슬롯(장부마다 고정 — 필터·누락이 있어도 색이 바뀌지 않게)과 끝점 라벨
COMPARE_BOOKS = (("shadow_A", 1), ("shadow_B", 2), ("shadow_C", 3), ("baseline_bh", 4))
SHORT_LABELS = {"shadow_A": "A", "shadow_B": "B", "shadow_C": "C", "baseline_bh": "매수·보유"}


def spec_json(spec: dict[str, Any]) -> str:
    """<script type="application/json"> 안에 넣을 JSON(그리기에 필요한 값만). '</'를 끊어 스크립트 영역 탈출을 막는다.
    표(table)는 서버가 HTML로 직접 그리므로 보내지 않는다."""
    data = {k: spec[k] for k in ("format", "area", "series", "reference") if k in spec}
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=float).replace("</", "<\\/")


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _window(ctx: Any, book_ids: list[str], days: int) -> tuple[datetime, int]:
    now = ctx.clock.now()
    since = now - timedelta(days=days)
    marks = ",".join("?" for _ in book_ids)
    last_change = ctx.db.scalar(f"SELECT MAX(ts) FROM ledger_entries WHERE kind='principal' AND book_id IN ({marks})",
                                tuple(book_ids))
    if last_change:
        since = max(since, parse_iso(last_change))  # type: ignore[type-var]
    span_days = max((now - since).total_seconds() / 86400, 1 / 24)
    return since, max(1, int(MAX_POINTS / span_days))


def _points(ctx: Any, book_id: str, since: datetime, buckets_per_day: int) -> list[tuple[datetime, Decimal]]:
    rows = ctx.db.query(
        "SELECT ts, equity_krw FROM equity_snapshots WHERE id IN (SELECT MAX(id) FROM equity_snapshots "
        "WHERE book_id=? AND stale=0 AND ts>=? GROUP BY CAST((julianday(ts) - julianday(?)) * ? AS INTEGER)) ORDER BY ts",
        (book_id, to_iso(since), to_iso(since), buckets_per_day))
    return [(parse_iso(r["ts"]), D(r["equity_krw"])) for r in rows]  # type: ignore[misc]


def _principal_steps(ctx: Any, book_id: str) -> tuple[list[datetime], list[Decimal]]:
    times, totals, total = [], [], ZERO
    for r in ctx.db.query("SELECT ts, delta FROM ledger_entries WHERE book_id=? AND kind='principal' ORDER BY ts, id", (book_id,)):
        total += D(r["delta"])
        times.append(parse_iso(r["ts"]))
        totals.append(total)
    return times, totals  # type: ignore[return-value]


def _sample(n: int) -> list[int]:
    """표로 보여 줄 행 번호(최대 TABLE_ROWS, 마지막 점 포함)."""
    if n <= TABLE_ROWS:
        return list(range(n))
    step = n / (TABLE_ROWS - 1)
    idx = sorted({min(n - 1, round(i * step)) for i in range(TABLE_ROWS - 1)} | {n - 1})
    return idx


def equity_chart(ctx: Any, book_id: str = "operating", days: int = 7) -> dict[str, Any]:
    """운용 장부 평가자산 추이(원금 기준선 포함)."""
    since, bpd = _window(ctx, [book_id], days)
    pts = _points(ctx, book_id, since, bpd)
    principal = ctx.ledger.principal(book_id)
    return {
        "format": "krw", "area": True, "since": to_iso(since),
        "series": [{"key": book_id, "label": "평가자산", "slot": 1, "points": [[_ms(t), float(v)] for t, v in pts]}],
        "reference": {"value": float(principal), "label": "원금"} if principal > 0 else None,
        "table": [{"t": to_iso(pts[i][0]), "values": [pts[i][1]]} for i in _sample(len(pts))],
        "columns": ["평가자산"],
    }


def compare_chart(ctx: Any, days: int = 30) -> dict[str, Any]:
    """A/B/C·매수보유 장부의 원금 대비 수익률(%) 추이. 현금 유지 장부는 0% 기준선."""
    books = [(b, slot) for b, slot in COMPARE_BOOKS if ctx.ledger.book(b) is not None]
    since, bpd = _window(ctx, [b for b, _ in books] or ["-"], days)
    series = []
    by_time: dict[int, dict[str, float]] = {}
    for book_id, slot in books:
        times, totals = _principal_steps(ctx, book_id)
        pts = []
        for t, eq in _points(ctx, book_id, since, bpd):
            i = bisect_right(times, t)
            principal = totals[i - 1] if i else ZERO
            if principal > 0:
                value = float((eq / principal - 1) * 100)
                pts.append([_ms(t), value])
                by_time.setdefault(_ms(t), {})[book_id] = value
        series.append({"key": book_id, "label": BOOK_LABELS.get(book_id, book_id), "short": SHORT_LABELS.get(book_id, book_id),
                       "slot": slot, "points": pts})
    stamps = sorted(by_time)
    table = [{"t": to_iso(datetime.fromtimestamp(stamps[i] / 1000, tz=since.tzinfo)),
              "values": [by_time[stamps[i]].get(b) for b, _ in books]} for i in _sample(len(stamps))]
    return {"format": "pct", "area": False, "since": to_iso(since), "series": series,
            "reference": {"value": 0, "label": "현금 유지 0%"}, "table": table,
            "columns": [BOOK_LABELS.get(b, b) for b, _ in books]}
