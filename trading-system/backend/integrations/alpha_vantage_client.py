"""
Alpha Vantage API wrapper.
Fetches US market close prices (S&P 500, NASDAQ), Dollar Index (DXY), and Forex data.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from core.config import get_settings

logger = logging.getLogger(__name__)

BASE_URL = "https://www.alphavantage.co/query"
TIMEOUT = 10

# Module-level singleton — the Research Agent runs once at 6 AM but makes
# several sequential API calls (_query × 4 + Stooq × 1).  Reusing one client
# avoids spinning up a new TCP connection for each call.
_http_client: Optional[httpx.AsyncClient] = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=TIMEOUT)
    return _http_client


async def _query(function: str, symbol: str, **extra) -> Optional[Dict[str, Any]]:
    """Generic Alpha Vantage query helper."""
    settings = get_settings()
    params = {
        "function": function,
        "symbol": symbol,
        "apikey": settings.alpha_vantage_api_key,
        **extra,
    }
    try:
        client = _get_http_client()
        resp = await client.get(BASE_URL, params=params)
        resp.raise_for_status()
        data = resp.json()
        if "Error Message" in data or "Note" in data:
            logger.warning("Alpha Vantage response issue: %s", data)
            return None
        return data
    except Exception as exc:
        logger.error("Alpha Vantage request failed (%s %s): %s", function, symbol, exc)
        return None


async def fetch_us_market_close() -> Dict[str, Any]:
    """
    Fetch previous-day closing data for S&P 500 and NASDAQ composite.
    Returns dict with sp500_close_pct and nasdaq_close_pct.
    """
    result: Dict[str, Any] = {"sp500_close_pct": 0.0, "nasdaq_close_pct": 0.0}

    # S&P 500 via SPY ETF
    spy = await _query("GLOBAL_QUOTE", "SPY")
    if spy and "Global Quote" in spy:
        result["sp500_close_pct"] = float(spy["Global Quote"].get("10. change percent", "0").rstrip("%"))

    # NASDAQ via QQQ ETF
    qqq = await _query("GLOBAL_QUOTE", "QQQ")
    if qqq and "Global Quote" in qqq:
        result["nasdaq_close_pct"] = float(qqq["Global Quote"].get("10. change percent", "0").rstrip("%"))

    logger.info("US market close: %s", result)
    return result


async def fetch_dxy() -> Dict[str, Any]:
    """
    Fetch Dollar Index (DXY) current value and 5-day trend.
    Returns dict with value, trend, change_5d.
    """
    data = await _query("FX_DAILY", "USD", to_currency="EUR", outputsize="compact")
    result: Dict[str, Any] = {"value": 0.0, "trend": "FLAT", "change_5d": 0.0}

    if data and "Time Series FX (Daily)" in data:
        ts = data["Time Series FX (Daily)"]
        dates = sorted(ts.keys(), reverse=True)[:5]
        if len(dates) >= 2:
            latest = float(ts[dates[0]]["4. close"])
            oldest = float(ts[dates[-1]]["4. close"])
            change = ((latest - oldest) / oldest) * 100
            result["value"] = latest
            result["change_5d"] = round(change, 3)
            if change > 0.5:
                result["trend"] = "STRENGTHENING"
            elif change < -0.5:
                result["trend"] = "WEAKENING"

    logger.info("DXY data: %s", result)
    return result


async def fetch_sgx_nifty() -> Dict[str, Any]:
    """
    Fetch Nifty 50 previous-session data from Stooq as a proxy for SGX NIFTY direction.
    Stooq is free and requires no API key.
    Falls back to FLAT if the request fails.
    """
    result: Dict[str, Any] = {"value": 0.0, "change_pct": 0.0, "signal": "FLAT"}
    try:
        client = _get_http_client()
        resp = await client.get(
            "https://stooq.com/q/l/",
            params={"s": "^nsei", "f": "sd2t2ohlcvn", "h": "", "e": "csv"},
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        # CSV columns (with header flag): Symbol,Date,Time,Open,High,Low,Close,Volume,Name
        lines = [
            line for line in resp.text.strip().splitlines()
            if line and not line.startswith("Symbol")
        ]
        if lines:
            parts = lines[0].split(",")
            if len(parts) >= 7:
                open_p = float(parts[3])
                close_p = float(parts[6])
                if open_p > 0:
                    change_pct = round(((close_p - open_p) / open_p) * 100, 3)
                    result["value"] = close_p
                    result["change_pct"] = change_pct
                    if change_pct > 0.2:
                        result["signal"] = "GAP_UP"
                    elif change_pct < -0.2:
                        result["signal"] = "GAP_DOWN"
                    logger.info(
                        "Nifty50 via Stooq: close=%.2f change=%.3f%% signal=%s",
                        close_p, change_pct, result["signal"],
                    )
    except Exception as exc:
        logger.warning("Stooq Nifty50 fetch failed: %s — using FLAT", exc)
    return result
