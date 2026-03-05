"""
Instrument Service — builds and caches the symbol→token map required by KiteTicker.

On startup this module:
  1. Tries to load the map from Redis (populated on a previous run today).
  2. If not found, downloads the full NSE instrument dump from Kite, filters to
     the configured focus_stocks list, and stores the map in Redis with a 24-hour TTL.
  3. Falls back to a hardcoded NIFTY 50 token map if Kite is unreachable (paper mode).
"""

from __future__ import annotations

import json
import logging
from typing import Dict, Optional

import asyncio

from core.config import get_settings
from core.redis_client import get_value, set_value

logger = logging.getLogger(__name__)

INSTRUMENT_MAP_KEY = "kite_instrument_map"
INSTRUMENT_MAP_TTL = 24 * 60 * 60  # 24 hours

# ── Hardcoded fallback token map (paper / offline mode) ────────────────────────
# Source: Zerodha NSE instrument dump — tokens are stable for NIFTY 50 large-caps.
FALLBACK_TOKEN_MAP: Dict[str, int] = {
    "RELIANCE":    738561,
    "HDFCBANK":    341249,
    "INFY":        408065,
    "TCS":         2953217,
    "ICICIBANK":   1270529,
    "BHARTIARTL":  2714625,
    "HINDUNILVR":  356865,
    "ITC":         424961,
    "KOTAKBANK":   492033,
    "LT":          2939649,
    "SBIN":        779521,
    "BAJFINANCE":  81153,
    "ASIANPAINT":  60417,
    "AXISBANK":    1510401,
    "MARUTI":      2815745,
    "SUNPHARMA":   857857,
    "TITAN":       897537,
    "NESTLEIND":   4598529,
    "WIPRO":       969473,
    "ONGC":        633601,
    "HCLTECH":     1850625,
    "ULTRACEMCO":  2952193,
    "POWERGRID":   3834113,
    "NTPC":        2977281,
    "TECHM":       3465729,
    "BAJAJFINSV":  4268801,
    "TATAMOTORS":  884737,
    "TATASTEEL":   895745,
    "JSWSTEEL":    3001089,
    "ADANIENT":    25,
    "ADANIPORTS":  3861249,
    "GRASIM":      315393,
    "HDFCLIFE":    119553,
    "SBILIFE":     5582849,
    "COALINDIA":   5215745,
    "BPCL":        134657,
    "EICHERMOT":   232961,
    "HEROMOTOCO":  345089,
    "CIPLA":       177665,
    "DRREDDY":     225537,
    "DIVISLAB":    2800641,
    "APOLLOHOSP":  157441,
    "BAJAJ-AUTO":  4267265,
    "BRITANNIA":   140033,
    "TATACONSUM":  878593,
    "UPL":         2889473,
    "INDUSINDBK":  1346049,
    "M&M":         519937,
    "SHREECEM":    3566337,
}


# ── Public API ─────────────────────────────────────────────────────────────────

async def load_instrument_map() -> Dict[str, int]:
    """
    Load symbol→token map from Redis or Kite API.
    Returns the map and also writes it to the module-level cache.
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

    # 2. Try Kite API
    settings = get_settings()
    try:
        from integrations.kite_client import get_kite_client  # lazy import
        kite_client = get_kite_client()
        token_map = await _fetch_from_kite(kite_client, settings.focus_stocks)
        if token_map:
            await set_value(
                INSTRUMENT_MAP_KEY,
                json.dumps(token_map),
                ttl=INSTRUMENT_MAP_TTL,
            )
            logger.info(
                "Instrument map fetched from Kite and cached (%d symbols)", len(token_map)
            )
            _set_live_map(token_map)
            return token_map
    except Exception as exc:
        logger.warning("Could not fetch instrument map from Kite: %s", exc)

    # 3. Fall back to hardcoded map
    logger.warning(
        "Using hardcoded NIFTY 50 token map (paper/offline mode) — %d symbols",
        len(FALLBACK_TOKEN_MAP),
    )
    _set_live_map(FALLBACK_TOKEN_MAP)
    return FALLBACK_TOKEN_MAP


async def _fetch_from_kite(kite_client, focus_stocks: list[str]) -> Dict[str, int]:
    """Download NSE instrument dump and extract tokens for focus stocks."""
    kite = await kite_client.get_kite()
    instruments = await asyncio.to_thread(kite.instruments, "NSE")
    token_map: Dict[str, int] = {}
    for inst in instruments:
        symbol = inst.get("tradingsymbol", "")
        if symbol in focus_stocks:
            token_map[symbol] = int(inst["instrument_token"])
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
