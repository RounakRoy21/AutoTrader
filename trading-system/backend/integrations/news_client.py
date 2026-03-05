"""
NewsAPI.org wrapper — fetches top financial headlines relevant to Indian equity markets.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import httpx

from core.config import get_settings

logger = logging.getLogger(__name__)

NEWSAPI_URL = "https://newsapi.org/v2/everything"
DEFAULT_QUERY = "RBI OR NSE OR SEBI OR earnings OR NIFTY OR budget OR inflation India"
DEFAULT_PAGE_SIZE = 20
TIMEOUT = 10


async def fetch_market_news(
    query: str = DEFAULT_QUERY,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> List[Dict[str, Any]]:
    """
    Fetch top financial news headlines from NewsAPI.
    Returns a list of article dicts with title, description, source, publishedAt.
    """
    settings = get_settings()
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": page_size,
        "apiKey": settings.newsapi_api_key,
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.get(NEWSAPI_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
            articles = data.get("articles", [])
            logger.info("Fetched %d news articles from NewsAPI", len(articles))
            return [
                {
                    "title": a.get("title"),
                    "description": a.get("description"),
                    "source": a.get("source", {}).get("name"),
                    "published_at": a.get("publishedAt"),
                    "url": a.get("url"),
                }
                for a in articles
            ]
    except httpx.HTTPStatusError as exc:
        logger.error("NewsAPI HTTP error: %s", exc)
        return []
    except Exception as exc:
        logger.error("NewsAPI fetch failed: %s", exc)
        return []
