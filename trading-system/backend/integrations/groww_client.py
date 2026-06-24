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


def _is_permanent_groww_error(exc: Exception) -> bool:
    """Return True for errors that retrying cannot fix (HTTP 4xx auth/permission).

    A 403 "Access forbidden" means the account's Trading API subscription does
    not include the requested API group (e.g. Live Data / Historical Data) — it
    is not a transient network blip.  Retrying such calls 3× with exponential
    backoff only wastes ~14s per call and floods the logs.  We surface the
    failure immediately instead.
    """
    code = str(getattr(exc, "code", "") or "")
    if code in ("401", "403"):
        return True
    msg = str(exc).lower()
    return "access forbidden" in msg or "unauthori" in msg


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
                # Permanent 4xx (forbidden / unauthorised) — retrying is futile.
                # Fail fast so the real cause (missing data-API entitlement) is
                # obvious and startup is not delayed by backoff sleeps.
                if _is_permanent_groww_error(exc):
                    logger.error(
                        "Groww API call %s rejected with a permanent error "
                        "(%s) — not retrying. This usually means the Trading API "
                        "subscription does not grant access to this endpoint.",
                        func.__name__, exc,
                    )
                    raise
                wait = 2 ** attempt
                logger.warning(
                    "Groww API call %s failed (attempt %d/%d): %s — retrying in %ds",
                    func.__name__, attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
        logger.error("Groww API call %s exhausted retries", func.__name__)
        raise last_exc  # type: ignore[misc]
    return wrapper


class _GrowwFeedKiteTicker:
    """KiteTicker-compatible adapter around the official GrowwFeed API.

    The scanner was written against the Zerodha KiteTicker interface:
      • ticker.on_ticks / on_connect / on_close / on_error  (callback attributes)
      • ticker.connect(reconnect=True)  (blocking call)
      • ws.subscribe(tokens)  +  ws.set_mode(mode, tokens)  (in on_connect)
      • ticker.MODE_FULL  (mode constant)

    GrowwFeed's actual SDK API is completely different:
      • feed.subscribe_ltp(instruments, on_data_received=cb)
      • feed.subscribe_index_value(instruments, on_data_received=cb)
      • feed.consume()   ← blocking call

    This adapter bridges the two so scanner.py requires zero changes.

    Tick format emitted to on_ticks:
      [{"instrument_token": <int>, "exchange_token": <int>,
        "ltp": <float>, "last_price": <float>}]
    """

    # Mode constant required by scanner._on_connect → ws.set_mode(ws.MODE_FULL, tokens)
    MODE_FULL = "ltp"
    MODE_QUOTE = "ltp"
    MODE_LTP = "ltp"

    def __init__(self, feed=None, groww=None) -> None:
        # Either an already-created feed (tests/mocks) or a GrowwAPI client that
        # we will use to lazily construct GrowwFeed inside connect().
        self._feed = feed
        self._groww = groww
        self._subscribed_tokens: List[int] = []

        # Callbacks — assigned by scanner.py before connect() is called
        self.on_ticks = None
        self.on_connect = None
        self.on_close = None
        self.on_error = None

    def subscribe(self, tokens: List[int]) -> None:
        """Store token list.  Called from scanner._on_connect via ws.subscribe()."""
        self._subscribed_tokens = list(tokens)

    def set_mode(self, mode: str, tokens: List[int]) -> None:
        """No-op: GrowwFeed always delivers LTP only."""

    def connect(self, reconnect: bool = True) -> None:
        """Connect to GrowwFeed and block until it closes or raises.

        This method is called from scanner.py inside run_in_executor so it
        runs on a thread-pool thread, not the event loop.

        Flow:
          1. Fire on_connect(self, {}) → scanner._on_connect calls
             ws.subscribe(tokens) which populates self._subscribed_tokens.
          2. Build the GrowwFeed instrument lists from those tokens.
          3. Register subscriptions with callbacks that convert GrowwFeed's
             data format to KiteTicker-style tick dicts.
          4. Call feed.consume() — blocks until disconnect or exception.
        """
        # IMPORTANT: GrowwFeed uses asyncio internally for all operations
        # (subscribe, consume, etc.).  This connect() method runs inside
        # run_in_executor on a thread-pool thread.  Without a fresh event loop,
        # the thread inherits the main FastAPI loop via asyncio.get_event_loop()
        # → "Cannot run the event loop while another loop is running".
        # Install a brand-new loop FIRST, before any GrowwFeed calls.
        _stage = "init_loop"
        _thread_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_thread_loop)

        try:
            # Construct GrowwFeed in this worker thread so any internal
            # run_until_complete calls do not execute on FastAPI's running loop.
            _stage = "create_feed"
            if self._feed is None:
                if self._groww is None:
                    raise RuntimeError("GrowwFeed adapter missing feed and groww client")
                from growwapi import GrowwFeed
                self._feed = GrowwFeed(self._groww)

            # Lazy import to avoid circular-import at module load time.
            _stage = "import_nifty_token"
            try:
                from integrations.instrument_service import NIFTY50_TOKEN as _NIFTY_TOKEN
            except Exception:
                _NIFTY_TOKEN = 2999  # hardcoded fallback

            # Step 1 — fire on_connect to collect subscription tokens.
            _stage = "on_connect"
            if self.on_connect:
                self.on_connect(self, {})

            # Step 2 — split tokens into equity and index buckets.
            _stage = "build_instrument_buckets"
            stock_instruments: List[Dict[str, str]] = []
            index_instruments: List[Dict[str, str]] = []
            for token in self._subscribed_tokens:
                if token == _NIFTY_TOKEN:
                    index_instruments.append(
                        {"exchange": "NSE", "segment": "CASH", "exchange_token": "NIFTY"}
                    )
                else:
                    stock_instruments.append(
                        {"exchange": "NSE", "segment": "CASH", "exchange_token": str(token)}
                    )

            # Step 3 — register callbacks with GrowwFeed.
            _stage = "register_callbacks"
            feed = self._feed
            on_ticks_cb = self.on_ticks

            def _safe_float(value: Any) -> float:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return 0.0

            def _extract_price(payload: Any) -> float:
                """Best-effort extraction across observed GrowwFeed payload shapes."""
                if isinstance(payload, dict):
                    direct = _safe_float(
                        payload.get("ltp")
                        or payload.get("last_price")
                        or payload.get("value")
                        or payload.get("price")
                    )
                    if direct > 0:
                        return direct
                    nested = payload.get("data")
                    if isinstance(nested, dict):
                        nested_val = _safe_float(
                            nested.get("ltp")
                            or nested.get("last_price")
                            or nested.get("value")
                            or nested.get("price")
                        )
                        if nested_val > 0:
                            return nested_val
                return 0.0

            if stock_instruments:
                def _on_stock_data(meta) -> None:
                    meta = meta or {}
                    exchange = meta.get("exchange", "NSE")
                    segment = meta.get("segment", "CASH")
                    token_str = str(meta.get("feed_key") or meta.get("exchange_token") or "")
                    try:
                        token_int = int(token_str)
                    except (ValueError, TypeError):
                        return

                    ltp_val = _extract_price(meta)
                    if ltp_val <= 0:
                        try:
                            ltp_data = (
                                feed.get_ltp()
                                .get(exchange, {})
                                .get(segment, {})
                                .get(token_str, {})
                            )
                            ltp_val = _safe_float(ltp_data.get("ltp"))
                        except Exception as exc:
                            logger.warning(
                                "GrowwFeed LTP snapshot fetch failed in callback: %s: %s",
                                type(exc).__name__,
                                exc,
                            )
                            return

                    if ltp_val > 0 and on_ticks_cb:
                        try:
                            on_ticks_cb(self, [{
                                "instrument_token": token_int,
                                "exchange_token": token_int,
                                "ltp": ltp_val,
                                "last_price": ltp_val,
                            }])
                        except Exception as exc:
                            logger.warning(
                                "Scanner tick callback failed for token %s: %s: %s",
                                token_str,
                                type(exc).__name__,
                                exc,
                            )

                _stage = "subscribe_ltp"
                feed.subscribe_ltp(stock_instruments, on_data_received=_on_stock_data)

            if index_instruments:
                def _on_index_data(meta) -> None:
                    meta = meta or {}
                    exchange = meta.get("exchange", "NSE")
                    segment = meta.get("segment", "CASH")
                    token_str = str(meta.get("feed_key") or meta.get("exchange_token") or "")

                    idx_val = _extract_price(meta)
                    if idx_val <= 0:
                        try:
                            idx_data = (
                                feed.get_index_value()
                                .get(exchange, {})
                                .get(segment, {})
                                .get(token_str, {})
                            )
                            idx_val = _safe_float(idx_data.get("value"))
                        except Exception as exc:
                            logger.warning(
                                "GrowwFeed index snapshot fetch failed in callback: %s: %s",
                                type(exc).__name__,
                                exc,
                            )
                            return

                    if idx_val > 0 and on_ticks_cb:
                        try:
                            on_ticks_cb(self, [{
                                "instrument_token": _NIFTY_TOKEN,
                                "exchange_token": _NIFTY_TOKEN,
                                "ltp": idx_val,
                                "last_price": idx_val,
                            }])
                        except Exception as exc:
                            logger.warning(
                                "Scanner tick callback failed for index %s: %s: %s",
                                token_str,
                                type(exc).__name__,
                                exc,
                            )

                _stage = "subscribe_index_value"
                feed.subscribe_index_value(index_instruments, on_data_received=_on_index_data)

            # Step 4 — block until done.
            _stage = "consume"
            feed.consume()
        except Exception as exc:
            logger.exception(
                "GrowwFeed adapter connect failed at stage=%s: %s: %s",
                _stage,
                type(exc).__name__,
                exc,
            )
            if self.on_error:
                self.on_error(self, None, f"stage={_stage}: {exc}")
            raise
        else:
            if self.on_close:
                self.on_close(self, None, "feed closed normally")
        finally:
            _thread_loop.close()
            asyncio.set_event_loop(None)


class GrowwClient:
    """Wrapper around the Groww API SDK with retry and circuit-breaker logic.

    Drop-in replacement for KiteClient — all method signatures are identical.
    """

    def __init__(self) -> None:
        self._settings = get_settings()
        self._groww: Optional["_GrowwAPI"] = None  # type: ignore[type-arg]
        self._last_failure: float = 0.0
        self._cached_token: Optional[str] = None
        self._zero_volume_warned_symbols: set[str] = set()

    @staticmethod
    def _extract_quote_volume(raw: Dict[str, Any]) -> int:
        """Extract cumulative traded volume from heterogeneous Groww quote payloads.

        Groww SDK payload keys are not fully stable across versions/environments.
        Try common aliases first, then inspect nested dicts used by some releases.
        Returns 0 when no usable volume field is present.
        """
        candidate_keys = (
            "volume",
            "total_volume",
            "volume_traded",
            "traded_volume",
            "volumeTraded",
            "volumeTradedToday",
            "totalTradedVolume",
            "todayVolume",
            "dayVolume",
            "v",
        )

        for key in candidate_keys:
            val = raw.get(key)
            if val is not None:
                try:
                    iv = int(float(val))
                    if iv >= 0:
                        return iv
                except (TypeError, ValueError):
                    pass

        # Some payloads nest quote stats under one of these objects.
        for nested_key in ("quote", "quote_data", "market_data", "stats", "market_stats"):
            nested = raw.get(nested_key)
            if not isinstance(nested, dict):
                continue
            for key in candidate_keys:
                val = nested.get(key)
                if val is not None:
                    try:
                        iv = int(float(val))
                        if iv >= 0:
                            return iv
                    except (TypeError, ValueError):
                        pass

        return 0

    def invalidate_token(self) -> None:
        """Evict the in-memory token so the next call re-reads from Redis."""
        self._cached_token = None
        self._groww = None
        logger.info("GrowwClient: access token cache invalidated")

    async def reauthenticate(self) -> None:
        """Re-authenticate with Groww using TOTP and store the new token in Redis.

        Called automatically when a GrowwAPIAuthenticationException is detected
        (i.e. the session token expired).  Requires GROWW_TOTP_SECRET to be
        configured in .env.  Resets the circuit-breaker timer so a successful
        re-auth doesn't leave the halt flag armed.
        """
        settings = self._settings
        if not (settings.groww_client_id and settings.groww_totp_secret):
            raise RuntimeError(
                "Cannot reauthenticate: GROWW_CLIENT_ID or GROWW_TOTP_SECRET not configured"
            )
        import pyotp
        totp_code = pyotp.TOTP(settings.groww_totp_secret).now()
        new_token = await asyncio.to_thread(
            lambda: _GrowwAPI.get_access_token(
                api_key=settings.groww_client_id,
                totp=totp_code,
            )
        )
        await set_value(GROWW_TOKEN_KEY, new_token)
        self._groww = None           # force fresh GrowwAPI instance on next call
        self._cached_token = None    # clear in-memory token cache
        self._last_failure = 0.0     # reset circuit-breaker timer
        logger.info("✅ Groww re-authenticated — new session token stored in Redis")

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

        Groww's bulk LTP API: get_ltp(segment=, exchange_trading_symbols=)
        where exchange_trading_symbols is a tuple of strings like 'NSE_RELIANCE'.
        Response: {"NSE_RELIANCE": 2500.5, "NSE_NIFTY": 22962.10}  (flat dict).

        Returns a dict keyed by 'NSE:SYMBOL' → {'last_price': float} to match
        Kite's response shape.
        """
        groww = await self.get_groww()

        # Build exchange_trading_symbols and a reverse map back to 'NSE:SYMBOL' keys.
        et_symbols: List[str] = []
        sym_map: Dict[str, str] = {}
        for instrument in instruments:
            if ":" in instrument:
                exchange, symbol = instrument.split(":", 1)
            else:
                exchange, symbol = "NSE", instrument
            et_sym = f"{exchange}_{symbol}"
            et_symbols.append(et_sym)
            sym_map[et_sym] = instrument

        @_retry_sync
        def _fetch():
            # Groww accepts a single string or a tuple of strings.
            return groww.get_ltp(
                segment="CASH",
                exchange_trading_symbols=tuple(et_symbols),
            )

        try:
            raw = await asyncio.to_thread(_fetch)
            # raw = {"NSE_RELIANCE": 2500.5, ...}  — values are plain floats
            result: Dict[str, Any] = {}
            for et_sym, price in raw.items():
                orig_key = sym_map.get(et_sym, et_sym.replace("_", ":", 1))
                result[orig_key] = {"last_price": float(price or 0)}
            # Fill missing instruments with 0.0
            for instrument in instruments:
                if instrument not in result:
                    result[instrument] = {"last_price": 0.0}
            return result
        except Exception as exc:
            logger.warning("get_ltp failed for %s: %s", instruments, exc)
            return {instr: {"last_price": 0.0} for instr in instruments}

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
                # segment is required; equity stocks are always CASH segment.
                return groww.get_quote(exchange=exch, segment="CASH", trading_symbol=sym)

            try:
                raw = await asyncio.to_thread(_fetch)
                result[instrument] = {
                    "last_price": float(raw.get("last_price") or raw.get("ltp") or 0),
                    "upper_circuit_limit": float(
                        raw.get("upper_circuit_limit") or raw.get("upper_circuit") or
                        raw.get("upperCircuit") or 0
                    ),
                    "lower_circuit_limit": float(
                        raw.get("lower_circuit_limit") or raw.get("lower_circuit") or
                        raw.get("lowerCircuit") or 0
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

        MockTickGenerator (active when credentials are absent) injects full OHLC
        data into every tick via tick["ohlc"], so no REST supplement is needed in
        that mode.  When GrowwFeed is active (credentials present), GrowwFeed
        delivers only LTP + timestamp — OHLCV must be fetched via REST in both
        paper-trading and live mode.

        Returns:
            Dict with keys "open", "high", "low", "close", "volume", or {} when
            MockTickGenerator is active.  Caller treats 0-values as missing.
        """
        # MockTickGenerator is active (no credentials) — OHLC injected per tick.
        if not self._settings.groww_client_id:
            return {}

        groww = await self.get_groww()

        @_retry_sync
        def _fetch():
            return groww.get_quote(trading_symbol=symbol, exchange="NSE", segment="CASH")

        try:
            raw = await asyncio.to_thread(_fetch)
        except Exception as exc:
            logger.warning("get_ohlcv_snapshot failed for %s: %s", symbol, exc)
            return {}

        # Response structure typically contains:
        #   {"ohlc": {"open": ..., "high": ..., "low": ..., "close": ...},
        #    "volume"|"volumeTradedToday"|..., "last_price": ..., ...}
        # The "ohlc.close" is the PREVIOUS day's settlement price.  "last_price" is the
        # current LTP, which is the better intraday "close" for indicator calculation.
        ohlc = raw.get("ohlc", {})
        volume = self._extract_quote_volume(raw)
        last_price = float(raw.get("last_price") or ohlc.get("close") or 0)
        if volume == 0 and last_price > 0 and symbol not in self._zero_volume_warned_symbols:
            self._zero_volume_warned_symbols.add(symbol)
            logger.warning(
                "get_ohlcv_snapshot: %s quote had LTP but no usable volume field. "
                "Observed keys: %s",
                symbol,
                sorted(list(raw.keys())),
            )
        return {
            "open":   float(ohlc.get("open")  or 0),
            "high":   float(ohlc.get("high")  or 0),
            "low":    float(ohlc.get("low")   or 0),
            "close":  last_price,
            "volume": volume,
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
            interval: candle interval — "day", "minute", "5minute", etc.
            days_back: calendar days of history to fetch.

        Returns:
            List of dicts with keys: date, open, high, low, close, volume.
        """
        from datetime import timedelta
        from integrations.instrument_service import get_symbol

        symbol = get_symbol(instrument_token)
        if not symbol:
            logger.warning(
                "get_historical_data: no symbol found for token %d", instrument_token
            )
            return []

        # Map Kite interval strings to Groww CANDLE_INTERVAL constants
        interval_map = {
            "day":      "1day",
            "minute":   "1minute",
            "5minute":  "5minute",
            "15minute": "15minute",
            "30minute": "30minute",
            "60minute": "60minute",
        }
        groww_interval = interval_map.get(interval, "1day")

        # Groww equity groww_symbol format: "NSE-{SYMBOL}"
        groww_symbol = f"NSE-{symbol}"

        groww = await self.get_groww()
        to_date = ist_today()
        from_date = to_date - timedelta(days=days_back)

        @_retry_sync
        def _fetch():
            return groww.get_historical_candles(
                exchange="NSE",
                segment="CASH",
                groww_symbol=groww_symbol,
                start_time=from_date.strftime("%Y-%m-%d 09:15:00"),
                end_time=to_date.strftime("%Y-%m-%d 15:30:00"),
                candle_interval=groww_interval,
            )

        try:
            raw = await asyncio.to_thread(_fetch)
            # Response: {"candles": [[timestamp, o, h, l, c, v, oi], ...], ...}
            candle_list = raw.get("candles", [])
            normalised = []
            for c in candle_list:
                if isinstance(c, (list, tuple)) and len(c) >= 6:
                    normalised.append({
                        "date":   c[0],
                        "open":   float(c[1] or 0),
                        "high":   float(c[2] or 0),
                        "low":    float(c[3] or 0),
                        "close":  float(c[4] or 0),
                        "volume": int(c[5] or 0),
                    })
                elif isinstance(c, dict):
                    normalised.append({
                        "date":   c.get("date") or c.get("timestamp") or c.get("t"),
                        "open":   float(c.get("open") or c.get("o") or 0),
                        "high":   float(c.get("high") or c.get("h") or 0),
                        "low":    float(c.get("low") or c.get("l") or 0),
                        "close":  float(c.get("close") or c.get("c") or 0),
                        "volume": int(c.get("volume") or c.get("v") or 0),
                    })
            return normalised
        except Exception as exc:
            # Historical data is used only for volume baseline — a failure here
            # must NOT trip the circuit-breaker halt.  The scanner already
            # catches this exception and disables the volume filter for the
            # affected symbol rather than stopping trading.
            logger.warning("get_historical_data failed for %s: %s", symbol, exc)
            raise

    # ── GrowwFeed WebSocket ───────────────────────────────────────────────────

    async def create_ticker(self):
        """Create and return a KiteTicker-compatible GrowwFeed adapter.

        Returns a _GrowwFeedKiteTicker that wraps the official GrowwFeed
        API (subscribe_ltp + consume) behind the KiteTicker callback
        interface that scanner.py expects.
        """
        groww = await self.get_groww()
        # IMPORTANT: do not construct GrowwFeed on the FastAPI event loop thread.
        # The adapter creates it lazily inside connect() (executor thread).
        token_tail = (self._cached_token or "")[-6:] or "?"
        logger.debug("GrowwFeed adapter created (token tail=...%s)", token_tail)
        return _GrowwFeedKiteTicker(groww=groww)

    # ── Circuit Breaker ───────────────────────────────────────────────────────

    async def _handle_failure(self, exc: Exception) -> None:
        """If Groww API has been failing for > 60s, set halt flag and alert.

        Authentication failures (token expired) are handled separately:
        they trigger automatic re-authentication rather than a circuit-breaker
        halt, since a 401 means the session token expired — not that the API
        is down.  Re-auth resets the failure timer.
        """
        # --- Auth failure: reauthenticate, do NOT trip the circuit breaker ---
        _is_auth = False
        try:
            from growwapi.groww.exceptions import (
                GrowwAPIAuthenticationException,
                GrowwFeedConnectionException,
            )
            if isinstance(exc, GrowwAPIAuthenticationException):
                _is_auth = True
            elif isinstance(exc, GrowwFeedConnectionException):
                # GrowwFeed wraps many auth handshake failures in this type.
                # Treat all such failures as potentially auth-related first.
                _is_auth = True
        except ImportError:
            pass
        # Fallback: message-based detection regardless of exception type.
        if not _is_auth:
            _msg = str(exc).lower()
            _is_auth = "authentication failed" in _msg or "unauthori" in _msg

        if _is_auth:
            logger.warning(
                "Groww auth failure detected (%s: %s) — attempting automatic re-authentication",
                type(exc).__name__, exc,
            )
            try:
                await self.reauthenticate()
                logger.info("Re-authentication succeeded — circuit breaker reset")
            except Exception as reauth_exc:
                logger.error(
                    "Re-authentication failed: %s — manual login via "
                    "POST /api/auth/groww/login required",
                    reauth_exc,
                )
            return  # never halt on auth errors

        # --- Network / API failure: apply circuit-breaker logic ---
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
