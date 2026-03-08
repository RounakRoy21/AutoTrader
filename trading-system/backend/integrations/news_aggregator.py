"""
Hybrid News Aggregator — Real-time Indian financial news via RSS feeds.

Replaces NewsAPI.org, which has a 24-hour delay on the free tier — an
absolute dealbreaker for a pre-market trading system.  By the time a
delayed headline is readable, the market has already priced the information
in and acted on it.

Sources used here are all free, require no authentication, have no rate
limits, and update within 1–3 minutes of publication:

    • 5 Indian financial media RSS feeds:
        Economic Times Markets, Moneycontrol, Livemint,
        Business Standard, CNBC TV18

    • Google News RSS per-stock queries (one per watchlist symbol)

    • Google News RSS broad India market query

Each source is fetched concurrently via asyncio.gather with an individual
8-second timeout (asyncio.wait_for).  A failure or timeout on any single
source is logged and skipped — it never blocks the overall aggregation run.

Near-duplicate headlines are removed using difflib.SequenceMatcher (≥ 80 %
similarity threshold).  Only articles published in the last 24 hours are
included.  The final list is sorted newest-first.

Public interface:

    aggregator = HybridNewsAggregator()
    items = await aggregator.fetch_all(watchlist=["RELIANCE", "INFY", ...])
    await aggregator.check_feed_health()   # call once at 6:00 AM startup
"""

from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote

import feedparser
import httpx
import pytz
from pydantic import BaseModel

from core.redis_client import publish

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

_FETCH_TIMEOUT = 8          # seconds — per-source timeout
_FRESHNESS_HOURS = 24       # default freshness window (used by health check)
_PRE_MARKET_FRESHNESS_HOURS = 16  # pre-market run: 2 PM yesterday → 6 AM today
                                  # excludes yesterday's daytime articles that are
                                  # fully priced in — only overnight filings matter
_MAX_PER_FEED = 20          # entries taken per RSS feed (overnight feeds accumulate
                            # 14+ hours of items; 10 was missing early-evening BSE filings)
_MAX_PER_STOCK_QUERY = 5    # entries per Google News per-stock query
_MAX_BROAD_QUERY = 10       # entries for the broad Google News query
_DEDUP_THRESHOLD = 0.80     # SequenceMatcher ratio above which titles are duplicates
_OUTPUT_CAP = 60            # hard cap on returned NewsItems

GOOGLE_NEWS_BASE = "https://news.google.com/rss/search"

# Deduplication priority: earlier index wins when two headlines are near-identical.
_SOURCE_PRIORITY = [
    "economic_times",
    "moneycontrol",
    "livemint",
    "business_standard",
    "cnbctv18",
    "google_news",
]


# ── Schema ────────────────────────────────────────────────────────────────────

class NewsItem(BaseModel):
    title: str
    published: datetime
    link: str
    source: str            # e.g. "economic_times" or "google_news"
    stock_tag: str | None  # e.g. "INFY" when from a per-stock Google News query
    age_minutes: int       # minutes elapsed since published (computed at fetch time)


# ── Aggregator ────────────────────────────────────────────────────────────────

class HybridNewsAggregator:
    """Fetches and merges real-time Indian financial news from multiple sources."""

    FEEDS: dict[str, str] = {
        "economic_times":    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "moneycontrol":      "https://www.moneycontrol.com/rss/latestnews.xml",
        "livemint":          "https://www.livemint.com/rss/markets",
        "business_standard": "https://www.business-standard.com/rss/markets-106.rss",
        "cnbctv18":          "https://www.cnbctv18.com/rss/market.xml",
    }

    # NIFTY 50 symbol → human-readable Google News search term.
    # Using descriptive phrases instead of tickers avoids ambiguity
    # (e.g. "ITC" conflicts with non-financial results).
    _STOCK_SEARCH_TERMS: dict[str, str] = {
        # ── Financials ────────────────────────────────────────────────────────
        "HDFCBANK":   "HDFC Bank NSE",
        "ICICIBANK":  "ICICI Bank NSE",
        "KOTAKBANK":  "Kotak Mahindra Bank NSE",
        "SBIN":       "State Bank of India SBI NSE",
        "AXISBANK":   "Axis Bank NSE",
        "BAJFINANCE": "Bajaj Finance NSE",
        "BAJAJFINSV": "Bajaj Finserv NSE",
        "INDUSINDBK": "IndusInd Bank NSE",
        "HDFCLIFE":   "HDFC Life Insurance NSE",
        "SBILIFE":    "SBI Life Insurance NSE",
        # ── Information Technology ────────────────────────────────────────────
        "INFY":       "Infosys NSE",
        "TCS":        "Tata Consultancy Services TCS NSE",
        "WIPRO":      "Wipro NSE",
        "HCLTECH":    "HCL Technologies NSE",
        "TECHM":      "Tech Mahindra NSE",
        "LTIM":       "LTIMindtree NSE",
        # ── Consumer / FMCG ──────────────────────────────────────────────────
        "RELIANCE":   "Reliance Industries NSE",
        "HINDUNILVR": "Hindustan Unilever HUL NSE",
        "NESTLEIND":  "Nestle India NSE",
        "ITC":        "ITC Limited NSE stock",
        "BRITANNIA":  "Britannia Industries NSE",
        "TATACONSUM": "Tata Consumer Products NSE",
        "DABUR":      "Dabur India NSE",
        # ── Automobiles ──────────────────────────────────────────────────────
        "MARUTI":     "Maruti Suzuki NSE",
        "TATAMOTORS": "Tata Motors NSE",
        "M&M":        "Mahindra Mahindra NSE",
        "BAJAJ-AUTO": "Bajaj Auto NSE",
        "HEROMOTOCO": "Hero MotoCorp NSE",
        "EICHERMOT":  "Eicher Motors Royal Enfield NSE",
        # ── Metals & Mining ───────────────────────────────────────────────────
        "TATASTEEL":  "Tata Steel NSE",
        "JSWSTEEL":   "JSW Steel NSE",
        "HINDALCO":   "Hindalco Industries NSE",
        "COALINDIA":  "Coal India NSE",
        "VEDL":       "Vedanta NSE",
        # ── Energy & Utilities ────────────────────────────────────────────────
        "ONGC":       "ONGC Oil Natural Gas Corporation NSE",
        "BPCL":       "Bharat Petroleum BPCL NSE",
        "NTPC":       "NTPC NSE",
        "POWERGRID":  "Power Grid Corporation NSE",
        # ── Pharmaceuticals ───────────────────────────────────────────────────
        "SUNPHARMA":  "Sun Pharmaceutical NSE",
        "DRREDDY":    "Dr Reddy Laboratories NSE",
        "CIPLA":      "Cipla NSE",
        "DIVISLAB":   "Divis Laboratories NSE",
        # ── Cement & Construction ─────────────────────────────────────────────
        "ULTRACEMCO": "Ultratech Cement NSE",
        "GRASIM":     "Grasim Industries NSE",
        "LT":         "Larsen Toubro NSE",
        # ── Diversified / Others ─────────────────────────────────────────────
        "TITAN":      "Titan Company NSE",
        "ASIANPAINT": "Asian Paints NSE",
        "PIDILITIND": "Pidilite Industries NSE",
        "APOLLOHOSP": "Apollo Hospitals NSE",
        "BHARTIARTL": "Bharti Airtel NSE",
        "ADANIPORTS": "Adani Ports NSE",
        "ADANIENT":   "Adani Enterprises NSE",
    }

    # ── Parsing helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_published(entry: Any, now_ist: datetime) -> datetime:
        """Extract a timezone-aware IST datetime from a feedparser entry.

        feedparser sets `published_parsed` to a UTC time.struct_time.
        calendar.timegm (NOT time.mktime) converts it to a correct UTC
        epoch without being affected by the local system timezone.
        Falls back to current time so the article is not silently dropped.
        """
        parsed = getattr(entry, "published_parsed", None)
        if parsed:
            try:
                utc_ts = calendar.timegm(parsed)
                return datetime.fromtimestamp(utc_ts, tz=timezone.utc).astimezone(IST)
            except Exception:
                pass
        # Fallback: treat as current time (safe — won't be filtered as stale)
        return now_ist

    # ── Per-source fetch methods ──────────────────────────────────────────────

    async def _fetch_rss_feed(
        self,
        source_name: str,
        url: str,
        freshness_hours: int = _FRESHNESS_HOURS,
    ) -> list[NewsItem]:
        """Fetch one RSS feed via httpx (async I/O) and parse with feedparser (sync, fast)."""
        now_ist = datetime.now(IST)
        cutoff = now_ist - timedelta(hours=freshness_hours)

        try:
            async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
                resp = await client.get(url, follow_redirects=True, headers={
                    "User-Agent": "Mozilla/5.0 (compatible; AutoTrader/1.0; +research-agent)",
                })
                resp.raise_for_status()
                content = resp.text
        except httpx.TimeoutException:
            logger.warning("RSS feed timed out: %s (%s)", source_name, url)
            return []
        except Exception as exc:
            logger.warning("RSS feed fetch failed: %s — %s", source_name, exc)
            return []

        # feedparser.parse(string) is pure XML parsing — no I/O, safe to call sync
        try:
            feed = feedparser.parse(content)
        except Exception as exc:
            logger.warning("RSS feed parse failed: %s — %s", source_name, exc)
            return []

        items: list[NewsItem] = []
        for entry in (feed.entries or [])[:_MAX_PER_FEED]:
            title = getattr(entry, "title", None)
            if not title:
                continue
            published = self._parse_published(entry, now_ist)
            if published < cutoff:
                continue
            age_minutes = max(0, int((now_ist - published).total_seconds() / 60))
            items.append(NewsItem(
                title=title.strip(),
                published=published,
                link=getattr(entry, "link", ""),
                source=source_name,
                stock_tag=None,
                age_minutes=age_minutes,
            ))

        logger.debug("RSS %s: %d fresh items", source_name, len(items))
        return items

    async def _fetch_google_news(
        self,
        query: str,
        stock_tag: str | None,
        max_items: int,
        freshness_hours: int = _FRESHNESS_HOURS,
    ) -> list[NewsItem]:
        """Fetch Google News RSS for a given query string."""
        url = f"{GOOGLE_NEWS_BASE}?q={quote(query)}&hl=en-IN&gl=IN&ceid=IN:en"
        now_ist = datetime.now(IST)
        cutoff = now_ist - timedelta(hours=freshness_hours)

        try:
            async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT) as client:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
                content = resp.text
        except httpx.TimeoutException:
            logger.warning("Google News query timed out: %r", query)
            return []
        except Exception as exc:
            logger.warning("Google News query failed: %r — %s", query, exc)
            return []

        try:
            feed = feedparser.parse(content)
        except Exception as exc:
            logger.warning("Google News parse failed: %r — %s", query, exc)
            return []

        items: list[NewsItem] = []
        for entry in (feed.entries or [])[:max_items]:
            title = getattr(entry, "title", None)
            if not title:
                continue
            published = self._parse_published(entry, now_ist)
            if published < cutoff:
                continue
            age_minutes = max(0, int((now_ist - published).total_seconds() / 60))
            items.append(NewsItem(
                title=title.strip(),
                published=published,
                link=getattr(entry, "link", ""),
                source="google_news",
                stock_tag=stock_tag,
                age_minutes=age_minutes,
            ))

        return items

    # ── Deduplication ─────────────────────────────────────────────────────────

    @staticmethod
    def _source_rank(source: str) -> int:
        try:
            return _SOURCE_PRIORITY.index(source)
        except ValueError:
            return len(_SOURCE_PRIORITY)

    @staticmethod
    def _deduplicate(items: list[NewsItem]) -> list[NewsItem]:
        """Remove near-duplicate titles, keeping the highest-priority source version.

        Items are pre-sorted by source priority so the first occurrence of any
        near-duplicate cluster is always the one from the more authoritative outlet.
        O(n²) — acceptable for n ≤ _OUTPUT_CAP (~60 items).
        """
        sorted_items = sorted(
            items,
            key=lambda x: HybridNewsAggregator._source_rank(x.source),
        )
        kept: list[NewsItem] = []
        for candidate in sorted_items:
            t_c = candidate.title.lower()
            is_dup = any(
                SequenceMatcher(None, t_c, existing.title.lower()).ratio() >= _DEDUP_THRESHOLD
                for existing in kept
            )
            if not is_dup:
                kept.append(candidate)
        return kept

    # ── Public interface ──────────────────────────────────────────────────────

    async def fetch_all(self, watchlist: list[str] | None = None) -> list[NewsItem]:
        """Fetch from all sources concurrently and return a deduplicated, sorted list.

        Args:
            watchlist: NIFTY 50 symbols to run targeted Google News queries for.
                       Falls back to a default set of the 10 most liquid stocks
                       when omitted so the pre-market collection always gets
                       stock-specific coverage even before the brief is generated.
        """
        if not watchlist:
            watchlist = [
                "RELIANCE", "HDFCBANK", "INFY", "TCS", "ICICIBANK",
                "AXISBANK", "SBIN", "TATAMOTORS", "BAJFINANCE", "WIPRO",
            ]

        coros = []

        # RSS feeds — use tighter 16h window so Claude only sees overnight news
        # (articles from yesterday's trading session are already priced in)
        for source_name, url in self.FEEDS.items():
            coros.append(self._fetch_rss_feed(
                source_name, url, freshness_hours=_PRE_MARKET_FRESHNESS_HOURS
            ))

        # Per-stock Google News queries — fetch ALL material news for each stock,
        # not just earnings.  Appending "earnings results" was a mistake that hid
        # deal wins, SEBI actions, management changes, and block deals.
        for symbol in watchlist:
            term = self._STOCK_SEARCH_TERMS.get(symbol, f"{symbol} NSE stock")
            coros.append(self._fetch_google_news(
                query=term,
                stock_tag=symbol,
                max_items=_MAX_PER_STOCK_QUERY,
                freshness_hours=_PRE_MARKET_FRESHNESS_HOURS,
            ))

        # Broad India market query — macro context: RBI, SEBI, FII, global cues
        coros.append(self._fetch_google_news(
            query="NSE NIFTY RBI SEBI India stock market",
            stock_tag=None,
            max_items=_MAX_BROAD_QUERY,
            freshness_hours=_PRE_MARKET_FRESHNESS_HOURS,
        ))

        results = await asyncio.gather(*coros, return_exceptions=True)

        all_items: list[NewsItem] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("News source raised an exception: %s", result)
            elif isinstance(result, list):
                all_items.extend(result)

        deduped = self._deduplicate(all_items)
        deduped.sort(key=lambda x: x.published, reverse=True)

        logger.info(
            "HybridNewsAggregator: %d raw items → %d after dedup (from %d sources)",
            len(all_items), len(deduped), len(coros),
        )
        return deduped[:_OUTPUT_CAP]

    async def check_feed_health(self) -> None:
        """Verify each RSS feed is reachable and returning fresh entries.

        Called once at Research Agent startup (6:00 AM IST), not on every
        collection run.  A feed being down is logged and published to
        system_alerts so the dashboard operator can investigate, but it
        does NOT halt the system — the other sources continue normally.

        All feeds are checked concurrently so this completes in one
        round-trip time (≤ _FETCH_TIMEOUT seconds) rather than
        N × _FETCH_TIMEOUT seconds.
        """
        now_ts = datetime.now(IST).isoformat()
        feed_names = list(self.FEEDS.keys())
        feed_urls  = list(self.FEEDS.values())

        results = await asyncio.gather(
            *[self._fetch_rss_feed(name, url) for name, url in zip(feed_names, feed_urls)],
            return_exceptions=True,
        )

        for source_name, result in zip(feed_names, results):
            if isinstance(result, Exception):
                msg = f"News feed '{source_name}' health check failed: {result}"
                logger.warning(msg)
                await publish("system_alerts", {"type": "warning", "message": msg, "timestamp": now_ts})
            elif isinstance(result, list) and result:
                logger.info("Feed health OK: %s (%d items)", source_name, len(result))
            else:
                msg = f"News feed '{source_name}' returned 0 fresh entries — may be down or stale"
                logger.warning(msg)
                await publish("system_alerts", {"type": "warning", "message": msg, "timestamp": now_ts})
