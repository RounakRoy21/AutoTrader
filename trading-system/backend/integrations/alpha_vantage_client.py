"""
Market data integrations for the Research Agent.

Sources:
  - Alpha Vantage  : US market close (S&P 500, NASDAQ), Dollar Index (DXY)
  - Yahoo Finance  : Nifty 50 previous-session close (^NSEI), India VIX (^INDIAVIX),
                     WTI crude oil futures (CL=F), gold futures (GC=F)

All functions are async and share a single module-level httpx.AsyncClient.
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
# several sequential API calls (_query × 4 + Yahoo Finance × 2).  Reusing one
# client avoids spinning up a new TCP connection for each call.
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
    Fetch Nifty 50 previous-session close from Yahoo Finance (^NSEI) as a
    directional proxy for the overnight gap.

    At 6 AM IST this reflects yesterday's close vs previous close, which
    provides the baseline.  GIFT Nifty live pre-market futures are not
    available on any free API; this is the best freely available substitute.

    Falls back to {value: 0, change_pct: 0, signal: FLAT} on any failure.
    """
    result: Dict[str, Any] = {"value": 0.0, "change_pct": 0.0, "signal": "FLAT"}
    try:
        client = _get_http_client()
        resp = await client.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI",
            params={"interval": "1d", "range": "2d"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        meta = data["chart"]["result"][0]["meta"]
        price = float(meta.get("regularMarketPrice") or 0.0)
        prev_close = float(
            meta.get("previousClose") or meta.get("chartPreviousClose") or 0.0
        )
        if price > 0 and prev_close > 0:
            change_pct = round(((price - prev_close) / prev_close) * 100, 3)
            result["value"] = price
            result["change_pct"] = change_pct
            if change_pct > 0.2:
                result["signal"] = "GAP_UP"
            elif change_pct < -0.2:
                result["signal"] = "GAP_DOWN"
            logger.info(
                "Nifty50 via Yahoo Finance: close=%.2f change=%.3f%% signal=%s",
                price, change_pct, result["signal"],
            )
    except Exception as exc:
        logger.warning("Yahoo Finance Nifty50 fetch failed: %s — using FLAT", exc)
    return result


async def fetch_india_vix() -> Dict[str, Any]:
    """
    Fetch India VIX from Yahoo Finance (^INDIAVIX).
    India VIX measures 30-day implied volatility of the Nifty 50 options market.

    Regime interpretation used downstream:
      < 14  — LOW: very low vol / complacency
      14–20 — NORMAL: standard trading environment
      20–25 — ELEVATED: elevated anxiety, reduce position size
      > 25  — STRESS: crisis regime, avoid trading
    """
    result: Dict[str, Any] = {"value": 0.0, "regime": "UNKNOWN"}
    try:
        client = _get_http_client()
        resp = await client.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EINDIAVIX",
            params={"interval": "1d", "range": "1d"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        vix = float(
            data["chart"]["result"][0]["meta"].get("regularMarketPrice") or 0.0
        )
        if vix > 0:
            result["value"] = round(vix, 2)
            if vix < 14:
                result["regime"] = "LOW"
            elif vix <= 20:
                result["regime"] = "NORMAL"
            elif vix <= 25:
                result["regime"] = "ELEVATED"
            else:
                result["regime"] = "STRESS"
            logger.info("India VIX via Yahoo Finance: %.2f regime=%s", vix, result["regime"])
    except Exception as exc:
        logger.warning("Yahoo Finance India VIX fetch failed: %s — returning UNKNOWN", exc)
    return result


async def fetch_crude_oil() -> Dict[str, Any]:
    """
    Fetch WTI crude oil front-month futures from Yahoo Finance (CL=F).
    Returns spot price and % change vs previous close.

    NSE relevance:
      • Upstream energy stocks (ONGC, Oil India) benefit from higher crude.
      • Downstream consumers (BPCL, HPCL, IOC) are hurt by crude spikes.
      • Aviation (IndiGo) and paints/chemicals (Asian Paints, Pidilite) have
        high crude cost pass-through — spikes compress margins.
      • Rule of thumb: crude +2%  →  bearish for OMCs/aviation/paints,
        bullish for upstream producers; crude −2%  →  opposite.
    """
    result: Dict[str, Any] = {"price": 0.0, "change_pct": 0.0, "available": False}
    try:
        client = _get_http_client()
        resp = await client.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/CL%3DF",
            params={"interval": "1d", "range": "2d"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        meta = data["chart"]["result"][0]["meta"]
        price = float(meta.get("regularMarketPrice") or 0.0)
        prev_close = float(
            meta.get("previousClose") or meta.get("chartPreviousClose") or 0.0
        )
        if price > 0 and prev_close > 0:
            change_pct = round(((price - prev_close) / prev_close) * 100, 3)
            result["price"] = round(price, 2)
            result["change_pct"] = change_pct
            result["available"] = True
            logger.info("WTI Crude via Yahoo Finance: $%.2f change=%.3f%%", price, change_pct)
    except Exception as exc:
        logger.warning("Yahoo Finance crude oil fetch failed: %s", exc)
    return result


async def fetch_gold() -> Dict[str, Any]:
    """
    Fetch gold front-month futures from Yahoo Finance (GC=F).
    Returns spot price and % change vs previous close.

    NSE relevance:
      • Gold is a risk-off / fear indicator.  Sharp rallies (>1%) correlate
        with equity sell-offs — compress bias_confidence and lean BEARISH/NEUTRAL.
      • Jewellery stocks (Titan, Kalyan Jewellers) move with gold sentiment but
        can diverge on stock-specific news.
      • In geopolitical stress (US-Iran, Israel-Hamas, etc.), gold rising while
        DXY is also rising signals genuine safe-haven demand — more bearish for
        equities than gold rising alone.
    """
    result: Dict[str, Any] = {"price": 0.0, "change_pct": 0.0, "available": False}
    try:
        client = _get_http_client()
        resp = await client.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF",
            params={"interval": "1d", "range": "2d"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        meta = data["chart"]["result"][0]["meta"]
        price = float(meta.get("regularMarketPrice") or 0.0)
        prev_close = float(
            meta.get("previousClose") or meta.get("chartPreviousClose") or 0.0
        )
        if price > 0 and prev_close > 0:
            change_pct = round(((price - prev_close) / prev_close) * 100, 3)
            result["price"] = round(price, 2)
            result["change_pct"] = change_pct
            result["available"] = True
            logger.info("Gold Futures via Yahoo Finance: $%.2f change=%.3f%%", price, change_pct)
    except Exception as exc:
        logger.warning("Yahoo Finance gold fetch failed: %s", exc)
    return result
