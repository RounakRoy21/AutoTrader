"""
Market data integrations for the Research Agent.

Sources:
  - Alpha Vantage  : US market close (S&P 500, NASDAQ via SPY/QQQ ETFs)
  - Yahoo Finance  : Dollar Index (DX-Y.NYB), USD/INR (USDINR=X),
                     Nifty 50 previous-session close (^NSEI), India VIX (^INDIAVIX),
                     WTI crude oil futures (CL=F), gold futures (GC=F)

DXY source rationale
--------------------
The previous implementation used Alpha Vantage EUR/USD (FX_DAILY) as a DXY proxy.
EUR is only 57.6 % of the ICE Dollar Index basket.  On days when JPY (13.6 %),
GBP (11.9 %), or CAD (9.1 %) move sharply — BoJ interventions, UK budget days,
Bank of Canada meetings — the EUR/USD trend diverges from true DXY direction.
Yahoo Finance publishes the actual ICE DXY futures contract as DX-Y.NYB using
the same unofficial endpoint pattern we use for crude oil and gold.  The
EUR/USD proxy is retained as a fallback only.

All functions are async and share a single module-level httpx.AsyncClient.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

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
    Fetch previous-session performance for S&P 500 and NASDAQ Composite.

    Source: Yahoo Finance chart endpoint (^GSPC and ^IXIC).
    Replaced the original Alpha Vantage GLOBAL_QUOTE (SPY/QQQ ETFs) to eliminate
    the Alpha Vantage API key dependency for this endpoint.  Yahoo Finance returns
    the actual index level rather than an ETF proxy, which is directionally
    identical and removes one of only 2 Alpha Vantage calls per research-agent run.

    Returns dict with sp500_close_pct and nasdaq_close_pct (percentage changes).
    """
    result: Dict[str, Any] = {"sp500_close_pct": 0.0, "nasdaq_close_pct": 0.0}

    async def _fetch_index(ticker: str) -> float:
        client = _get_http_client()
        resp = await client.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "2d"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        meta = resp.json()["chart"]["result"][0]["meta"]
        price = float(meta.get("regularMarketPrice") or 0.0)
        prev = float(meta.get("previousClose") or meta.get("chartPreviousClose") or 0.0)
        if price > 0 and prev > 0:
            return round((price - prev) / prev * 100, 3)
        return 0.0

    try:
        sp500, nasdaq = await asyncio.gather(
            _fetch_index("%5EGSPC"),   # S&P 500 index (actual, not ETF proxy)
            _fetch_index("%5EIXIC"),   # NASDAQ Composite
            return_exceptions=True,
        )
        result["sp500_close_pct"] = sp500 if not isinstance(sp500, Exception) else 0.0
        result["nasdaq_close_pct"] = nasdaq if not isinstance(nasdaq, Exception) else 0.0
    except Exception as exc:
        logger.error("US market close fetch failed: %s", exc)

    logger.info("US market close: %s", result)
    return result


async def fetch_dxy() -> Dict[str, Any]:
    """
    Fetch the ICE US Dollar Index (DXY) value and 5-day trend.

    Primary source: Yahoo Finance DX-Y.NYB — the actual ICE DXY futures contract.
    Covers the full basket: EUR 57.6%, JPY 13.6%, GBP 11.9%, CAD 9.1%,
    SEK 4.2%, CHF 3.6%.  Value is in the familiar ~95–110 range.

    Fallback: Alpha Vantage EUR/USD (FX_DAILY).  Directionally correct ~85% of
    the time but misses JPY/GBP/CAD-driven moves.  Value in this path is the
    raw USD/EUR rate (~0.90–0.95), still usable for trend direction only.

    Returns dict with value, trend (STRENGTHENING/WEAKENING/FLAT), change_5d.
    """
    result: Dict[str, Any] = {"value": 0.0, "trend": "FLAT", "change_5d": 0.0}

    # ── Primary: Yahoo Finance DX-Y.NYB (real ICE DXY futures) ───────────────
    try:
        client = _get_http_client()
        resp = await client.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/DX-Y.NYB",
            params={"interval": "1d", "range": "7d"},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        chart_result = data["chart"]["result"][0]
        meta = chart_result["meta"]
        closes = chart_result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        # Filter out None values that appear for non-trading days
        closes = [c for c in closes if c is not None]

        current = float(meta.get("regularMarketPrice") or 0.0)
        if current > 0 and len(closes) >= 2:
            oldest = closes[0]
            change = ((current - oldest) / oldest) * 100
            result["value"] = round(current, 3)
            result["change_5d"] = round(change, 3)
            if change > 0.5:
                result["trend"] = "STRENGTHENING"
            elif change < -0.5:
                result["trend"] = "WEAKENING"
            logger.info(
                "DXY (ICE DX-Y.NYB): %.3f  5d_chg=%.3f%%  trend=%s",
                current, change, result["trend"],
            )
            return result
    except Exception as exc:
        logger.warning("Yahoo Finance DX-Y.NYB failed (%s) — falling back to EUR/USD proxy", exc)

    # ── Fallback: Alpha Vantage EUR/USD ───────────────────────────────────────
    # EUR is 57.6% of DXY so this is directionally correct on most days.
    # Value returned here is the USD/EUR rate (~0.90–0.95), NOT the DXY level.
    data = await _query("FX_DAILY", "USD", to_currency="EUR", outputsize="compact")
    if data and "Time Series FX (Daily)" in data:
        ts = data["Time Series FX (Daily)"]
        dates = sorted(ts.keys(), reverse=True)[:5]
        if len(dates) >= 2:
            latest = float(ts[dates[0]]["4. close"])
            oldest = float(ts[dates[-1]]["4. close"])
            change = ((latest - oldest) / oldest) * 100
            result["value"] = latest        # USD/EUR rate, not DXY level
            result["change_5d"] = round(change, 3)
            if change > 0.5:
                result["trend"] = "STRENGTHENING"
            elif change < -0.5:
                result["trend"] = "WEAKENING"
            logger.warning(
                "DXY fallback (EUR/USD proxy): %.4f  5d_chg=%.3f%%  trend=%s",
                latest, change, result["trend"],
            )

    return result


async def fetch_usdinr() -> Dict[str, Any]:
    """
    Fetch USD/INR exchange rate from Yahoo Finance (USDINR=X).

    USD/INR is the most India-specific USD signal:
      - Rising USD/INR (INR weakening) → higher import costs, FII outflows → bearish Nifty
      - Falling USD/INR (INR strengthening) → FII inflows, RBI comfortable → mildly bullish

    Returns dict with:
        value (float)         — current USD/INR spot rate (e.g. 84.5)
        change_pct (float)    — overnight % change vs previous close
        trend (str)           — INR_WEAKENING | INR_STRENGTHENING | STABLE
        available (bool)      — False if fetch failed
    """
    result: Dict[str, Any] = {
        "value": 0.0,
        "change_pct": 0.0,
        "trend": "STABLE",
        "available": False,
    }
    try:
        client = _get_http_client()
        resp = await client.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/USDINR%3DX",
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
            result["value"] = round(price, 4)
            result["change_pct"] = change_pct
            result["available"] = True
            # INR weakens when USD/INR rises (more rupees per dollar)
            if change_pct > 0.15:
                result["trend"] = "INR_WEAKENING"
            elif change_pct < -0.15:
                result["trend"] = "INR_STRENGTHENING"
            logger.info(
                "USD/INR: %.4f  chg=%.3f%%  trend=%s",
                price, change_pct, result["trend"],
            )
    except Exception as exc:
        logger.warning("Yahoo Finance USD/INR fetch failed: %s", exc)
    return result


async def fetch_sgx_nifty() -> Dict[str, Any]:
    """
    Estimate the Nifty 50 opening gap direction using overnight S&P 500 futures.

    WHY ES=F INSTEAD OF ^NSEI
    -------------------------
    At 6 AM IST, Yahoo Finance's ^NSEI returns yesterday's 3:30 PM closing price
    vs the day before — a 16-hour-old data point that is fully priced in.  It
    tells us nothing about what happened overnight.

    S&P 500 futures (ES=F) trade virtually 24/7 and at 6 AM IST reflect:
      - The US close at 4 PM ET (1:30 AM IST)
      - Any post-close developments
      - Asian session early risk sentiment (Nikkei, KOSPI open at 5:30 AM IST)

    Nifty 50 has a historical beta of ~0.65 to the S&P 500.  The overnight
    ES=F % change × 0.65 is the best freely available synthetic Nifty gap
    estimate and is what Indian institutional desks actually use.

    Returns the same schema as before (value, change_pct, signal) for backward
    compatibility with the database and LLM prompt.
    """
    result: Dict[str, Any] = {"value": 0.0, "change_pct": 0.0, "signal": "FLAT", "source": "fallback"}

    # ── Primary: S&P 500 futures (ES=F) ──────────────────────────────────────
    try:
        client = _get_http_client()
        resp = await client.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/ES%3DF",
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
            es_change_pct = round(((price - prev_close) / prev_close) * 100, 3)
            # Apply Nifty/SPX beta (~0.65) to estimate Nifty gap
            nifty_est_pct = round(es_change_pct * 0.65, 3)
            result["value"] = price
            result["change_pct"] = nifty_est_pct
            result["es_change_pct"] = es_change_pct   # raw futures change for context
            result["source"] = "es_futures"
            if nifty_est_pct > 0.2:
                result["signal"] = "GAP_UP"
            elif nifty_est_pct < -0.2:
                result["signal"] = "GAP_DOWN"
            logger.info(
                "Nifty gap estimate via ES=F: ES %.2f chg=%.3f%% → Nifty est=%.3f%% signal=%s",
                price, es_change_pct, nifty_est_pct, result["signal"],
            )
            return result
    except Exception as exc:
        logger.warning("ES=F futures fetch failed (%s) — falling back to ^NSEI close", exc)

    # ── Fallback: previous Nifty close (stale but better than nothing) ────────
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
            result["source"] = "nsei_close"
            if change_pct > 0.2:
                result["signal"] = "GAP_UP"
            elif change_pct < -0.2:
                result["signal"] = "GAP_DOWN"
            logger.warning(
                "Nifty gap fallback (^NSEI close — stale): %.2f chg=%.3f%% signal=%s",
                price, change_pct, result["signal"],
            )
    except Exception as exc:
        logger.warning("Yahoo Finance ^NSEI fallback also failed: %s — using FLAT", exc)
    return result


async def fetch_nikkei() -> Dict[str, Any]:
    """
    Fetch live Nikkei 225 performance from Yahoo Finance (^N225).

    At 6 AM IST, the Tokyo Stock Exchange has been open since 5:30 AM IST.
    Nikkei is the only major Asian index actually trading when the research
    agent runs.  Hang Seng and Shanghai open at 7 AM IST (too late).

    Nifty/Nikkei correlation is ~0.5–0.6, lower than Nifty/SPX but still
    a meaningful independent signal — especially on BoJ policy days or when
    JPY makes large moves (which also affect DXY).

    Returns dict with:
        value (float)       — current Nikkei 225 level
        change_pct (float)  — % change vs previous close
        signal (str)        — POSITIVE | NEGATIVE | FLAT
        available (bool)    — False if fetch failed
    """
    result: Dict[str, Any] = {
        "value": 0.0,
        "change_pct": 0.0,
        "signal": "FLAT",
        "available": False,
    }
    try:
        client = _get_http_client()
        resp = await client.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EN225",
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
            result["value"] = round(price, 2)
            result["change_pct"] = change_pct
            result["available"] = True
            if change_pct > 0.3:
                result["signal"] = "POSITIVE"
            elif change_pct < -0.3:
                result["signal"] = "NEGATIVE"
            logger.info(
                "Nikkei 225: %.2f  chg=%.3f%%  signal=%s",
                price, change_pct, result["signal"],
            )
    except Exception as exc:
        logger.warning("Yahoo Finance Nikkei fetch failed: %s", exc)
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


_EARNINGS_DEFAULT_SYMBOLS: List[str] = [
    "RELIANCE", "HDFCBANK", "INFY", "ICICIBANK", "TCS",
    "WIPRO", "AXISBANK", "KOTAKBANK", "SBIN", "BAJFINANCE",
    "HINDUNILVR", "ITC", "LT", "ONGC", "NTPC",
    "TMPV", "TMCV", "TATASTEEL", "SUNPHARMA", "MARUTI", "TITAN",
]


async def fetch_earnings_calendar(
    symbols: List[str] | None = None,
    lookahead_days: int = 7,
) -> List[Dict[str, Any]]:
    """
    Fetch upcoming NSE earnings dates via Yahoo Finance quoteSummary calendarEvents.

    For each symbol appends '.NS' and hits:
      https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}.NS?modules=calendarEvents

    Returns a list of {"stock": "INFY", "earnings_date": "2026-03-12"} for any
    stock with a result date in the next `lookahead_days` calendar days.
    Falls back to _EARNINGS_DEFAULT_SYMBOLS when no symbols list is provided.
    """
    target_symbols = symbols if symbols else _EARNINGS_DEFAULT_SYMBOLS
    today = datetime.now(timezone.utc).date()
    cutoff = today + timedelta(days=lookahead_days)

    async def _fetch_one(symbol: str) -> Dict[str, Any] | None:
        try:
            client = _get_http_client()
            resp = await client.get(
                f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}.NS",
                params={"modules": "calendarEvents"},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                },
                timeout=8,
            )
            resp.raise_for_status()
            result = resp.json()["quoteSummary"]["result"]
            if not result:
                return None
            dates = result[0]["calendarEvents"]["earnings"].get("earningsDate", [])
            if not dates:
                return None
            ts = dates[0].get("raw")
            if ts is None:
                return None
            earnings_date = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            if today <= earnings_date <= cutoff:
                return {"stock": symbol, "earnings_date": str(earnings_date)}
        except Exception as exc:
            logger.debug("Earnings calendar fetch skipped for %s: %s", symbol, exc)
        return None

    results = await asyncio.gather(*[_fetch_one(s) for s in target_symbols])
    candidates = [r for r in results if r is not None]
    logger.info(
        "Earnings calendar: %d upcoming results in next %d days",
        len(candidates), lookahead_days,
    )
    return candidates
