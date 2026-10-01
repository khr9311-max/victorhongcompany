"""전략 변형 목록. 수십 개를 무작위로 만들지 않고 계열별 진입 × 청산 조합으로 구조화한다.

- 책 패턴: 책 진입 패턴(+대조군) × 책 청산 방식. 해석(setups.INTERPRETATIONS)마다 따로 만든다.
- 추세: 추세 진입(+대조군) × 추세 청산 방식
- 순환: 종목 간 상대강도 전략(+대조군). 포트폴리오 단위라 체결 가정은 현 시스템 방식 하나다.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from aifund.lab.engine import MODELS
from aifund.lab.exits import EXITS, ExitSpec
from aifund.lab.rotation import ROTATIONS, RotationSpec
from aifund.lab.setups import ENTRIES, INTERPRETATIONS, EntrySpec, entries_for

FAMILIES = {"pattern": "책 패턴", "trend": "추세", "rotation": "순환"}


@dataclass(frozen=True)
class Variant:
    family: str
    entry: EntrySpec | None = None
    exit: ExitSpec | None = None
    rotation: RotationSpec | None = None
    interp: str = "v1"  # 책 패턴 해석(다른 계열은 v1)

    @property
    def prefix(self) -> str:
        """v1이 아닌 해석은 id 앞에 '해석:'을 붙인다(대조군 짝 찾기에도 쓴다)."""
        return "" if self.interp == "v1" else f"{self.interp}:"

    @property
    def id(self) -> str:
        if self.rotation is not None:
            return self.rotation.kind
        assert self.entry is not None and self.exit is not None
        return f"{self.prefix}{self.entry.kind}.{self.exit.kind}"

    @property
    def label(self) -> str:
        if self.rotation is not None:
            return self.rotation.label
        assert self.entry is not None and self.exit is not None
        head = "" if self.interp == "v1" else f"[{INTERPRETATIONS[self.interp].label}] "
        return f"{head}{self.entry.label}·{self.exit.label}"

    @property
    def control(self) -> bool:
        if self.rotation is not None:
            return self.rotation.control
        assert self.entry is not None
        return self.entry.control

    @property
    def models(self) -> tuple[str, ...]:
        return ("bar_close",) if self.rotation is not None else MODELS


def variants(market: str, entries: list[str] | None = None, exits: list[str] | None = None,
             families: list[str] | None = None, interpretations: list[str] | None = None) -> list[Variant]:
    """시장에 맞는 변형. entries·exits를 주면 그 진입·청산만(순환 계열은 빠짐). 책 패턴은 해석마다 만든다."""
    for name in entries or []:
        if name not in ENTRIES:
            raise ValueError(f"알 수 없는 진입 {name} (가능: {', '.join(ENTRIES)})")
    for name in exits or []:
        if name not in EXITS:
            raise ValueError(f"알 수 없는 청산 방식 {name} (가능: {', '.join(EXITS)})")
    for name in families or []:
        if name not in FAMILIES:
            raise ValueError(f"알 수 없는 계열 {name} (가능: {', '.join(FAMILIES)})")
    for name in interpretations or []:
        if name not in INTERPRETATIONS:
            raise ValueError(f"알 수 없는 해석 {name} (가능: {', '.join(INTERPRETATIONS)})")
    fams = families or list(FAMILIES)
    out: list[Variant] = []
    for fam in ("pattern", "trend"):
        if fam not in fams:
            continue
        es = [e for e in entries_for(market, fam) if not entries or e.kind in entries]
        xs = [x for x in EXITS.values() if x.family == fam and (not exits or x.kind in exits)]
        for interp in (interpretations or ["v1"]) if fam == "pattern" else ["v1"]:
            out += [Variant(fam, e, x, interp=interp) for e in es for x in xs]
    if "rotation" in fams and not entries and not exits:
        out += [Variant("rotation", rotation=r) for r in ROTATIONS.values()]
    return out


@lru_cache(maxsize=1)
def _all() -> dict[str, Variant]:
    """시장·해석을 가리지 않은 전체 변형(국내주식 목록이 모든 진입을 포함한다)."""
    return {v.id: v for v in variants("kr_stock", interpretations=list(INTERPRETATIONS))}


def all_variants() -> list[Variant]:
    return list(_all().values())


def find(variant_id: str) -> Variant | None:
    return _all().get(variant_id)


def compatible(v: Variant, market: str) -> bool:
    """그 시장에서 쓸 수 있는 변형인가(빅 벨트는 갭이 있는 주식만)."""
    return v.entry is None or v.entry.markets is None or market in v.entry.markets
