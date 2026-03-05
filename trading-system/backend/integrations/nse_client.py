"""
NSE India API wrapper — fetches FII/DII trade data.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import httpx

logger = logging.getLogger(__name__)

NSE_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
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
