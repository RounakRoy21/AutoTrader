"""
NSE India API wrapper — fetches FII/DII trade data, corporate actions, and live indices.

All endpoints use the same anti-scraping workaround: hit the homepage first so
NSE sets a valid session cookie, then hit the JSON API endpoint.  The market-data
pages are JavaScript-rendered — there is no HTML table to parse.  The page's own
JS calls the same JSON endpoints we use here.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
import pytz

from core.nse_calendar import is_nse_holiday, ist_today

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

NSE_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
NSE_CORP_ACTIONS_URL = "https://www.nseindia.com/api/corporates-corporateActions"
NSE_ALL_INDICES_URL = "https://www.nseindia.com/api/allIndices"
NSE_MARKET_STATUS_URL = "https://www.nseindia.com/api/marketStatus"
NSE_EVENT_CALENDAR_URL = "https://www.nseindia.com/api/event-calendar"
NSE_CORP_ANNOUNCEMENTS_URL = "https://www.nseindia.com/api/corporate-announcements"
NSE_LARGE_DEAL_URL = "https://www.nseindia.com/api/snapshot-capital-market-largedeal"
# Security-wise full bhavcopy (includes DELIV_PER column).  Date is appended as DDMMYYYY.
NSE_BHAVCOPY_BASE = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_"
TIMEOUT = 10

# Hard cap on corporate-announcement items returned (keeps the LLM prompt bounded).
_ANNOUNCEMENTS_CAP = 25

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

            # NSE allIndices uses "last" for the current value, not "current".
            current = _safe_float(entry.get("last"))
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


async def fetch_gift_nifty() -> Dict[str, Any]:
    """
    Fetch real GIFT Nifty (NSE IX) overnight futures from NSE's marketStatus API.

    GIFT Nifty trades on NSE International Exchange (GIFT City, Gandhinagar) almost
    around the clock and is the single best free pre-market indicator of the Nifty 50
    opening gap.  Unlike the ES=F × 0.65 synthetic proxy, this is the actual Nifty
    futures contract that Indian institutional desks watch at 6 AM IST.

    The marketStatus endpoint returns a `giftnifty` object alongside the cash-market
    open/close states:
        {"INSTRUMENTTYPE": "GIFT NIFTY", "LASTPRICE": 23456.5, "DAYCHANGE": "+85.00",
         "PERCHANGE": "+0.36", "EXPIRYDATE": "26-Jun-2026", ...}

    Returns the same schema shape as fetch_sgx_nifty() (value, change_pct, signal)
    for backward compatibility with the LLM prompt and database, plus `expiry` and
    `available`.
    """
    result: Dict[str, Any] = {
        "value": 0.0,
        "change_pct": 0.0,
        "day_change": 0.0,
        "expiry": None,
        "signal": "FLAT",
        "source": "nse_gift_nifty",
        "available": False,
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
            # Establish session cookie — NSE blocks unauthenticated API calls.
            await client.get("https://www.nseindia.com/")
            resp = await client.get(NSE_MARKET_STATUS_URL)
            resp.raise_for_status()
            data = resp.json()

        gift = data.get("giftnifty") or {}
        last_price = _safe_float(gift.get("LASTPRICE"))
        pct_change = _safe_float(gift.get("PERCHANGE"))
        day_change = _safe_float(gift.get("DAYCHANGE"))

        if last_price > 0:
            result["available"] = True
            result["value"] = last_price
            result["change_pct"] = round(pct_change, 3)
            result["day_change"] = round(day_change, 2)
            result["expiry"] = gift.get("EXPIRYDATE")
            if pct_change > 0.2:
                result["signal"] = "GAP_UP"
            elif pct_change < -0.2:
                result["signal"] = "GAP_DOWN"
            logger.info(
                "GIFT Nifty via NSE: %.2f (%.3f%%) signal=%s expiry=%s",
                last_price, pct_change, result["signal"], result["expiry"],
            )
        else:
            logger.warning("NSE marketStatus returned no usable giftnifty data")

    except httpx.HTTPStatusError as exc:
        logger.error("NSE GIFT Nifty HTTP error: %s", exc)
    except Exception as exc:
        logger.error("NSE GIFT Nifty fetch failed: %s", exc)

    return result


# Watchlist applied to the NSE event calendar so it mirrors the Yahoo Finance
# earnings-calendar fallback exactly and never floods the watchlist with
# whole-market board meetings.  Mirrors alpha_vantage_client._EARNINGS_DEFAULT_SYMBOLS.
_EVENT_CALENDAR_DEFAULT_SYMBOLS = [
    "RELIANCE", "HDFCBANK", "INFY", "ICICIBANK", "TCS",
    "WIPRO", "AXISBANK", "KOTAKBANK", "SBIN", "BAJFINANCE",
    "HINDUNILVR", "ITC", "LT", "ONGC", "NTPC",
    "TMPV", "TATASTEEL", "SUNPHARMA", "MARUTI", "TITAN",
    "JIOFIN", "SHRIRAMFIN", "TRENT", "ETERNAL", "INDIGO", "BEL", "MAXHEALTH",
]


async def fetch_event_calendar(
    symbols: Optional[List[str]] = None,
    lookahead_days: int = 7,
) -> List[Dict[str, Any]]:
    """
    Fetch upcoming NSE earnings dates from the NSE event-calendar API.

    This is the authoritative source for Indian board-meeting / results dates —
    it is the same data companies file with the exchange.  It returns the WHOLE
    market, so results are filtered to *symbols* (the watchlist) and to board
    meetings whose `purpose` includes "Financial Results" (the purpose field can
    be compound, e.g. "Financial Results/Dividend").

    Returns the SAME schema as alpha_vantage_client.fetch_earnings_calendar():
        [{"stock": "INFY", "earnings_date": "2026-07-23"}, ...]
    so it is a drop-in replacement.  Returns [] on any failure or no match — the
    caller falls back to the Yahoo Finance calendar so behaviour never regresses.
    """
    target = {s.strip().upper() for s in (symbols or _EVENT_CALENDAR_DEFAULT_SYMBOLS)}
    today = ist_today()
    cutoff = today + timedelta(days=lookahead_days)

    results: List[Dict[str, Any]] = []
    seen: set[str] = set()

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
            # Establish session cookie — NSE blocks unauthenticated API calls.
            await client.get("https://www.nseindia.com/")
            resp = await client.get(NSE_EVENT_CALENDAR_URL)
            resp.raise_for_status()
            data = resp.json()

        for entry in data if isinstance(data, list) else []:
            purpose = (entry.get("purpose") or "")
            if "financial results" not in purpose.lower():
                continue
            symbol = (entry.get("symbol") or "").strip().upper()
            if symbol not in target or symbol in seen:
                continue
            date_str = (entry.get("date") or "").strip()
            try:
                ev_date = datetime.strptime(date_str, "%d-%b-%Y").date()
            except ValueError:
                logger.debug("Could not parse event-calendar date '%s' for %s", date_str, symbol)
                continue
            if today <= ev_date <= cutoff:
                results.append({"stock": symbol, "earnings_date": ev_date.isoformat()})
                seen.add(symbol)

        logger.info(
            "NSE event-calendar: %d watchlist results in next %d days",
            len(results), lookahead_days,
        )

    except httpx.HTTPStatusError as exc:
        logger.error("NSE event-calendar HTTP error: %s", exc)
    except Exception as exc:
        logger.error("NSE event-calendar fetch failed: %s", exc)

    return results


async def fetch_corporate_announcements(
    symbols: Optional[List[str]] = None,
    lookback_hours: int = 20,
) -> List[Dict[str, Any]]:
    """
    Fetch recent NSE corporate announcements — overnight catalysts that move stocks
    at the open (order wins, fundraising, board changes, regulatory filings, etc.).

    When *symbols* is provided the feed is filtered to those stocks (keeps the LLM
    prompt focused on the watchlist).  When omitted (cold start) the whole-market
    feed is returned, capped at _ANNOUNCEMENTS_CAP.

    Returns a list of:
        {"symbol", "category", "summary", "time", "industry"}
    Returns [] on any failure.
    """
    now = datetime.now(IST)
    cutoff = now - timedelta(hours=lookback_hours)
    target = {s.strip().upper() for s in symbols} if symbols else None

    out: List[Dict[str, Any]] = []

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
            # Establish session cookie — NSE blocks unauthenticated API calls.
            await client.get("https://www.nseindia.com/")
            resp = await client.get(f"{NSE_CORP_ANNOUNCEMENTS_URL}?index=equities")
            resp.raise_for_status()
            data = resp.json()

        for entry in data if isinstance(data, list) else []:
            symbol = (entry.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            if target is not None and symbol not in target:
                continue

            an_dt = (entry.get("an_dt") or "").strip()  # e.g. "26-Jun-2026 08:15:30"
            ann_time = None
            try:
                ann_time = IST.localize(datetime.strptime(an_dt, "%d-%b-%Y %H:%M:%S"))
            except (ValueError, TypeError):
                ann_time = None  # keep undated items rather than silently dropping
            # Only drop items we can date AND that are older than the window.
            if ann_time is not None and ann_time < cutoff:
                continue

            summary = (entry.get("attchmntText") or entry.get("desc") or "").strip()
            if len(summary) > 240:
                summary = summary[:237] + "..."

            out.append({
                "symbol": symbol,
                "category": (entry.get("desc") or "").strip(),
                "summary": summary,
                "time": an_dt or None,
                "industry": (entry.get("smIndustry") or "").strip() or None,
            })

        out = out[:_ANNOUNCEMENTS_CAP]
        logger.info(
            "NSE corporate announcements: %d items (watchlist filter=%s)",
            len(out), target is not None,
        )

    except httpx.HTTPStatusError as exc:
        logger.error("NSE corp announcements HTTP error: %s", exc)
    except Exception as exc:
        logger.error("NSE corp announcements fetch failed: %s", exc)

    return out


# Official NSE index-constituents CSV.  Stable URL, columns:
#   Company Name, Industry, Symbol, Series, ISIN Code
# Preferred over the equity-stockIndices JSON API, which is frequently 403/404
# for unauthenticated clients.  This static archive file is reliably reachable.
NSE_NIFTY50_LIST_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"


async def fetch_nifty50_constituents() -> Dict[str, Any]:
    """
    Fetch the current official NIFTY 50 index constituents from NSE.

    Source: the authoritative ind_nifty50list.csv archive file (the same list NSE
    publishes on every index reconstitution).  Used by the quarterly constituent
    monitor to detect companies entering/leaving the index and demergers/splits
    (e.g. TATAMOTORS → TMPV + TMCV) so the app's hardcoded ticker maps can be
    refreshed.

    Returns:
        {
            "available": bool,         # False if the fetch/parse failed
            "count": int,              # number of constituents (normally 50)
            "symbols": [str, ...],     # NSE trading symbols, upper-cased
            "companies": {sym: name},  # symbol → company name
            "isins": {sym: isin},      # symbol → ISIN (useful to spot demergers)
            "source": "nse_csv",
        }
    Never raises — returns available=False on any failure.
    """
    result: Dict[str, Any] = {
        "available": False,
        "count": 0,
        "symbols": [],
        "companies": {},
        "isins": {},
        "source": "nse_csv",
    }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(NSE_NIFTY50_LIST_URL)
            resp.raise_for_status()
            text = resp.text

        symbols: List[str] = []
        companies: Dict[str, str] = {}
        isins: Dict[str, str] = {}
        for row in csv.DictReader(io.StringIO(text)):
            symbol = (row.get("Symbol") or "").strip().upper()
            if not symbol:
                continue
            symbols.append(symbol)
            companies[symbol] = (row.get("Company Name") or "").strip()
            isins[symbol] = (row.get("ISIN Code") or "").strip()

        if symbols:
            result.update(
                available=True,
                count=len(symbols),
                symbols=symbols,
                companies=companies,
                isins=isins,
            )
            logger.info("NIFTY 50 constituents fetched: %d symbols", len(symbols))
        else:
            logger.warning("NIFTY 50 constituents CSV parsed but contained no rows")

    except httpx.HTTPStatusError as exc:
        logger.error("NSE NIFTY 50 constituents HTTP error: %s", exc)
    except Exception as exc:
        logger.error("NSE NIFTY 50 constituents fetch failed: %s", exc)

    return result


def _last_trading_day(reference: date | None = None) -> date:
    """Return the most recent NSE trading day strictly before *reference*.

    Walks backwards skipping weekends and exchange holidays.  Used to locate the
    previous session's end-of-day data (delivery bhavcopy) when the research
    agent runs pre-market (~6 AM IST, before today's session has any EOD data).
    """
    d = (reference or ist_today()) - timedelta(days=1)
    # Cap the walk-back at 10 days so a calendar gap can never loop forever.
    for _ in range(10):
        if d.weekday() < 5 and not is_nse_holiday(d):
            return d
        d -= timedelta(days=1)
    return d


def _norm_side(raw: Any) -> str:
    """Normalise NSE's buy/sell field to 'BUY' | 'SELL' | 'UNKNOWN'."""
    s = str(raw or "").strip().upper()
    if s in ("BUY", "B", "P"):  # P = purchase in some legacy feeds
        return "BUY"
    if s in ("SELL", "S"):
        return "SELL"
    return "UNKNOWN"


async def fetch_bulk_deals(watchlist: Optional[List[str]] = None) -> Dict[str, Any]:
    """Fetch the latest session's bulk & block deals from the NSE largedeal API.

    Bulk/block deals reveal institutional accumulation or distribution: a large
    buy by a known fund in a watchlist stock is a conviction signal, while heavy
    selling is a caution flag.  The research agent runs pre-market, so the
    "latest" snapshot is the previous session's deals — exactly the context we
    want before today's open.

    Parameters
    ----------
    watchlist : list[str] | None
        If provided, only deals in these symbols are returned (keeps the LLM
        payload small and focuses the signal on tradeable stocks).  Matching is
        case-insensitive.  If None, all deals are returned.

    Returns
    -------
    dict with keys:
        available (bool)        — False if the request failed
        as_on_date (str | None) — session date the deals belong to
        deals (list[dict])      — each: {symbol, side, qty, price, client}
    """
    result: Dict[str, Any] = {"available": False, "as_on_date": None, "deals": []}
    wl = {s.strip().upper() for s in watchlist} if watchlist else None

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
            # Homepage first so NSE sets the session cookie, else 403.
            await client.get("https://www.nseindia.com/")
            resp = await client.get(NSE_LARGE_DEAL_URL)
            resp.raise_for_status()
            data = resp.json()

        result["available"] = True
        result["as_on_date"] = data.get("as_on_date")

        # The snapshot bundles bulk, block and short-sell deals under separate
        # keys.  Bulk + block are the institutional-footprint signals we care
        # about; short deals are excluded (noisy, intraday).
        raw_deals: List[dict] = []
        for key in ("BULK_DEALS_DATA", "BLOCK_DEALS_DATA"):
            entries = data.get(key)
            if isinstance(entries, list):
                raw_deals.extend(entries)

        for entry in raw_deals:
            symbol = str(entry.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            if wl is not None and symbol not in wl:
                continue
            client_name = str(entry.get("clientName") or entry.get("name") or "").strip()
            result["deals"].append({
                "symbol": symbol,
                "side": _norm_side(entry.get("buySell") or entry.get("buyOrSell")),
                "qty": int(_safe_float(entry.get("qty"))),
                "price": round(_safe_float(entry.get("watp") or entry.get("price")), 2),
                # Truncate long fund names to keep the prompt compact.
                "client": client_name[:60],
            })

        # Cap at 25 entries so an unusually active session cannot bloat the
        # prompt.  Watchlist filtering already keeps this small in practice.
        result["deals"] = result["deals"][:25]

        logger.info(
            "NSE bulk/block deals (as_on=%s): %d relevant deal(s)",
            result["as_on_date"], len(result["deals"]),
        )

    except httpx.HTTPStatusError as exc:
        logger.error("NSE bulk deals HTTP error: %s", exc)
    except Exception as exc:
        logger.error("NSE bulk deals fetch failed: %s", exc)

    return result


async def fetch_delivery_data(watchlist: Optional[List[str]] = None) -> Dict[str, Any]:
    """Fetch previous-session delivery percentages for watchlist stocks.

    Delivery % = the fraction of traded volume that resulted in actual delivery
    (settled to demat) rather than intraday squaring-off.  High delivery % means
    buyers took genuine ownership — a sign of conviction/accumulation rather than
    speculative churn.  It is a useful signal-quality filter for watchlist stocks.

    Source: NSE's security-wise full bhavcopy
    (``sec_bhavdata_full_DDMMYYYY.csv``), published end-of-day.  We resolve the
    previous trading day and fall back to a couple of earlier sessions in case
    the most recent file is not yet posted (e.g. very early morning).

    Parameters
    ----------
    watchlist : list[str] | None
        Symbols to extract delivery % for.  If None, returns an empty mapping
        (downloading delivery for the whole market is wasteful for the brief).

    Returns
    -------
    dict with keys:
        available (bool)            — False if no bhavcopy could be fetched
        as_on_date (str | None)     — session the data belongs to (DD-Mon-YYYY)
        delivery_pct (dict[str,float]) — {symbol: delivery_percent}
    """
    result: Dict[str, Any] = {"available": False, "as_on_date": None, "delivery_pct": {}}

    if not watchlist:
        # No targets — nothing worth downloading a full bhavcopy for.
        return result

    wl = {s.strip().upper() for s in watchlist}

    # Try the last trading day, then step back up to 3 more sessions if that
    # file isn't posted yet (NSE occasionally publishes the EOD file late).
    candidate = _last_trading_day()
    for _ in range(4):
        ddmmyyyy = candidate.strftime("%d%m%Y")
        url = f"{NSE_BHAVCOPY_BASE}{ddmmyyyy}.csv"
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
                # The bhavcopy CSV is a static public file on nsearchives — no
                # session cookie is required.  The nsearchives homepage itself
                # returns 404, so we go straight to the file URL.
                resp = await client.get(url)
            if resp.status_code != 200 or not resp.content:
                candidate = _last_trading_day(candidate)
                continue

            reader = csv.DictReader(io.StringIO(resp.text))
            for row in reader:
                # NSE prefixes many CSV column names with a leading space.
                clean = {k.strip().upper(): (v.strip() if isinstance(v, str) else v)
                         for k, v in row.items()}
                symbol = str(clean.get("SYMBOL", "")).upper()
                if symbol not in wl:
                    continue
                # Only the cash-market equity series carry meaningful delivery %.
                series = str(clean.get("SERIES", "")).upper()
                if series and series not in ("EQ", "BE", "BZ"):
                    continue
                deliv = _safe_float(clean.get("DELIV_PER"))
                if deliv > 0:
                    result["delivery_pct"][symbol] = round(deliv, 2)

            result["available"] = True
            result["as_on_date"] = candidate.strftime("%d-%b-%Y")
            logger.info(
                "NSE delivery data (as_on=%s): %d/%d watchlist symbols matched",
                result["as_on_date"], len(result["delivery_pct"]), len(wl),
            )
            return result

        except httpx.HTTPStatusError as exc:
            logger.error("NSE delivery bhavcopy HTTP error (%s): %s", ddmmyyyy, exc)
            candidate = _last_trading_day(candidate)
        except Exception as exc:
            logger.error("NSE delivery bhavcopy fetch failed (%s): %s", ddmmyyyy, exc)
            candidate = _last_trading_day(candidate)

    return result
