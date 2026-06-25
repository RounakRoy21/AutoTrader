"""
RBI client — macro / monetary-policy context via the Reserve Bank of India RSS feeds.

RBI is the single most important domestic macro driver for Indian equities: repo-rate
decisions, liquidity / OMO operations, CRR changes, regulatory penalties on banks/NBFCs,
and forex actions all move the market.  These signals are NOT NSE data, so they live in
their own integration module rather than in nse_client.py.

Both feeds are free, unauthenticated, and update within minutes of publication:

    • Press releases: https://www.rbi.org.in/pressreleases_rss.xml
        (policy, auctions, penalties, governor statements)
    • Notifications:  https://www.rbi.org.in/notifications_rss.xml
        (circulars, regulatory directions)

The fetch is best-effort: a failure or timeout on either feed is logged and skipped —
it never blocks the overall pre-market collection run.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import feedparser
import httpx
import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

_FETCH_TIMEOUT = 8          # seconds — per-feed timeout
_LOOKBACK_HOURS = 48        # policy/regulatory items stay relevant for a few days
_MAX_PER_FEED = 10          # entries taken per feed before filtering
_OUTPUT_CAP = 12            # hard cap on returned items (keeps the LLM prompt bounded)

RBI_FEEDS: Dict[str, str] = {
    "press_release": "https://www.rbi.org.in/pressreleases_rss.xml",
    "notification":  "https://www.rbi.org.in/notifications_rss.xml",
}

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AutoTrader/1.0; +research-agent)",
}


def _parse_published(entry: Any, now_ist: datetime) -> datetime:
    """Extract a timezone-aware IST datetime from a feedparser entry.

    feedparser sets `published_parsed` to a UTC time.struct_time.  calendar.timegm
    (NOT time.mktime) converts it to a correct UTC epoch irrespective of the local
    system timezone.  Falls back to current time so the item is not dropped as stale.
    """
    parsed = getattr(entry, "published_parsed", None)
    if parsed:
        try:
            utc_ts = calendar.timegm(parsed)
            return datetime.fromtimestamp(utc_ts, tz=timezone.utc).astimezone(IST)
        except Exception:
            pass
    return now_ist


async def _fetch_feed(category: str, url: str, cutoff: datetime, now_ist: datetime) -> List[Dict[str, Any]]:
    """Fetch one RBI RSS feed via httpx (async) and parse with feedparser (sync)."""
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, headers=_HEADERS) as client:
            resp = await client.get(url, follow_redirects=True)
            resp.raise_for_status()
            content = resp.text
    except httpx.TimeoutException:
        logger.warning("RBI feed timed out: %s (%s)", category, url)
        return []
    except Exception as exc:
        logger.warning("RBI feed fetch failed: %s — %s", category, exc)
        return []

    try:
        feed = feedparser.parse(content)
    except Exception as exc:
        logger.warning("RBI feed parse failed: %s — %s", category, exc)
        return []

    items: List[Dict[str, Any]] = []
    for entry in (feed.entries or [])[:_MAX_PER_FEED]:
        title = getattr(entry, "title", None)
        if not title:
            continue
        published = _parse_published(entry, now_ist)
        if published < cutoff:
            continue
        age_hours = max(0, int((now_ist - published).total_seconds() / 3600))
        items.append({
            "title": title.strip(),
            "category": category,
            "published": published.isoformat(),
            "link": getattr(entry, "link", ""),
            "age_hours": age_hours,
        })
    return items


async def fetch_rbi_updates(lookback_hours: int = _LOOKBACK_HOURS) -> List[Dict[str, Any]]:
    """
    Fetch recent RBI press releases and notifications for macro-policy context.

    Returns a list of:
        {"title", "category", "published" (ISO IST), "link", "age_hours"}
    sorted newest-first and capped at _OUTPUT_CAP.  Returns [] on total failure —
    it never raises, so a bad RBI day cannot break the pre-market brief.
    """
    now_ist = datetime.now(IST)
    cutoff = now_ist - timedelta(hours=lookback_hours)

    results = await asyncio.gather(
        *[_fetch_feed(cat, url, cutoff, now_ist) for cat, url in RBI_FEEDS.items()],
        return_exceptions=True,
    )

    items: List[Dict[str, Any]] = []
    for result in results:
        if isinstance(result, Exception):
            logger.warning("RBI feed raised an exception: %s", result)
        elif isinstance(result, list):
            items.extend(result)

    items.sort(key=lambda x: x["published"], reverse=True)
    logger.info("RBI updates: %d items in last %dh", len(items), lookback_hours)
    return items[:_OUTPUT_CAP]
