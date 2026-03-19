"""
Zerodha Kite Connect wrapper.
Centralised interface for order placement, position fetch, and WebSocket ticks.
All calls include retry logic with exponential backoff and a 60-second
circuit-breaker that triggers a trading halt if Kite is unreachable.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from kiteconnect import KiteConnect, KiteTicker

from core.config import get_settings
from core.redis_client import get_value, set_value
from core.redis_keys import HALT_KEY, KITE_TOKEN_KEY

logger = logging.getLogger(__name__)
MAX_RETRIES = 3
CIRCUIT_BREAKER_SECONDS = 60


def _retry_sync(func: Callable) -> Callable:
    """Decorator: retry a synchronous Kite SDK call with exponential backoff."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning(
                    "Kite API call %s failed (attempt %d/%d): %s — retrying in %ds",
                    func.__name__, attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
        logger.error("Kite API call %s exhausted retries", func.__name__)
        raise last_exc  # type: ignore[misc]
    return wrapper


class KiteClient:
    """Wrapper around the Kite Connect SDK with retry and circuit-breaker logic."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._kite: Optional[KiteConnect] = None
        self._ticker: Optional[KiteTicker] = None
        self._last_failure: float = 0.0
        self._cached_token: Optional[str] = None  # in-memory token cache

    def invalidate_token(self) -> None:
        """Evict the in-memory token so the next call re-reads from Redis.

        Must be called by the auth callback after each successful token refresh.
        """
        self._cached_token = None
        logger.info("KiteClient: access token cache invalidated")

    async def _get_access_token(self) -> str:
        """Return the access token, using the in-memory cache when available.

        The token is fetched from Redis only on the first call after startup
        or after ``invalidate_token()`` is called (at most once per day).
        """
        if self._cached_token:
            return self._cached_token
        token = await get_value(KITE_TOKEN_KEY)
        if not token:
            raise RuntimeError("No Kite access_token found in Redis. Please authenticate first.")
        self._cached_token = token
        return token

    async def get_kite(self) -> KiteConnect:
        """Return an authenticated KiteConnect instance."""
        token = await self._get_access_token()
        if self._kite is None:
            self._kite = KiteConnect(api_key=self._settings.kite_api_key)
        self._kite.set_access_token(token)
        return self._kite

    # ── Order Management ──────────────────────────

    async def place_order(
        self,
        tradingsymbol: str,
        exchange: str = "NSE",
        transaction_type: str = "BUY",
        quantity: int = 1,
        product: str = "MIS",
        order_type: str = "MARKET",
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        tag: str = "autotrader",
    ) -> str:
        """Place an order and return the order ID."""
        # ── Paper trading bypass ──────────────────────────────────────────────
        if self._settings.paper_trading:
            paper_id = f"paper_{uuid.uuid4().hex[:10]}"
            logger.info(
                "[PAPER] Order simulated: %s %s %s qty=%d product=%s → %s",
                transaction_type, exchange, tradingsymbol, quantity, product, paper_id,
            )
            return paper_id

        kite = await self.get_kite()

        @_retry_sync
        def _place():
            params: Dict[str, Any] = {
                "tradingsymbol": tradingsymbol,
                "exchange": exchange,
                "transaction_type": transaction_type,
                "quantity": quantity,
                "product": product,
                "order_type": order_type,
                "variety": "regular",
                "tag": tag,
            }
            if price is not None:
                params["price"] = price
            if trigger_price is not None:
                params["trigger_price"] = trigger_price
            return kite.place_order(**params)

        try:
            order_id = await asyncio.to_thread(_place)
            logger.info(
                "Order placed: %s %s %s qty=%d product=%s → order_id=%s",
                transaction_type, exchange, tradingsymbol, quantity, product, order_id,
            )
            self._last_failure = 0.0
            return order_id
        except Exception as exc:
            await self._handle_failure(exc)
            raise

    async def place_gtt(
        self,
        tradingsymbol: str,
        exchange: str,
        trigger_type: str,            # "single"
        trigger_values: List[float],
        last_price: float,
        orders: List[Dict[str, Any]],
    ) -> int:
        """Place a GTT (Good Till Triggered) stop-loss order. Returns GTT trigger ID."""
        # ── Paper trading bypass ──────────────────────────────────────────────
        if self._settings.paper_trading:
            paper_gtt_id = int(uuid.uuid4().int % 10_000_000)
            logger.info(
                "[PAPER] GTT simulated: %s trigger=%s → gtt_id=%d",
                tradingsymbol, trigger_values, paper_gtt_id,
            )
            return paper_gtt_id

        kite = await self.get_kite()

        @_retry_sync
        def _place_gtt():
            return kite.place_gtt(
                trigger_type=trigger_type,
                tradingsymbol=tradingsymbol,
                exchange=exchange,
                trigger_values=trigger_values,
                last_price=last_price,
                orders=orders,
            )

        try:
            gtt_id = await asyncio.to_thread(_place_gtt)
            logger.info("GTT placed for %s trigger=%s → gtt_id=%s", tradingsymbol, trigger_values, gtt_id)
            return gtt_id
        except Exception as exc:
            await self._handle_failure(exc)
            raise

    async def delete_gtt(self, trigger_id: int) -> None:
        """Cancel a server-side GTT order by its trigger ID.

        Called before placing a manual SELL so the GTT cannot fire in the
        window between our sell and its cancellation, which would create a
        naked short position.  Non-fatal if the GTT has already triggered
        or expired — callers should log the error and continue.
        """
        if self._settings.paper_trading:
            logger.info("[PAPER] GTT cancel simulated: trigger_id=%d", trigger_id)
            return

        kite = await self.get_kite()

        @_retry_sync
        def _delete():
            return kite.delete_gtt(trigger_id)

        try:
            await asyncio.to_thread(_delete)
            logger.info("GTT cancelled: trigger_id=%d", trigger_id)
        except Exception as exc:
            await self._handle_failure(exc)
            raise

    # ── Position & Order Queries ──────────────────

    async def get_positions(self) -> Dict[str, Any]:
        """Fetch current positions (net + day)."""
        kite = await self.get_kite()

        @_retry_sync
        def _fetch():
            return kite.positions()

        return await asyncio.to_thread(_fetch)

    async def get_orders(self) -> List[Dict[str, Any]]:
        """Fetch all orders for the day."""
        kite = await self.get_kite()

        @_retry_sync
        def _fetch():
            return kite.orders()

        return await asyncio.to_thread(_fetch)

    async def get_order_history(self, order_id: str) -> List[Dict[str, Any]]:
        """Fetch the lifecycle history of a specific order.

        Returns a list of status-update dicts.  The last entry with
        ``status == 'COMPLETE'`` contains the ``average_price`` (actual fill).
        """
        kite = await self.get_kite()

        @_retry_sync
        def _fetch():
            return kite.order_history(order_id)

        return await asyncio.to_thread(_fetch)

    async def get_holdings(self) -> List[Dict[str, Any]]:
        """Fetch CNC holdings."""
        kite = await self.get_kite()

        @_retry_sync
        def _fetch():
            return kite.holdings()

        return await asyncio.to_thread(_fetch)

    async def get_ltp(self, instruments: List[str]) -> Dict[str, Any]:
        """Fetch last traded price for a list of instruments (e.g. ['NSE:RELIANCE'])."""
        kite = await self.get_kite()

        @_retry_sync
        def _fetch():
            return kite.ltp(*instruments)

        return await asyncio.to_thread(_fetch)

    async def get_quote(self, instruments: List[str]) -> Dict[str, Any]:
        """Fetch full market quote including circuit limits for a list of instruments.

        The returned dict keyed by 'NSE:SYMBOL' includes:
          - last_price, upper_circuit_limit, lower_circuit_limit
          - depth (bid/ask), ohlc, volume, etc.

        Used for the pre-order circuit limit check (A3).
        """
        if self._settings.paper_trading:
            # In paper mode return a minimal stub — circuit check is skipped
            return {instr: {"last_price": 0.0, "upper_circuit_limit": 0.0,
                            "lower_circuit_limit": 0.0} for instr in instruments}

        kite = await self.get_kite()

        @_retry_sync
        def _fetch():
            return kite.quote(*instruments)

        try:
            result = await asyncio.to_thread(_fetch)
            self._last_failure = 0.0
            return result
        except Exception as exc:
            await self._handle_failure(exc)
            raise

    async def get_historical_data(
        self,
        instrument_token: int,
        interval: str = "day",
        days_back: int = 30,
    ) -> List[Dict[str, Any]]:
        """Fetch historical OHLCV candles for an instrument.

        Args:
            instrument_token: Kite instrument token (integer).
            interval: Kite interval string ("day", "minute", "5minute", etc.).
            days_back: How many calendar days back to fetch (extra days ensure
                       20 trading days even across weekends / NSE holidays).

        Returns:
            List of dicts with keys: date, open, high, low, close, volume.
        """
        from datetime import date, timedelta

        kite = await self.get_kite()
        to_date = date.today()
        from_date = to_date - timedelta(days=days_back)

        @_retry_sync
        def _fetch():
            return kite.historical_data(
                instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval=interval,
                continuous=False,
                oi=False,
            )

        try:
            result = await asyncio.to_thread(_fetch)
            self._last_failure = 0.0
            return result
        except Exception as exc:
            await self._handle_failure(exc)
            raise

    # ── KiteTicker (WebSocket) ───────────────────

    async def create_ticker(self) -> KiteTicker:
        """Create and return a KiteTicker instance for live tick streaming."""
        token = await self._get_access_token()
        self._ticker = KiteTicker(self._settings.kite_api_key, token)
        return self._ticker

    # ── Circuit Breaker ──────────────────────────

    async def _handle_failure(self, exc: Exception) -> None:
        """If Kite has been failing for > 60s, set the halt flag AND send
        a Telegram alert so the operator knows immediately."""
        now = time.time()
        if self._last_failure == 0.0:
            self._last_failure = now
        elif now - self._last_failure > CIRCUIT_BREAKER_SECONDS:
            logger.critical(
                "Kite API unreachable for >%ds — halting trading", CIRCUIT_BREAKER_SECONDS
            )
            await set_value(HALT_KEY, "TRUE")
            # Lazy import avoids circular dependency (telegram_client → core only)
            try:
                from integrations.telegram_client import send_halt_alert  # noqa: PLC0415
                await send_halt_alert()
            except Exception as tg_exc:
                logger.error("Telegram halt alert failed: %s", tg_exc)


# ── Module-level singleton ────────────────────────
_client: Optional[KiteClient] = None


def get_kite_client() -> KiteClient:
    """Return the singleton KiteClient."""
    global _client
    if _client is None:
        _client = KiteClient()
    return _client
