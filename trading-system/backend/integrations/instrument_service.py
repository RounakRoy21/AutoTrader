"""
Instrument Service — builds and caches the symbol→token map required by GrowwFeed.

On startup this module:
  1. Tries to load the map from Redis (populated on a previous run today).
  2. If not found, downloads the Groww NSE instrument list, filters to
     the configured focus_stocks list, and stores the map in Redis with a 24-hour TTL.
  3. Falls back to a hardcoded NIFTY 50 token map if Groww is unreachable (paper mode).

Tokens are Groww exchange_token integers — different from Zerodha instrument tokens
even for the same stock (e.g. RELIANCE = 2885 on Groww vs 738561 on Zerodha).
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

import asyncio

from core.config import get_settings
from core.redis_client import get_value, set_value
from core.redis_keys import INSTRUMENT_MAP_KEY, TODAY_WATCHLIST_KEY

logger = logging.getLogger(__name__)

INSTRUMENT_MAP_TTL = 24 * 60 * 60  # 24 hours

# NIFTY 50 index exchange_token — Groww value for NSE:NIFTY 50.
# Used by the Scanner to subscribe to the index feed for the intraday trend filter.
# NOTE: This is the Groww exchange_token (2999 on NSE), NOT the Zerodha instrument token.
NIFTY50_TOKEN: int = 2999

# ── Hardcoded fallback token map (paper / offline mode) ────────────────────────
# Source: Groww NSE instrument list — exchange_token values for NIFTY 50 large-caps.
# Groww exchange_tokens differ from Zerodha instrument tokens for the same stocks.
FALLBACK_TOKEN_MAP: Dict[str, int] = {
    "RELIANCE":    2885,
    "HDFCBANK":    1333,
    "INFY":        1594,
    "TCS":         11536,
    "ICICIBANK":   4963,
    "BHARTIARTL":  10604,
    "HINDUNILVR":  1394,
    "ITC":         1660,
    "KOTAKBANK":   1922,
    "LT":          11483,
    "SBIN":        3045,
    "BAJFINANCE":  317,
    "ASIANPAINT":  236,
    "AXISBANK":    5900,
    "MARUTI":      10999,
    "SUNPHARMA":   3351,
    "TITAN":       3506,
    "NESTLEIND":   17963,
    "WIPRO":       3787,
    "ONGC":        2475,
    "HCLTECH":     7229,
    "ULTRACEMCO":  11532,
    "POWERGRID":   14977,
    "NTPC":        11630,
    "TECHM":       13538,
    "BAJAJFINSV":  16675,
    "TATAMOTORS":  3456,
    "TATASTEEL":   3499,
    "JSWSTEEL":    11723,
    "ADANIENT":    25,
    "ADANIPORTS":  15083,
    "GRASIM":      1232,
    "HDFCLIFE":    467,
    "SBILIFE":     21808,
    "COALINDIA":   20374,
    "BPCL":        526,
    "EICHERMOT":   910,
    "HEROMOTOCO":  1348,
    "CIPLA":       694,
    "DRREDDY":     881,
    "DIVISLAB":    10940,
    "APOLLOHOSP":  157441,
    "BAJAJ-AUTO":  16669,
    "BRITANNIA":   547,
    "TATACONSUM":  3432,
    "UPL":         11287,
    "INDUSINDBK":  5258,
    "M&M":         2031,
    "SHREECEM":    13970,
}


# ── Public API ─────────────────────────────────────────────────────────────────

async def load_instrument_map() -> Dict[str, int]:
    """
    Load symbol→token map from Redis or Groww API.

    Stock list priority:
      1. Redis cache (instrument map previously built today) — fastest path.
      2. TODAY_WATCHLIST_KEY — LLM-chosen stocks from this morning’s Market Brief.
      3. settings.focus_stocks — static fallback defined in .env / config.
      4. Hardcoded FALLBACK_TOKEN_MAP — paper/offline mode (all Nifty 50).
    """
    # 1. Try Redis cache
    cached = await get_value(INSTRUMENT_MAP_KEY)
    if cached:
        try:
            token_map: Dict[str, int] = json.loads(cached)
            logger.info(
                "Instrument map loaded from Redis (%d symbols)", len(token_map)
            )
            _set_live_map(token_map)
            return token_map
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Invalid cached instrument map: %s", exc)

    # 2. Determine the stock list to subscribe to:
    #    prefer today’s dynamic watchlist written by the Research Agent.
    settings = get_settings()
    stock_list = settings.focus_stocks  # static default
    dynamic_raw = await get_value(TODAY_WATCHLIST_KEY)
    if dynamic_raw:
        try:
            dynamic_list = json.loads(dynamic_raw)
            if isinstance(dynamic_list, list) and dynamic_list:
                stock_list = dynamic_list
                logger.info(
                    "Using today’s dynamic watchlist for instrument map (%d stocks): %s",
                    len(stock_list), stock_list,
                )
        except Exception as exc:
            logger.warning("Could not parse TODAY_WATCHLIST_KEY: %s", exc)

    # 3. Try Groww API
    try:
        from integrations.groww_client import get_groww_client  # lazy import
        groww_client = get_groww_client()
        token_map = await _fetch_from_groww(groww_client, stock_list)
        if token_map:
            await set_value(
                INSTRUMENT_MAP_KEY,
                json.dumps(token_map),
                ttl=INSTRUMENT_MAP_TTL,
            )
            logger.info(
                "Instrument map fetched from Groww and cached (%d symbols)", len(token_map)
            )
            _set_live_map(token_map)
            return token_map
    except Exception as exc:
        logger.warning("Could not fetch instrument map from Groww: %s", exc)

    # 4. Fall back to hardcoded map
    logger.warning(
        "Using hardcoded NIFTY 50 token map (paper/offline mode) — %d symbols",
        len(FALLBACK_TOKEN_MAP),
    )
    _set_live_map(FALLBACK_TOKEN_MAP)
    return FALLBACK_TOKEN_MAP


async def _fetch_from_groww(groww_client, focus_stocks: list[str]) -> Dict[str, int]:
    """Download Groww NSE instrument list and extract exchange_tokens for focus stocks."""
    groww = await groww_client.get_groww()

    def _get_instruments():
        return groww.get_instruments(exchange="NSE")

    instruments = await asyncio.to_thread(_get_instruments)
    if not isinstance(instruments, list):
        instruments = instruments.get("data", instruments.get("instruments", []))

    token_map: Dict[str, int] = {}
    for inst in instruments:
        symbol = inst.get("trading_symbol") or inst.get("tradingSymbol") or inst.get("tradingsymbol", "")
        if symbol in focus_stocks:
            token = inst.get("exchange_token") or inst.get("exchangeToken") or inst.get("instrument_token")
            if token is not None:
                token_map[symbol] = int(token)
    return token_map


# ── Module-level live map (shared with Scanner) ────────────────────────────────

_live_map: Dict[str, int] = {}
_token_to_symbol: Dict[int, str] = {}  # reverse index — O(1) lookup per tick


def _set_live_map(m: Dict[str, int]) -> None:
    global _live_map, _token_to_symbol
    _live_map = m
    _token_to_symbol = {tok: sym for sym, tok in m.items()}


def get_instrument_map() -> Dict[str, int]:
    """Return the currently loaded symbol→token map (populated at startup)."""
    return _live_map


def get_token(symbol: str) -> Optional[int]:
    """Return the instrument token for a given symbol, or None if not found."""
    return _live_map.get(symbol)


def get_symbol(token: int) -> Optional[str]:
    """Reverse lookup: return symbol for an instrument token — O(1)."""
    return _token_to_symbol.get(token)
