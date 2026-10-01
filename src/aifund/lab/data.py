"""연구소용 과거 캔들 수집·저장(조회 전용, 장부 DB와 분리).

- 저장 위치: var/<모드>/lab/candles/<봉>/<시장>/<종목>.csv. 모드별로 분리해 데모(가짜) 데이터가 실데이터와 섞이지 않는다.
- 국내·미국주식: 키움 조회 키(KIWOOM_DATA_*)가 있으면 키움 일봉(연속조회), 없으면 KIS 시세(최근 약 100봉만).
- 코인: 업비트 공개 시세(키 불필요). 원화 마켓 24시간 거래대금 상위 종목, 스테이블코인·유의 종목 제외.
- offline_demo: 결정적 가짜 시세(네트워크·키 사용 안 함).
기본 시험 종목은 '현재' 대형주라 생존편향이 있다(사라진 종목 제외). 보고서에 같은 경고를 남긴다.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from aifund.brokers.base import MarketData
from aifund.core.timeutil import UTC, parse_iso, to_iso, utcnow
from aifund.domain.models import Candle
from aifund.lab.bars import Bars

# 국내 대형주(유가증권시장 시가총액 상위권, 2026년 기준 선정)
KR_UNIVERSE = [
    "005930", "000660", "373220", "207940", "005380", "000270", "068270", "035420", "105560", "055550",
    "005490", "006400", "051910", "035720", "012330", "028260", "096770", "066570", "033780", "032830",
    "017670", "086790", "009150", "015760", "034730", "034020", "012450", "018260", "003550", "011200",
    "010950", "316140", "010130", "010140", "090430", "030200", "009540", "024110", "000810", "042660",
]
# 미국 대형주(키움 식별자: 거래소:티커)
US_UNIVERSE = [
    "NASD:AAPL", "NASD:MSFT", "NASD:NVDA", "NASD:AMZN", "NASD:GOOGL", "NASD:META", "NASD:TSLA", "NASD:AVGO",
    "NASD:COST", "NASD:NFLX", "NASD:AMD", "NASD:ADBE", "NASD:PEP", "NASD:CSCO", "NASD:INTC", "NASD:QCOM",
    "NASD:TXN", "NASD:AMAT", "NYSE:JPM", "NYSE:V", "NYSE:MA", "NYSE:UNH", "NYSE:XOM", "NYSE:JNJ", "NYSE:PG",
    "NYSE:HD", "NYSE:KO", "NYSE:BAC", "NYSE:CVX", "NYSE:LLY",
]
STABLECOINS = {"USDT", "USDC", "USDS", "USDE", "DAI", "TUSD", "BUSD", "FDUSD", "PYUSD", "USD1"}
COLUMNS = ["open_time", "close_time", "open", "high", "low", "close", "volume"]
# 원천별 한 번에 받을 수 있는 최대 봉 수. 키움 미국 일봉은 한 페이지 100봉 × 연속조회 20페이지(brokers/kiwoom.py)
MAX_BARS = {"kiwoom_us": 1990}


def default_symbols(market: str) -> list[str]:
    return {"kr_stock": KR_UNIVERSE, "us_stock": US_UNIVERSE}.get(market, [])


def bars_for_days(market: str, interval: str, days: int) -> int:
    """달력 일수 → 봉 수. 주식 일봉은 1년 약 252거래일(휴장 포함 여유 +5%)."""
    if interval == "1d":
        return days if market == "crypto" else int(days * 252 / 365 * 1.05) + 5
    return days * (1440 // int(interval[:-1]))


@dataclass
class LabStore:
    root: Path  # var/<모드>/lab

    def path(self, market: str, interval: str, symbol: str) -> Path:
        return self.root / "candles" / interval / market / (symbol.replace(":", "_") + ".csv")

    def meta_path(self, market: str, interval: str) -> Path:
        return self.root / "candles" / interval / market / "_meta.json"

    def save(self, candles: list[Candle], market: str, interval: str, symbol: str) -> int:
        """기존 파일과 open_time 기준으로 합쳐 저장. 저장된 봉 수를 돌려준다."""
        p = self.path(market, interval, symbol)
        rows: dict[str, list[str]] = {}
        if p.exists():
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    rows[r["open_time"]] = [r[c] for c in COLUMNS]
        for k in candles:
            ot = to_iso(k.open_time)
            assert ot is not None
            rows[ot] = [ot, to_iso(k.close_time) or "", str(k.open), str(k.high), str(k.low), str(k.close), str(k.volume)]
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(COLUMNS)
            for key in sorted(rows):
                w.writerow(rows[key])
        tmp.replace(p)
        return len(rows)

    def write_meta(self, market: str, interval: str, info: dict[str, Any]) -> None:
        p = self.meta_path(market, interval)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    def read_meta(self, market: str, interval: str) -> dict[str, Any]:
        p = self.meta_path(market, interval)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def symbols(self, market: str, interval: str) -> list[str]:
        d = self.root / "candles" / interval / market
        if not d.exists():
            return []
        out = [f.stem.replace("_", ":", 1) if market == "us_stock" else f.stem for f in sorted(d.glob("*.csv"))]
        return out

    def load(self, market: str, interval: str, symbol: str, *, since: datetime | None = None) -> Bars | None:
        p = self.path(market, interval, symbol)
        if not p.exists():
            return None
        ot, ct, o, h, lo, c = [], [], [], [], [], []
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                t0 = parse_iso(r["open_time"])
                assert t0 is not None
                if since is not None and t0 < since:
                    continue
                vals = [float(r[k]) for k in ("open", "high", "low", "close")]
                if min(vals) <= 0 or vals[1] < vals[2]:
                    continue  # 깨진 봉(0원·고가<저가)은 버린다
                ot.append(t0)
                ct.append(parse_iso(r["close_time"]))
                o.append(vals[0])
                h.append(vals[1])
                lo.append(vals[2])
                c.append(vals[3])
        if not c:
            return None
        return Bars(f"{market}:{symbol}", interval, ot, ct, o, h, lo, c)  # type: ignore[arg-type]


async def upbit_universe(top: int) -> list[str]:
    """업비트 원화 마켓 24시간 거래대금 상위(스테이블코인·유의 종목 제외)."""
    from aifund.brokers.upbit import UpbitHttp, _err

    http = UpbitHttp(scope="public")
    try:
        status, markets, _ = await http.request("GET", "/v1/market/all", params={"isDetails": "true"}, group="market")
        if status != 200:
            raise _err(status, markets)
        syms = []
        for m in markets:
            sym = m["market"]
            ev = m.get("market_event") or {}
            caution = ev.get("caution") or {}
            warned = bool(ev.get("warning")) or m.get("market_warning") == "CAUTION" or any(bool(v) for v in caution.values())
            if sym.startswith("KRW-") and sym.split("-", 1)[1] not in STABLECOINS and not warned:
                syms.append(sym)
        value: dict[str, float] = {}
        for i in range(0, len(syms), 100):
            st, tick, _ = await http.request("GET", "/v1/ticker", params={"markets": ",".join(syms[i:i + 100])}, group="ticker")
            if st != 200:
                raise _err(st, tick)
            for t in tick:
                value[t["market"]] = float(t.get("acc_trade_price_24h") or 0)
        return sorted(syms, key=lambda s: -value.get(s, 0))[:top]
    finally:
        await http.close()


async def fetch(store: LabStore, source: MarketData, market: str, interval: str, symbols: list[str], bars: int,
                *, source_label: str, demo: bool, progress: Any = None) -> dict[str, int]:
    """종목마다 과거 캔들을 받아 저장한다. 실패한 종목은 건너뛰고 결과에 -1로 남긴다."""
    from aifund.brokers.upbit import UpbitMarketData

    cap = MAX_BARS.get(source.source_name)
    if cap is not None and bars > cap:
        if progress:
            progress(f"  {source_label}은 한 번에 최대 {cap:,}봉(약 {cap / 252:.1f}년)까지 받습니다 → {cap:,}봉으로 줄여 수집")
        bars = cap
    insts = await source.instruments(market, symbols)
    out: dict[str, int] = {}
    for inst in insts:
        try:
            if isinstance(source, UpbitMarketData):
                cs = await source.history(inst, interval, bars)
            else:
                cs = await source.candles(inst, interval, bars)
            out[inst.symbol] = store.save([k for k in cs if k.close_time <= utcnow()], market, interval, inst.symbol)
        except Exception as exc:  # 한 종목 실패가 전체 수집을 막지 않게
            out[inst.symbol] = -1
            if progress:
                progress(f"  {inst.symbol}: 실패 {exc}")
            continue
        if progress:
            progress(f"  {inst.symbol}: {out[inst.symbol]}봉 저장")
    prev = store.read_meta(market, interval)
    store.write_meta(market, interval, {"source": source_label, "demo": demo, "fetched_at": to_iso(datetime.now(UTC)),
                                        "symbols": sorted(set(prev.get("symbols", [])) | {s for s, n in out.items() if n > 0})})
    return out
