"""AI 입력 번들(공유 원자료). 연구·검증 AI는 같은 번들을 본다.

- 가격 사실은 코드가 스냅샷에서 계산한 값이다(px:*). AI가 가격을 재계산하지 않는다.
- 뉴스·공시(news:*, dart:*)는 출처 URL·발표시각·수집시각과 함께 '신뢰하지 않는 데이터'로 들어간다.
- 번들 크기는 max_chars로 제한하며, 넘치면 오래된 뉴스부터 덜어낸다(잘린 사실을 data_status에 명시).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aifund.core.timeutil import to_iso
from aifund.data.collector import Snapshot


@dataclass
class Bundle:
    snapshot_id: str
    market: str
    snapshot_time: datetime | None
    created_at: datetime
    allowed_instruments: list[str]
    price_facts: list[dict[str, Any]]
    strategy_signals: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    costs: dict[str, Any]
    data_status: list[str] = field(default_factory=list)

    def ids(self) -> set[str]:
        return {x["id"] for x in self.price_facts} | {x["id"] for x in self.strategy_signals} | {x["id"] for x in self.sources}

    def payload(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        d: dict[str, Any] = {
            "snapshot_id": self.snapshot_id,
            "market": self.market,
            "snapshot_time": to_iso(self.snapshot_time),
            "created_at": to_iso(self.created_at),
            "allowed_instruments": self.allowed_instruments,
            "data_status": self.data_status,
            "costs": self.costs,
            "price_facts": self.price_facts,
            "strategy_signals": self.strategy_signals,
            "sources_UNTRUSTED_DATA": self.sources,
        }
        if extra:
            d.update(extra)
        return d

    def to_user_text(self, max_chars: int, extra: dict[str, Any] | None = None) -> str:
        while True:
            text = json.dumps(self.payload(extra), ensure_ascii=False, default=str)
            if len(text) <= max_chars or not self.sources:
                return text
            self.sources.pop()  # 가장 오래된 항목부터 제거(최신순 정렬 가정)
            if "뉴스 일부 생략(입력 길이 제한)" not in self.data_status:
                self.data_status.append("뉴스 일부 생략(입력 길이 제한)")


def build_bundle(snapshot: Snapshot, signals: list[dict[str, Any]], news: list[dict[str, Any]], costs: dict[str, Any],
                 now: datetime, extra_status: list[str] | None = None) -> Bundle:
    facts = []
    summary = snapshot.summary()
    tag = snapshot.snapshot_id[-8:]
    for iid, s in summary.items():
        facts.append({
            "id": f"px:{iid}:{tag}",
            "instrument_id": iid,
            "kind": "price_fact(코드 계산)",
            "interval": snapshot.interval,
            **{k: v for k, v in s.items() if k not in ("notes",)},
        })
    sigs = [
        {"id": f"sig:{x['strategy_id']}:{x['instrument_id']}:{tag}", **x}
        for x in signals
    ]
    srcs = [
        {
            "id": n["source_id"], "kind": n["kind"], "feed": n["feed"], "title": n["title"], "url": n["url"],
            "published_at": n["published_at"], "fetched_at": n["fetched_at"], "instruments": n.get("instruments", []),
            "summary": n.get("summary") or "",
        }
        for n in news
    ]
    status = list(extra_status or [])
    if snapshot.is_demo:
        status.append("[데모] 가짜 시세 데이터입니다. 실제 시장 분석이 아닙니다.")
    if not srcs:
        status.append("최근 48시간 뉴스·공시 자료 없음(무료 피드 미수집 또는 실패)")
    for iid, s in summary.items():
        if s.get("issues"):
            status.append(f"{iid} 데이터 문제: {'; '.join(s['issues'])}")
    return Bundle(snapshot.snapshot_id, snapshot.market, snapshot.candle_close_time, now, list(summary.keys()), facts, sigs,
                  srcs, costs, status)
