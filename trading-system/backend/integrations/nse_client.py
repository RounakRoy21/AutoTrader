"""
NSE India API wrapper — fetches FII/DII trade data and corporate actions.
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
