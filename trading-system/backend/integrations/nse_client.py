"""
NSE India API wrapper — fetches FII/DII trade data, corporate actions, and live indices.

All endpoints use the same anti-scraping workaround: hit the homepage first so
NSE sets a valid session cookie, then hit the JSON API endpoint.  The market-data
pages are JavaScript-rendered — there is no HTML table to parse.  The page's own
JS calls the same JSON endpoints we use here.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

import httpx
import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

NSE_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
NSE_CORP_ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions"
NSE_ALL_INDICES_URL = "https://www.nseindia.com/api/allIndices"
TIMEOUT = 10

# NSE requires specific headers to avoid 403
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

# Indices extracted from the allIndices response that are relevant to the
# research agent.  Broad-market, sector, and volatility indices only.
_KEY_INDICES = {
    "NIFTY 50",
    "INDIA VIX",
    "NIFTY BANK",
    "NIFTY IT",
    "NIFTY PHARMA",
    "NIFTY AUTO",
    "NIFTY FMCG",
    "NIFTY METAL",
    "NIFTY ENERGY",
    "NIFTY REALTY",
    "NIFTY PSU BANK",
    "NIFTY PRIVATE BANK",
    "NIFTY FINANCIAL SERVICES",
}


def _safe_float(value: Any) -> float:
    """Convert an NSE API value to float, returning 0.0 on any failure.

    NSE sends numbers as plain floats, but edge cases include None, "-", or
    comma-formatted strings like "24,378.10" in some legacy endpoints.
    """
    try:
        if value is None or value == "-" or value == "":
            return 0.0
        if isinstance(value, str):
            value = value.replace(",", "")
        return float(value)
    except (ValueError, TypeError):
        return 0.0


async def fetch_fii_dii_data() -> Dict[str, Any]:
    """
    Fetch FII/DII net buy/sell data from the NSE India API.
    Returns dict with fii_net_crore, dii_net_crore, and a signal.
    """
    result: Dict[str, Any] = {
        "fii_net_crore": 0.0,
        "dii_net_crore": 0.0,
        "signal": "NEUTRAL",
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
            # First hit the home page to get cookies
            await client.get("https://www.nseindia.com/")
            resp = await client.get(NSE_FII_DII_URL)
            resp.raise_for_status()
            data = resp.json()

            # Parse FII/DII values from the response
            for entry in data:
                category = entry.get("category", "")
                net_value = float(entry.get("netValue", "0").replace(",", ""))
                if "FII" in category.upper() or "FPI" in category.upper():
                    result["fii_net_crore"] = net_value
                elif "DII" in category.upper():
                    result["dii_net_crore"] = net_value

            # Determine signal
            fii = result["fii_net_crore"]
            dii = result["dii_net_crore"]
            if fii > 500 and dii > 0:
                result["signal"] = "LEAN_LONG"
            elif fii < -500:
                result["signal"] = "LEAN_SHORT"
            else:
                result["signal"] = "NEUTRAL"

            logger.info("FII/DII data: %s", result)

    except httpx.HTTPStatusError as exc:
        logger.error("NSE API HTTP error: %s", exc)
    except Exception as exc:
        logger.error("NSE FII/DII fetch failed: %s", exc)

    return result


async def fetch_corporate_actions_today() -> List[str]:
    """
    Fetch NSE equity corporate actions whose ex-date is today.

    Returns a deduplicated list of stock symbols (e.g. ["HDFCBANK", "INFY"]).

    Stocks with today as their ex-date have their opening price adjusted by the
    exchange for the event (dividend, split, bonus, rights, etc.).  This means:
    - VWAP calculation becomes meaningless (opening reference is adjusted)
    - RSI and other momentum indicators trigger false signals
    - Volume patterns are distorted by arbitrage / dividend stripping

    These symbols are mechanically forced into 'avoid_today' in the market brief
    regardless of what the LLM recommends.
    """
    today = datetime.now(IST).date()
    # NSE API accepts dates as "DD-Mon-YYYY" (e.g. "28-Dec-2023")
    today_str = today.strftime("%d-%b-%Y")
    url = f"{NSE_CORP_ACTIONS_URL}?index=equities&from_date={today_str}&to_date={today_str}"

    symbols: List[str] = []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
            # Hit home page first so NSE sets the session cookie — without it the
            # API endpoint returns 403.
            await client.get("https://www.nseindia.com/")
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

            for entry in data:
                ex_date_str = entry.get("exDate", "")
                symbol = entry.get("symbol", "").strip().upper()
                if not symbol or not ex_date_str:
                    continue
                try:
                    ex_date = datetime.strptime(ex_date_str, "%d-%b-%Y").date()
                    if ex_date == today:
                        symbols.append(symbol)
                except ValueError:
                    logger.debug(
                        "Could not parse exDate '%s' for symbol %s",
                        ex_date_str, symbol,
                    )

            # Deduplicate while preserving insertion order (a stock can appear
            # multiple times if it has several corporate events on the same day).
            symbols = list(dict.fromkeys(symbols))

            if symbols:
                logger.info(
                    "Corporate action ex-dates today (%s): %s", today_str, symbols
                )
            else:
                logger.info("No corporate action ex-dates today (%s)", today_str)

    except httpx.HTTPStatusError as exc:
        logger.error("NSE corp actions HTTP error: %s", exc)
    except Exception as exc:
        logger.error("NSE corp actions fetch failed: %s", exc)

    return symbols


async def fetch_nse_indices() -> Dict[str, Any]:
    """
    Fetch live market indices from the NSE India JSON API.

    The nseindia.com/market-data/live-market-indices page is JavaScript-rendered
    and has no scrapable HTML table.  Its JS calls this endpoint internally.
    We use the same homepage-cookie pattern as fetch_fii_dii_data().

    Returns
    -------
    dict with keys:
        available (bool)        — False if the request failed
        timestamp (str | None)  — "22-Apr-2026 15:29:52" or None
        nifty50 (dict | None):
            current, previous_close, percent_change, open, high, low,
            year_high, year_low
        india_vix (dict | None):
            value, percent_change, previous_close, regime
            regime: LOW (<14) | NORMAL (14-20) | ELEVATED (20-25) | STRESS (>25)
        sector_indices (dict[str, dict]):
            keyed by index name (e.g. "NIFTY BANK"), each entry has
            current, previous_close, percent_change
    """
    result: Dict[str, Any] = {
        "available": False,
        "timestamp": None,
        "nifty50": None,
        "india_vix": None,
        "sector_indices": {},
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
            # Establish session cookie — NSE blocks unauthenticated API calls.
            await client.get("https://www.nseindia.com/")
            resp = await client.get(NSE_ALL_INDICES_URL)
            resp.raise_for_status()
            data = resp.json()

        result["available"] = True
        result["timestamp"] = data.get("timestamp")

        for entry in data.get("data", []):
            name = (entry.get("index") or entry.get("indexSymbol") or "").strip()
            if name not in _KEY_INDICES:
                continue

            current = _safe_float(entry.get("current"))
            prev_close = _safe_float(entry.get("previousClose"))
            pct_change = _safe_float(entry.get("percentChange"))

            if name == "NIFTY 50":
                result["nifty50"] = {
                    "current": current,
                    "previous_close": prev_close,
                    "percent_change": pct_change,
                    "open": _safe_float(entry.get("open")),
                    "high": _safe_float(entry.get("high")),
                    "low": _safe_float(entry.get("low")),
                    "year_high": _safe_float(entry.get("yearHigh")),
                    "year_low": _safe_float(entry.get("yearLow")),
                }

            elif name == "INDIA VIX":
                vix = current
                if vix < 14:
                    regime = "LOW"
                elif vix <= 20:
                    regime = "NORMAL"
                elif vix <= 25:
                    regime = "ELEVATED"
                elif vix > 25:
                    regime = "STRESS"
                else:
                    regime = "UNKNOWN"
                result["india_vix"] = {
                    "value": round(vix, 2),
                    "percent_change": pct_change,
                    "previous_close": prev_close,
                    "regime": regime,
                }

            else:
                result["sector_indices"][name] = {
                    "current": current,
                    "percent_change": pct_change,
                    "previous_close": prev_close,
                }

        logger.info(
            "NSE allIndices: Nifty50=%.2f VIX=%.2f (regime=%s) timestamp=%s",
            result["nifty50"]["current"] if result["nifty50"] else 0.0,
            result["india_vix"]["value"] if result["india_vix"] else 0.0,
            result["india_vix"]["regime"] if result["india_vix"] else "N/A",
            result["timestamp"],
        )

    except httpx.HTTPStatusError as exc:
        logger.error("NSE allIndices HTTP error: %s", exc)
    except Exception as exc:
        logger.error("NSE allIndices fetch failed: %s", exc)

    return result
