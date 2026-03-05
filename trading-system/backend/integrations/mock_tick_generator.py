"""
Mock Tick Generator — simulates a KiteTicker feed for paper trading mode.

Instead of a live WebSocket, this module drives the Scanner by:
  1. Fetching today's OHLC snapshot for each focus stock via Kite LTP (or
     hardcoded seed prices if Kite is unavailable).
  2. Running a random-walk simulation that produces tick-like dicts every second,
     constrained within a ±2% intraday range from the open price.
  3. Pushing those ticks through the same `_on_ticks` callback used by the real
     KiteTicker, so the Scanner pipeline is identical in both modes.

The simulation honours:
  - Market hours: 09:15–15:30 IST (Mon–Fri)
  - Volume ramp: volume seeds from ~1x avg in early session, growing ~1.6x by noon.
  - Momentum: 60% probability that a price move continues its direction (persistence).
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, time as dtime
from typing import Any, Callable, Dict, List, Optional

import pytz

from core.config import get_settings
from integrations.instrument_service import get_instrument_map

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
TICK_INTERVAL_SECONDS = 1.0

# Seed prices used when Kite LTP is unavailable (approximate CMP for paper mode)
SEED_PRICES: Dict[str, float] = {
    "RELIANCE":    2950.0,
    "HDFCBANK":    1620.0,
    "INFY":        1780.0,
    "TCS":         3850.0,
    "ICICIBANK":    1250.0,
    "BHARTIARTL":  1680.0,
    "HINDUNILVR":  2700.0,
    "ITC":          470.0,
    "KOTAKBANK":   1780.0,
    "LT":          3600.0,
    "SBIN":         840.0,
    "BAJFINANCE":  6800.0,
    "AXISBANK":    1140.0,
    "MARUTI":     12200.0,
    "SUNPHARMA":   1750.0,
    "TITAN":       3400.0,
    "WIPRO":        550.0,
    "HCLTECH":    1650.0,
    "TATAMOTORS":   950.0,
    "TATASTEEL":    155.0,
}


class MockTickGenerator:
    """
    Generates synthetic tick data and drives a Scanner's _on_ticks callback.
    Used exclusively when PAPER_TRADING=true.
    """

    def __init__(self, on_ticks_callback: Callable) -> None:
        self._on_ticks = on_ticks_callback
        self._settings = get_settings()
        self._running = False
        self._prices: Dict[str, float] = {}     # current simulated price per symbol
        self._directions: Dict[str, int] = {}   # +1 or -1 (momentum persistence)
        self._volume_base: Dict[str, float] = {}
        self._open_prices: Dict[str, float] = {}  # session open price — price bound pivot
        self._day_high: Dict[str, float] = {}   # running session high per symbol
        self._day_low: Dict[str, float] = {}    # running session low per symbol
        self._cum_volume: Dict[str, int] = {}   # cumulative volume per symbol (like real Kite)

    def _is_market_open(self) -> bool:
        now = datetime.now(IST).time()
        return MARKET_OPEN <= now <= MARKET_CLOSE

    def _seed_prices(self) -> None:
        """Initialise starting prices from Kite LTP or hardcoded seeds."""
        for symbol in get_instrument_map().keys():
            if symbol not in self._prices:
                seed = SEED_PRICES.get(symbol, 1000.0)
                self._prices[symbol] = seed
                self._open_prices[symbol] = seed  # anchor for \u00b12% intraday bound
                self._directions[symbol] = random.choice([-1, 1])
                self._volume_base[symbol] = random.uniform(80_000, 500_000)
                self._day_high[symbol] = seed
                self._day_low[symbol] = seed
                self._cum_volume[symbol] = 0

    async def _try_seed_from_kite(self) -> None:
        """Optionally seed prices from live Kite LTP (best-effort)."""
        try:
            from integrations.kite_client import get_kite_client  # lazy
            kite_client = get_kite_client()
            symbols = list(get_instrument_map().keys())
            instruments = [f"NSE:{sym}" for sym in symbols]
            ltp_data = await kite_client.get_ltp(instruments)
            for sym in symbols:
                key = f"NSE:{sym}"
                if key in ltp_data and ltp_data[key].get("last_price"):
                    price = float(ltp_data[key]["last_price"])
                    self._prices[sym] = price
                    # Re-anchor the open price so \u00b12% bound uses the real LTP
                    self._open_prices[sym] = price
                    self._day_high[sym] = price
                    self._day_low[sym] = price
            logger.info("[MockTick] Seeded %d prices from Kite LTP", len(ltp_data))
        except Exception as exc:
            logger.warning("[MockTick] Could not seed from Kite LTP: %s — using hardcoded seeds", exc)

    def _next_tick(self, symbol: str) -> Dict[str, Any]:
        """Generate the next synthetic tick for a symbol."""
        now_ist = datetime.now(IST)
        minutes_since_open = (
            (now_ist.hour * 60 + now_ist.minute) - (9 * 60 + 15)
        )

        price = self._prices[symbol]
        direction = self._directions[symbol]

        # Random-walk: 60% continue direction (momentum), 40% reverse
        if random.random() < 0.60:
            pass  # keep direction
        else:
            direction = -direction
            self._directions[symbol] = direction

        # Tick size: 0.01–0.05% of price
        move_pct = random.uniform(0.0001, 0.0005) * direction
        new_price = price * (1 + move_pct)

        # Enforce ±2% intraday range from the session open price so the
        # random walk cannot drift unrealistically over a full 375-minute session.
        open_price = self._open_prices[symbol]
        new_price = max(open_price * 0.98, min(open_price * 1.02, new_price))
        new_price = max(round(new_price, 2), 1.0)

        # Reverse direction when hitting a band boundary to prevent sticking
        if new_price >= open_price * 1.02 or new_price <= open_price * 0.98:
            self._directions[symbol] = -direction

        self._prices[symbol] = new_price

        # Volume increases through the session (ramp from 1x → 1.8x avg)
        session_progress = min(1.0, max(0.0, minutes_since_open / 370.0))
        vol_multiplier = 1.0 + 0.8 * session_progress + random.uniform(-0.1, 0.1)
        tick_volume = int(self._volume_base[symbol] * vol_multiplier / 375)  # per-second slice
        self._cum_volume[symbol] += tick_volume

        # Track running session high / low
        self._day_high[symbol] = max(self._day_high[symbol], new_price)
        self._day_low[symbol] = min(self._day_low[symbol], new_price)

        token = get_instrument_map().get(symbol, 0)

        return {
            "instrument_token": token,
            "exchange_timestamp": now_ist,
            "last_price": new_price,
            "volume_traded": self._cum_volume[symbol],
            "ohlc": {
                "open": open_price,
                "high": self._day_high[symbol],
                "low": self._day_low[symbol],
                "close": new_price,
            },
        }

    async def run(self) -> None:
        """Main loop: emit ticks every second during market hours."""
        self._seed_prices()
        await self._try_seed_from_kite()

        self._running = True
        logger.info("[MockTick] Paper trading tick generator started — %d symbols",
                    len(get_instrument_map()))

        while self._running:
            if not self._is_market_open():
                logger.debug("[MockTick] Outside market hours — sleeping 30s")
                await asyncio.sleep(30)
                continue

            ticks: List[Dict[str, Any]] = []
            for symbol in list(get_instrument_map().keys()):
                ticks.append(self._next_tick(symbol))

            # Drive the Scanner's tick callback directly (same interface as KiteTicker)
            try:
                self._on_ticks(ws=None, ticks=ticks)
            except Exception as exc:
                logger.error("[MockTick] on_ticks callback error: %s", exc)

            await asyncio.sleep(TICK_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._running = False
        logger.info("[MockTick] Paper trading tick generator stopped")
