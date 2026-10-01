"""무료 뉴스·공시 수집(RSS/Atom, 선택적으로 DART).

- 각 항목에 출처 URL, 발표 시각, 수집 시각, 관련 시장·종목을 남긴다.
- 외부 텍스트는 신뢰하지 않는 데이터다: 제어문자 제거·길이 제한 후 AI 입력의 '데이터' 영역에만 들어간다.
  어떤 텍스트도 코드 실행·주문·설정 변경 권한으로 이어지지 않는다(AI에는 도구가 없다).
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from defusedxml import ElementTree as ET

from aifund.config.settings import NewsFeed
from aifund.core.timeutil import UTC, Clock, parse_iso, to_iso
from aifund.db.database import Database, dumps, loads

log = logging.getLogger(__name__)

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAGS = re.compile(r"<[^>]+>")


def sanitize(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    t = _TAGS.sub(" ", text)
    t = html.unescape(t)
    t = _CTRL.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:limit]


def safe_url(u: str | None) -> str | None:
    """대시보드 링크로 쓰이므로 http/https만 허용(javascript: 등 차단)."""
    if not u:
        return None
    u = u.strip()
    return u[:1000] if u.lower().startswith(("http://", "https://")) else None


def _parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        return parsedate_to_datetime(s).astimezone(UTC)
    except (TypeError, ValueError):
        pass
    try:
        return parse_iso(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class FeedResult:
    feed: str
    ok: bool
    new_items: int
    error: str | None = None


class NewsCollector:
    def __init__(self, db: Database, clock: Clock, client: httpx.AsyncClient | None = None) -> None:
        self.db = db
        self.clock = clock
        self._client = client
        self.last_results: dict[str, FeedResult] = {}

    def _match(self, feed: NewsFeed, title: str, summary: str) -> list[str]:
        text = f"{title} {summary}".lower()
        hits = []
        for iid, words in feed.keywords.items():
            if any(re.search(r"\b" + re.escape(w.lower()) + r"\b", text) for w in words):
                hits.append(iid)
        return hits

    async def fetch_feed(self, feed: NewsFeed, max_items: int = 30) -> FeedResult:
        client = self._client or httpx.AsyncClient(timeout=15, follow_redirects=True)
        headers = {"User-Agent": feed.user_agent or "aifund-news/0.1 (personal research)"}
        try:
            r = await client.get(feed.url, headers=headers)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as exc:  # 네트워크·파싱 실패는 '자료 없음'으로 기록
            res = FeedResult(feed.name, False, 0, f"{exc.__class__.__name__}: {str(exc)[:120]}")
            self.last_results[feed.name] = res
            return res
        finally:
            if self._client is None:
                await client.aclose()
        now = self.clock.now()
        items = []
        # RSS 2.0
        for it in root.iter("item"):
            items.append((it.findtext("title"), it.findtext("link"), it.findtext("pubDate"), it.findtext("description"), it.findtext("guid")))
        # Atom
        ns = "{http://www.w3.org/2005/Atom}"
        for it in root.iter(f"{ns}entry"):
            link_el = it.find(f"{ns}link")
            link = link_el.get("href") if link_el is not None else None
            items.append((it.findtext(f"{ns}title"), link, it.findtext(f"{ns}updated") or it.findtext(f"{ns}published"),
                          it.findtext(f"{ns}summary"), it.findtext(f"{ns}id")))
        new = 0
        with self.db.tx() as c:
            for title, link, pub, desc, guid in items[:max_items]:
                title_s = sanitize(title, 300)
                if not title_s:
                    continue
                summary = sanitize(desc, 500)
                sid = "news:" + hashlib.sha256(f"{feed.url}|{guid or link or title_s}".encode()).hexdigest()[:12]
                published = _parse_date(pub)
                cur = c.execute(
                    "INSERT OR IGNORE INTO sources(source_id, kind, feed, title, url, published_at, fetched_at, market, instruments_json, summary) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, "news", feed.name, title_s, safe_url(link), to_iso(published), to_iso(now), ",".join(feed.markets),
                     dumps(self._match(feed, title_s, summary)), summary),
                )
                new += cur.rowcount
        res = FeedResult(feed.name, True, new)
        self.last_results[feed.name] = res
        return res

    async def fetch_naver(self, client_id: str, client_secret: str, query: str,
                          market: str, max_items: int = 30) -> FeedResult:
        name = f"네이버 {market}: {query}"
        client = self._client or httpx.AsyncClient(timeout=15)
        try:
            response = await client.get(
                "https://openapi.naver.com/v1/search/news.json",
                headers={"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret},
                params={"query": query, "display": min(100, max(1, max_items)), "sort": "date"},
            )
            response.raise_for_status()
            items = response.json()["items"]
            if not isinstance(items, list):
                raise ValueError("Invalid items")
            new = 0
            with self.db.tx() as c:
                for item in items[:max_items]:
                    if not isinstance(item, dict):
                        continue
                    title = sanitize(item.get("title"), 300)
                    url = safe_url(item.get("originallink")) or safe_url(item.get("link"))
                    published = _parse_date(item.get("pubDate"))
                    if not title or not url or published is None:
                        continue
                    sid = "naver:" + hashlib.sha256(f"{market}|{url}".encode()).hexdigest()[:20]
                    cur = c.execute(
                        "INSERT OR IGNORE INTO sources(source_id,kind,feed,title,url,published_at,fetched_at,market,instruments_json,summary) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (sid, "news", name, title, url, to_iso(published), to_iso(self.clock.now()),
                         market, "[]", sanitize(item.get("description"), 500)),
                    )
                    new += cur.rowcount
            result = FeedResult(name, True, new)
        except Exception as exc:
            # 요청 헤더·응답 본문·키 값은 남기지 않는다. 상태 코드만 기록(401 키 오류, 429 한도 초과 등).
            detail = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
            result = FeedResult(name, False, 0, detail)
        finally:
            if self._client is None:
                await client.aclose()
        self.last_results[name] = result
        return result

    async def fetch_dart(self, api_key: str, corp_codes: list[str] | None = None) -> FeedResult:
        """DART 공시 목록(opendart.fss.or.kr list.json). 키가 있을 때만 사용."""
        url = "https://opendart.fss.or.kr/api/list.json"
        today = self.clock.now().strftime("%Y%m%d")
        params = {"crtfc_key": api_key, "bgn_de": today, "end_de": today, "page_count": "50"}
        client = self._client or httpx.AsyncClient(timeout=15)
        try:
            r = await client.get(url, params=params)
            d = r.json()
        except Exception as exc:
            res = FeedResult("DART", False, 0, exc.__class__.__name__)
            self.last_results["DART"] = res
            return res
        finally:
            if self._client is None:
                await client.aclose()
        now = self.clock.now()
        new = 0
        with self.db.tx() as c:
            for it in d.get("list", []) or []:
                if corp_codes and it.get("stock_code") not in corp_codes:
                    continue
                sid = "dart:" + str(it.get("rcept_no"))
                link = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it.get('rcept_no')}"
                inst = [f"kr_stock:{it['stock_code']}"] if it.get("stock_code") else []
                pub = datetime.strptime(str(it.get("rcept_dt")), "%Y%m%d").replace(tzinfo=UTC) if it.get("rcept_dt") else None
                cur = c.execute(
                    "INSERT OR IGNORE INTO sources(source_id, kind, feed, title, url, published_at, fetched_at, market, instruments_json, summary) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sid, "disclosure", "DART", sanitize(f"{it.get('corp_name')} {it.get('report_nm')}", 300), link, to_iso(pub),
                     to_iso(now), "kr_stock", dumps(inst), ""),
                )
                new += cur.rowcount
        res = FeedResult("DART", str(d.get("status")) in ("000", "013"), new, None if str(d.get("status")) in ("000", "013") else d.get("message"))
        self.last_results["DART"] = res
        return res

    def recent(self, market: str, hours: int = 48, limit: int = 40) -> list[dict]:
        cutoff = to_iso(self.clock.now() - timedelta(hours=hours))
        rows = self.db.query(
            "SELECT * FROM sources WHERE kind IN ('news','disclosure') AND fetched_at >= ? AND market LIKE ? "
            "ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?",
            (cutoff, f"%{market}%", limit),
        )
        return [dict(r) | {"instruments": loads(r["instruments_json"], [])} for r in rows]
