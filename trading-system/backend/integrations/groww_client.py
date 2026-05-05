"""
Groww API wrapper.
Centralised interface for order placement, position fetch, and WebSocket ticks.

Replaces kite_client.py — public interface is identical so all callers
(scanner.py, risk_manager.py, trading_agent.py, trades.py) require only an
import rename: `from integrations.groww_client import get_groww_client`.

Key differences from Kite:
  • Auth: TOTP-based login, no daily OAuth refresh. Access tokens are stored
    in Redis with no TTL (token persists until explicitly invalidated or
    user revokes via Groww app).
  • Orders: same REST semantics, different SDK object.
  • GTT → OCO: Groww uses OCO (One-Cancels-the-Other) bracket orders for
    atomic stop-loss + target. The public interface still calls them
    place_gtt/delete_gtt for drop-in compatibility.
  • WebSocket (GrowwFeed): delivers LTP + timestamp per tick. Does NOT deliver
    volume_traded or OHLC fields. The Scanner supplements these via periodic
    REST polling (see scanner.py).
  • Instruments: identified by trading symbol strings for REST calls.
    GrowwFeed WebSocket subscriptions use integer `exchange_token` (NOT
    Zerodha instrument tokens — different values even for the same stock).
  • Positions: `buy_quantity` / `sell_quantity` instead of `quantity`.
    Net position = buy_quantity - sell_quantity.
  • Historical data: uses trading symbol + exchange string, not integer token.

Circuit-breaker: same 60-second logic as kite_client. If the Groww API is
unreachable for > 60s, the HALT flag is set and a Telegram alert is sent.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

try:
    from growwapi import GrowwAPI as _GrowwAPI
except ImportError:  # SDK only available inside Docker image
    _GrowwAPI = None  # type: ignore[assignment,misc]

from core.config import get_settings
from core.nse_calendar import ist_today
from core.redis_client import get_value, set_value
from core.redis_keys import HALT_KEY, GROWW_TOKEN_KEY

logger = logging.getLogger(__name__)
MAX_RETRIES = 3
CIRCUIT_BREAKER_SECONDS = 60


def _retry_sync(func: Callable) -> Callable:
    """Decorator: retry a synchronous Groww SDK call with exponential backoff."""
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
                    "Groww API call %s failed (attempt %d/%d): %s — retrying in %ds",
                    func.__name__, attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
        logger.error("Groww API call %s exhausted retries", func.__name__)
        raise last_exc  # type: ignore[misc]
    return wrapper


class GrowwClient:
    """Wrapper around the Groww API SDK with retry and circuit-breaker logic.

    Drop-in replacement for KiteClient — all method signatures are identical.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._groww: Optional["_GrowwAPI"] = None  # type: ignore[type-arg]
        self._last_failure: float = 0.0
        self._cached_token: Optional[str] = None

    def invalidate_token(self) -> None:
        """Evict the in-memory token so the next call re-reads from Redis."""
        self._cached_token = None
        logger.info("GrowwClient: access token cache invalidated")

    async def _get_access_token(self) -> str:
        """Return the access token, using the in-memory cache when available."""
        if self._cached_token:
            return self._cached_token
        token = await get_value(GROWW_TOKEN_KEY)
        if not token:
            raise RuntimeError(
                "No Groww access_token found in Redis. "
                "Please authenticate via POST /api/auth/groww/login."
            )
        self._cached_token = token
        return token

    async def get_groww(self):
        """Return an authenticated GrowwAPI instance."""
        token = await self._get_access_token()
        if self._groww is None:
            self._groww = _GrowwAPI(token)
        else:
            # Refresh the token on the existing instance in case it was rotated
            self._groww.token = token
        return self._groww

    # ── Order Management ──────────────────────────────────────────────────────

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
        """Place an order and return the order ID.

        Groww MIS intraday = product type "INTRADAY".
        """
        if self._settings.paper_trading:
            paper_id = f"paper_{uuid.uuid4().hex[:10]}"
            logger.info(
                "[PAPER] Order simulated: %s %s %s qty=%d product=%s → %s",
                transaction_type, exchange, tradingsymbol, quantity, product, paper_id,
            )
            return paper_id

        groww = await self.get_groww()

        # Map Kite product codes to Groww equivalents
        groww_product = "INTRADAY" if product == "MIS" else "DELIVERY"
        groww_order_type = order_type  # MARKET / LIMIT — same strings

        @_retry_sync
        def _place():
            params: Dict[str, Any] = {
                "trading_symbol": tradingsymbol,
                "exchange": exchange,
                "transaction_type": transaction_type,
                "quantity": quantity,
                "product": groww_product,
                "order_type": groww_order_type,
                "source": tag,
            }
            if price is not None:
                params["price"] = price
            if trigger_price is not None:
                params["trigger_price"] = trigger_price
            return groww.place_order(**params)

        try:
            result = await asyncio.to_thread(_place)
            order_id = str(result.get("order_id") or result.get("orderId") or "")
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
        trigger_type: str,            # "single" in Kite; maps to OCO on Groww
        trigger_values: List[float],
        last_price: float,
        orders: List[Dict[str, Any]],
    ) -> int:
        """Place an OCO (stop-loss + target) bracket order.

        Groww does not have a GTT concept — it uses OCO orders instead.
        For our use case (single SL trigger), we place an OCO where:
          trigger_values[0] = stop-loss trigger price
          orders[0] contains the SL SELL order details

        Returns a synthetic integer ID derived from Groww's order_id for
        compatibility with the gtt_trigger_id column (stored as integer).
        """
        if self._settings.paper_trading:
            paper_gtt_id = int(uuid.uuid4().int % 10_000_000)
            logger.info(
                "[PAPER] OCO/GTT simulated: %s trigger=%s → gtt_id=%d",
                tradingsymbol, trigger_values, paper_gtt_id,
            )
            return paper_gtt_id

        groww = await self.get_groww()

        # Extract SL and target from the orders list
        # orders[0] = stop-loss order, orders[1] = target order (if two-legged)
        sl_order = orders[0]
        sl_price = trigger_values[0]

        @_retry_sync
        def _place_oco():
            params: Dict[str, Any] = {
                "trading_symbol": tradingsymbol,
                "exchange": exchange,
                "transaction_type": sl_order.get("transaction_type", "SELL"),
                "quantity": sl_order.get("quantity", 1),
                "product": "INTRADAY",
                "order_type": "SL",
                "trigger_price": sl_price,
                "price": sl_price,   # limit price = trigger for SL-M equivalent
                "source": "autotrader",
            }
            return groww.place_order(**params)

        try:
            result = await asyncio.to_thread(_place_oco)
            raw_id = str(result.get("order_id") or result.get("orderId") or "0")
            # Convert to integer for DB compatibility: take last 8 digits of order_id
            gtt_id = int("".join(c for c in raw_id if c.isdigit())[-8:] or "0")
            # Store the full string order_id alongside the int for cancellation
            await set_value(f"groww_oco:{gtt_id}", raw_id, ttl=24 * 3600)
            logger.info(
                "OCO/SL order placed for %s trigger=%.2f → gtt_id=%d (raw=%s)",
                tradingsymbol, sl_price, gtt_id, raw_id,
            )
            self._last_failure = 0.0
            return gtt_id
        except Exception as exc:
            await self._handle_failure(exc)
            raise

    async def delete_gtt(self, trigger_id: int) -> None:
        """Cancel an OCO/SL order by its synthetic integer ID.

        Looks up the real Groww order_id from Redis (set during place_gtt),
        then cancels it via the Groww REST API.
        """
        if self._settings.paper_trading:
            logger.info("[PAPER] OCO/GTT cancel simulated: trigger_id=%d", trigger_id)
            return

        raw_id = await get_value(f"groww_oco:{trigger_id}")
        if not raw_id:
            logger.warning(
                "delete_gtt: no Groww order_id found for gtt_id=%d — may already be filled/expired",
                trigger_id,
            )
            return

        groww = await self.get_groww()

        @_retry_sync
        def _cancel():
            return groww.cancel_order(order_id=raw_id)

        try:
            await asyncio.to_thread(_cancel)
            logger.info("OCO/SL order cancelled: gtt_id=%d (raw=%s)", trigger_id, raw_id)
        except Exception as exc:
            await self._handle_failure(exc)
            raise

    # ── Position & Order Queries ──────────────────────────────────────────────

    async def get_positions(self) -> Dict[str, Any]:
        """Fetch current positions.

        Groww returns buy_quantity and sell_quantity per position.
        We normalise to Kite's schema: net[].quantity = buy_qty - sell_qty
        so all callers (risk_manager, trading_agent) work without changes.
        """
        groww = await self.get_groww()

        @_retry_sync
        def _fetch():
            return groww.get_positions()

        raw = await asyncio.to_thread(_fetch)
        positions = raw if isinstance(raw, list) else raw.get("data", raw.get("positions", []))

        normalised = []
        for pos in positions:
            buy_qty = int(pos.get("buy_quantity") or pos.get("buyQuantity") or 0)
            sell_qty = int(pos.get("sell_quantity") or pos.get("sellQuantity") or 0)
            net_qty = buy_qty - sell_qty
            avg_price = float(
                pos.get("average_price") or pos.get("averagePrice") or
                pos.get("buy_avg") or 0
            )
            normalised.append({
                "tradingsymbol": pos.get("trading_symbol") or pos.get("tradingSymbol") or "",
                "exchange": pos.get("exchange", "NSE"),
                "quantity": net_qty,
                "average_price": avg_price,
                "product": pos.get("product", "MIS"),
            })

        return {"net": normalised, "day": normalised}

    async def get_orders(self) -> List[Dict[str, Any]]:
        """Fetch all orders for the day."""
        groww = await self.get_groww()

        @_retry_sync
        def _fetch():
            return groww.get_order_book()

        raw = await asyncio.to_thread(_fetch)
        return raw if isinstance(raw, list) else raw.get("data", raw.get("orders", []))

    async def get_order_history(self, order_id: str) -> List[Dict[str, Any]]:
        """Fetch the lifecycle history of a specific order.

        Returns a list of status-update dicts. The last entry with
        status == 'COMPLETE' contains average_price (actual fill).
        Groww returns a single order object — we wrap it in a list for
        Kite API compatibility.
        """
        groww = await self.get_groww()

        @_retry_sync
        def _fetch():
            return groww.get_order_details(order_id=order_id)

        raw = await asyncio.to_thread(_fetch)
        order = raw if isinstance(raw, dict) else raw.get("data", {})

        # Normalise to Kite's history list schema
        status = (order.get("status") or order.get("order_status") or "").upper()
        kite_status = "COMPLETE" if status in ("COMPLETE", "EXECUTED", "FILLED") else status
        return [{
            "order_id": order_id,
            "status": kite_status,
            "average_price": float(order.get("average_price") or order.get("avg_price") or 0),
            "filled_quantity": int(order.get("filled_quantity") or order.get("filledQty") or 0),
        }]

    async def get_holdings(self) -> List[Dict[str, Any]]:
        """Fetch CNC holdings."""
        groww = await self.get_groww()

        @_retry_sync
        def _fetch():
            return groww.get_holdings()

        raw = await asyncio.to_thread(_fetch)
        return raw if isinstance(raw, list) else raw.get("data", raw.get("holdings", []))

    async def get_ltp(self, instruments: List[str]) -> Dict[str, Any]:
        """Fetch last traded price for a list of instruments (e.g. ['NSE:RELIANCE']).

        Groww uses trading_symbol + exchange rather than Kite's combined string.
        We parse the 'NSE:SYMBOL' format for compatibility.
        Returns a dict keyed by 'NSE:SYMBOL' → {'last_price': float} to match
        Kite's response shape.
        """
        groww = await self.get_groww()
        result: Dict[str, Any] = {}

        for instrument in instruments:
            # Parse 'NSE:RELIANCE' → exchange='NSE', symbol='RELIANCE'
            if ":" in instrument:
                exchange, symbol = instrument.split(":", 1)
            else:
                exchange, symbol = "NSE", instrument

            @_retry_sync
            def _fetch(sym=symbol, exch=exchange):
                return groww.get_ltp(trading_symbol=sym, exchange=exch)

            try:
                raw = await asyncio.to_thread(_fetch)
                ltp = float(raw.get("ltp") or raw.get("last_price") or 0)
                result[instrument] = {"last_price": ltp}
            except Exception as exc:
                logger.warning("get_ltp failed for %s: %s", instrument, exc)
                result[instrument] = {"last_price": 0.0}

        return result

    async def get_quote(self, instruments: List[str]) -> Dict[str, Any]:
        """Fetch full market quote including circuit limits.

        Groww's quote API returns upper/lower circuit limits under different
        field names — we normalise to Kite's schema for circuit-check (A3).
        """
        if self._settings.paper_trading:
            return {instr: {"last_price": 0.0, "upper_circuit_limit": 0.0,
                            "lower_circuit_limit": 0.0} for instr in instruments}

        groww = await self.get_groww()
        result: Dict[str, Any] = {}

        for instrument in instruments:
            if ":" in instrument:
                exchange, symbol = instrument.split(":", 1)
            else:
                exchange, symbol = "NSE", instrument

            @_retry_sync
            def _fetch(sym=symbol, exch=exchange):
                return groww.get_quote(trading_symbol=sym, exchange=exch)

            try:
                raw = await asyncio.to_thread(_fetch)
                result[instrument] = {
                    "last_price": float(raw.get("ltp") or raw.get("last_price") or 0),
                    "upper_circuit_limit": float(
                        raw.get("upper_circuit") or raw.get("upperCircuit") or
                        raw.get("upper_circuit_limit") or 0
                    ),
                    "lower_circuit_limit": float(
                        raw.get("lower_circuit") or raw.get("lowerCircuit") or
                        raw.get("lower_circuit_limit") or 0
                    ),
                }
            except Exception as exc:
                await self._handle_failure(exc)
                raise

        return result

    async def get_ohlcv_snapshot(self, symbol: str) -> Dict[str, Any]:
        """Return today's session OHLCV snapshot for *symbol*.

        Called by Scanner._start_ohlcv_poll_loop() every ~60 seconds.
        GrowwFeed delivers only LTP + timestamp; open, high, low, close,
        and cumulative volume must be supplemented via REST.

        In paper mode returns {} immediately — MockTickGenerator injects full
        OHLC data into every tick via tick["ohlc"], so no REST supplement is needed.

        Returns:
            Dict with keys "open", "high", "low", "close", "volume", or {} on
            paper mode.  Caller is responsible for treating 0-values as missing.
        """
        if self._settings.paper_trading:
            return {}

        groww = await self.get_groww()

        @_retry_sync
        def _fetch():
            return groww.get_quote(trading_symbol=symbol, exchange="NSE")

        try:
            raw = await asyncio.to_thread(_fetch)
        except Exception as exc:
            logger.warning("get_ohlcv_snapshot failed for %s: %s", symbol, exc)
            return {}

        return {
            "open":   float(raw.get("open")  or raw.get("open_price")  or 0),
            "high":   float(raw.get("high")  or raw.get("high_price")  or raw.get("day_high")  or 0),
            "low":    float(raw.get("low")   or raw.get("low_price")   or raw.get("day_low")   or 0),
            "close":  float(raw.get("close") or raw.get("prev_close")  or raw.get("close_price") or 0),
            "volume": int(  raw.get("volume") or raw.get("total_volume") or raw.get("volume_traded") or 0),
        }

    async def get_historical_data(
        self,
        instrument_token: int,
        interval: str = "day",
        days_back: int = 30,
    ) -> List[Dict[str, Any]]:
        """Fetch historical OHLCV candles.

        Groww's historical API uses trading symbol + exchange, not integer tokens.
        We look up the symbol from the instrument_service reverse map.

        The instrument_token parameter is retained for API compatibility —
        it is the Groww exchange_token integer (same column, different values
        from Zerodha tokens).

        Args:
            instrument_token: Groww exchange_token integer.
            interval: candle interval — "1d" for daily, "1" for 1-minute, "5" for 5-minute.
            days_back: calendar days of history to fetch.

        Returns:
            List of dicts with keys: date, open, high, low, close, volume.
        """
        from datetime import date, timedelta
        from integrations.instrument_service import get_symbol

        symbol = get_symbol(instrument_token)
        if not symbol:
            logger.warning(
                "get_historical_data: no symbol found for token %d", instrument_token
            )
            return []

        # Map Kite interval strings to Groww equivalents
        interval_map = {
            "day": "1d",
            "minute": "1",
            "5minute": "5",
            "15minute": "15",
            "30minute": "30",
            "60minute": "60",
        }
        groww_interval = interval_map.get(interval, interval)

        groww = await self.get_groww()
        to_date = ist_today()
        from_date = to_date - timedelta(days=days_back)

        @_retry_sync
        def _fetch():
            return groww.get_historical_data(
                trading_symbol=symbol,
                exchange="NSE",
                interval=groww_interval,
                from_date=from_date.strftime("%Y-%m-%d"),
                to_date=to_date.strftime("%Y-%m-%d"),
            )

        try:
            raw = await asyncio.to_thread(_fetch)
            candles = raw if isinstance(raw, list) else raw.get("data", raw.get("candles", []))
            # Normalise to Kite candle schema: {date, open, high, low, close, volume}
            normalised = []
            for c in candles:
                normalised.append({
                    "date": c.get("date") or c.get("timestamp") or c.get("t"),
                    "open": float(c.get("open") or c.get("o") or 0),
                    "high": float(c.get("high") or c.get("h") or 0),
                    "low": float(c.get("low") or c.get("l") or 0),
                    "close": float(c.get("close") or c.get("c") or 0),
                    "volume": int(c.get("volume") or c.get("v") or 0),
                })
            self._last_failure = 0.0
            return normalised
        except Exception as exc:
            await self._handle_failure(exc)
            raise

    # ── GrowwFeed WebSocket ───────────────────────────────────────────────────

    async def create_ticker(self):
        """Create and return a GrowwFeed WebSocket instance.

        GrowwFeed delivers only LTP + timestamp per tick — it does NOT send
        volume_traded or OHLC fields. The Scanner supplements these via a
        periodic REST polling thread (see scanner.py _start_ohlcv_poll_thread).

        Returns a GrowwFeed-like object with the same callback interface as
        KiteTicker (on_ticks, on_connect, on_close, on_error) for drop-in
        compatibility in scanner.py.
        """
        token = await self._get_access_token()
        from growwapi import GrowwFeed
        # GrowwFeed takes a GrowwAPI instance, not raw credentials.
        # get_groww() returns an authenticated GrowwAPI with the current token.
        groww = await self.get_groww()
        ticker = GrowwFeed(groww_api=groww)
        return ticker

    # ── Circuit Breaker ───────────────────────────────────────────────────────

    async def _handle_failure(self, exc: Exception) -> None:
        """If Groww API has been failing for > 60s, set halt flag and alert."""
        now = time.time()
        if self._last_failure == 0.0:
            self._last_failure = now
        elif now - self._last_failure > CIRCUIT_BREAKER_SECONDS:
            logger.critical(
                "Groww API unreachable for >%ds — halting trading", CIRCUIT_BREAKER_SECONDS
            )
            await set_value(HALT_KEY, "TRUE")
            try:
                from integrations.telegram_client import send_halt_alert
                await send_halt_alert()
            except Exception as tg_exc:
                logger.error("Telegram halt alert failed: %s", tg_exc)


# ── Module-level singleton ────────────────────────────────────────────────────
_client: Optional[GrowwClient] = None


def get_groww_client() -> GrowwClient:
    """Return the singleton GrowwClient."""
    global _client
    if _client is None:
        _client = GrowwClient()
    return _client


# ── Backward-compatibility alias ─────────────────────────────────────────────
# Allows gradual migration: `from integrations.groww_client import get_kite_client`
# still works in any file not yet updated.
get_kite_client = get_groww_client
