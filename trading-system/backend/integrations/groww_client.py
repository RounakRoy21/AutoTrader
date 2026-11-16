"""
Groww API wrapper.
Centralised interface for order placement, position fetch, and WebSocket ticks.
The trading code reaches it through ``integrations.groww_broker.GrowwBroker``
(the live ``BrokerPort``); market-data readers use ``get_groww_client()``.

Notes:
  • Auth: TOTP-based login, no daily OAuth refresh. Access tokens are stored
    in Redis with no TTL (token persists until explicitly invalidated or
    user revokes via Groww app).
  • Broker stop: a single plain SL-limit SELL order (no OCO, no target leg —
    the target is enforced client-side by the RiskManager poll).  See
    ``place_stop_loss_order``.
  • Order writes (place / cancel) are NEVER retried automatically: every
    placed order carries a unique order_reference_id and an ambiguous failure
    (timeout / connection error) is resolved by looking the order up by that
    reference instead of resubmitting (audit R5 / C3).
  • Prices sent to the broker are snapped to the instrument tick size
    (core.tick_size, audit R10 / C6).
  • WebSocket (GrowwFeed): delivers LTP + timestamp per tick. Does NOT deliver
    volume_traded or OHLC fields. The Scanner supplements these via periodic
    REST polling (see scanner.py).
  • Instruments: identified by trading symbol strings for REST calls.
    GrowwFeed WebSocket subscriptions use integer `exchange_token`.
  • Positions: read with ``get_positions_for_user(segment="CASH")``; the net
    quantity is ``quantity`` (else credit - debit, else buy - sell).
  • Orders: read with ``get_order_list`` (paged) / ``get_order_detail``.
    Every SDK read passes ``timeout=READ_TIMEOUT_S`` (the SDK default is
    infinite).
  • Historical data: uses trading symbol + exchange string, not integer token.

Circuit-breaker: if the Groww API is unreachable for > 60s, the HALT flag is
set and a Telegram alert is sent.
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
from core.tick_size import ceil_to_tick, floor_to_tick, get_tick_size, sl_limit_price
from core.redis_client import get_value, set_value
from core.redis_keys import GROWW_TOKEN_KEY
from core.risk_state import set_halt

logger = logging.getLogger(__name__)
MAX_RETRIES = 3
CIRCUIT_BREAKER_SECONDS = 60

# Order-write safety (audit R5 / C3 / R12)
ORDER_WRITE_TIMEOUT_S = 10          # HTTP timeout passed to the SDK for writes
ORDER_LOOKUP_ATTEMPTS = 3           # reference lookups after an ambiguous failure
ORDER_LOOKUP_DELAY_S = 1.0          # async (non-blocking) delay between lookups
SL_LIMIT_BUFFER_PCT = 0.005         # SL-limit: limit price 0.5% below the trigger
# Every SDK READ gets an explicit HTTP timeout: the SDK default (None) waits
# forever, and one hung read would stall the single-flight RiskManager poll.
READ_TIMEOUT_S = 8
ORDER_LIST_PAGE_SIZE = 25           # Groww's default order-list page size
ORDER_LIST_MAX_PAGES = 40           # safety cap when paging the day's order list


class OrderStateUnknownError(RuntimeError):
    """An order write failed ambiguously and its fate could not be confirmed.

    The order MAY be live at the broker.  Callers must not resubmit blindly;
    they should treat the position as needing manual / reconciliation review.
    """

    def __init__(self, message: str, order_reference_id: str) -> None:
        super().__init__(message)
        self.order_reference_id = order_reference_id


def new_order_reference_id() -> str:
    """Return a unique Groww order_reference_id (8-20 alphanumeric chars)."""
    return f"AT{uuid.uuid4().hex[:16].upper()}"  # 18 chars, [A-Z0-9]


def _is_definitive_rejection(exc: Exception) -> bool:
    """True when the broker definitely did NOT accept the order.

    A Groww API exception carrying a 4xx code or a business-level FAILURE
    code is a definitive answer from the server.  Timeouts (504), 5xx,
    connection resets and any unknown exception are ambiguous: the request
    may have reached the exchange.
    """
    code = getattr(exc, "code", None)
    if code is None:
        return False
    code = str(code)
    if not code:
        return False
    if code.isdigit():
        return 400 <= int(code) < 500
    # Non-numeric business error codes (e.g. "GA001") come from a parsed
    # FAILURE response, i.e. the server rejected the request.
    return True


def _positive_price(value: Any) -> Optional[float]:
    """Return *value* as a float if it is a finite price > 0, else None."""
    try:
        p = float(value)
    except (TypeError, ValueError):
        return None
    if p != p or p <= 0 or p == float("inf"):
        return None
    return p


# Normalised order states (audit R3 / R4).  Groww reports EXECUTED / COMPLETED
# for a fully filled order and FAILED for a rejected one.
TERMINAL_ORDER_STATUSES = frozenset({"COMPLETE", "REJECTED", "CANCELLED"})
_COMPLETE_STATUSES = frozenset({"COMPLETE", "COMPLETED", "EXECUTED", "FILLED", "DELIVERY_AWAITED"})
_REJECTED_STATUSES = frozenset({"REJECTED", "FAILED"})
_CANCELLED_STATUSES = frozenset({"CANCELLED", "CANCELED"})


def normalise_order_status(raw_status: Any) -> str:
    """Map a broker order status onto COMPLETE / REJECTED / CANCELLED or the
    raw (upper-cased) pending status; empty -> ``UNKNOWN``."""
    status = str(raw_status or "").strip().upper()
    if status in _COMPLETE_STATUSES:
        return "COMPLETE"
    if status in _REJECTED_STATUSES:
        return "REJECTED"
    if status in _CANCELLED_STATUSES:
        return "CANCELLED"
    return status or "UNKNOWN"


def normalise_order_state(order_id: str, raw: Any) -> Dict[str, Any]:
    """Normalise a Groww order-detail payload (see ``GrowwClient.get_order_state``)."""
    order = raw if isinstance(raw, dict) else {}
    if isinstance(order.get("data"), dict):
        order = order["data"]

    def _int(*keys: str) -> int:
        for k in keys:
            v = order.get(k)
            if v not in (None, ""):
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    continue
        return 0

    avg = None
    for k in ("average_fill_price", "average_price", "avg_price", "averagePrice"):
        avg = _positive_price(order.get(k))
        if avg is not None:
            break

    status = normalise_order_status(order.get("order_status") or order.get("status"))
    quantity = _int("quantity", "qty")
    filled = _int("filled_quantity", "filledQty", "filled_qty")
    if status == "COMPLETE" and filled <= 0 and quantity > 0:
        # A COMPLETE order is fully filled by definition; a missing / renamed
        # filled-quantity field must not read as "nothing sold".
        logger.warning(
            "Order %s is COMPLETE but reports no filled quantity — using quantity=%d",
            order_id, quantity,
        )
        filled = quantity
    return {
        "order_id": order_id,
        "status": status,
        "filled_quantity": filled,
        "quantity": quantity,
        "average_price": avg,
    }


def _position_net_qty(pos: Dict[str, Any]) -> int:
    """Net (signed) quantity of one Groww position row.

    Groww's ``/positions/user`` rows carry ``quantity`` (net) plus
    ``credit_quantity`` / ``debit_quantity``; older / alternative shapes use
    ``buy_quantity`` / ``sell_quantity``.  Raises ``ValueError`` when no
    recognised quantity field exists, so callers treat the broker quantity as
    UNKNOWN instead of silently reading 0 (flat).
    """
    def _num(*keys: str) -> Optional[int]:
        for k in keys:
            v = pos.get(k)
            if v not in (None, ""):
                return int(float(v))
        return None

    net = _num("quantity", "net_quantity", "netQuantity")
    if net is not None:
        return net
    credit = _num("credit_quantity", "creditQuantity")
    debit = _num("debit_quantity", "debitQuantity")
    if credit is not None or debit is not None:
        return (credit or 0) - (debit or 0)
    buy = _num("buy_quantity", "buyQuantity")
    sell = _num("sell_quantity", "sellQuantity")
    if buy is not None or sell is not None:
        return (buy or 0) - (sell or 0)
    raise ValueError(f"position row has no quantity field: {sorted(pos)}")


def _to_groww_product(product: str) -> str:
    """Map internal product codes to the Groww SDK product constants."""
    return "CNC" if str(product).upper() in ("CNC", "DELIVERY") else "MIS"


def _is_permanent_groww_error(exc: Exception) -> bool:
    """Return True for errors that retrying cannot fix (HTTP 4xx auth/permission).

    A 403 "Access forbidden" means the account's Trading API subscription does
    not include the requested API group (e.g. Live Data / Historical Data) — it
    is not a transient network blip.  Retrying such calls 3× with exponential
    backoff only wastes ~14s per call and floods the logs.  We surface the
    failure immediately instead.
    """
    # A wrong SDK method name / signature can never succeed on retry; failing
    # fast avoids ~6 s of backoff on every call (e.g. before an exit SELL).
    if isinstance(exc, (AttributeError, TypeError)):
        return True
    code = str(getattr(exc, "code", "") or "")
    if code in ("401", "403"):
        return True
    msg = str(exc).lower()
    return "access forbidden" in msg or "unauthori" in msg


def _retry_sync(func: Callable) -> Callable:
    """Decorator: retry a synchronous Groww SDK call with exponential backoff.

    READS ONLY.  Never wrap an order write (place / cancel / modify) with this:
    a retried write after a timeout can duplicate an order (audit R5 / C3).
    Runs inside ``asyncio.to_thread`` workers, so the sleep never blocks the
    event loop.
    """
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
                if attempt >= MAX_RETRIES:
                    break  # no pointless sleep after the final attempt
                wait = 2 ** attempt
                logger.warning(
                    "Groww API call %s failed (attempt %d/%d): %s — retrying in %ds",
                    func.__name__, attempt, MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)
        logger.error("Groww API call %s exhausted retries", func.__name__)
        raise last_exc  # type: ignore[misc]
    return wrapper


class _GrowwFeedTicker:
    """Callback-style ticker adapter around the official GrowwFeed API.

    The scanner drives a ticker through:
      • ticker.on_ticks / on_connect / on_close / on_error  (callback attributes)
      • ticker.connect(reconnect=True)  (blocking call)
      • ws.subscribe(tokens)  +  ws.set_mode(mode, tokens)  (in on_connect)
      • ticker.MODE_FULL  (mode constant)

    GrowwFeed's SDK API is different:
      • feed.subscribe_ltp(instruments, on_data_received=cb)
      • feed.subscribe_index_value(instruments, on_data_received=cb)
      • feed.consume()   ← blocking call

    This adapter bridges the two.

    Tick format emitted to on_ticks:
      [{"instrument_token": <int>, "exchange_token": <int>,
        "ltp": <float>, "last_price": <float>}]
    """

    # Mode constant required by scanner._on_connect → ws.set_mode(ws.MODE_FULL, tokens)
    MODE_FULL = "ltp"

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
             data format to the tick dicts the scanner consumes.
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
    """Wrapper around the Groww API SDK with retry and circuit-breaker logic."""

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

    async def _submit_order_write(
        self,
        groww: Any,
        params: Dict[str, Any],
        order_reference_id: str,
    ) -> Dict[str, Any]:
        """Send ONE order-placement request; never resubmit (audit R5 / C3).

        On a definitive broker rejection the exception is re-raised.  On an
        ambiguous failure (timeout, connection error, 5xx, unknown) the order
        is looked up by ``order_reference_id``: if the broker has it, that
        order is returned as the result; if the broker positively reports it
        unknown the original exception is re-raised; if the lookup itself
        cannot answer, ``OrderStateUnknownError`` is raised.
        """
        def _send():
            return groww.place_order(timeout=ORDER_WRITE_TIMEOUT_S, **params)

        try:
            return await asyncio.to_thread(_send)
        except Exception as exc:
            if _is_definitive_rejection(exc):
                raise
            logger.warning(
                "Order write for %s ambiguous (%s: %s) — looking up ref=%s before deciding",
                params.get("trading_symbol"), type(exc).__name__, exc, order_reference_id,
            )
            found = await self._lookup_order_by_reference(groww, order_reference_id)
            if found is None:
                raise
            if found is False:
                logger.critical(
                    "Order ref=%s for %s: state UNKNOWN after %s — manual review required",
                    order_reference_id, params.get("trading_symbol"), exc,
                )
                raise OrderStateUnknownError(
                    f"Order {order_reference_id} for {params.get('trading_symbol')} "
                    f"failed ambiguously ({exc}) and could not be confirmed — "
                    f"manual review required",
                    order_reference_id,
                ) from exc
            logger.warning(
                "Order ref=%s was accepted by the broker despite the error — using it",
                order_reference_id,
            )
            return found

    async def _lookup_order_by_reference(self, groww: Any, order_reference_id: str):
        """Look an order up by its reference id (a READ, safe to repeat).

        Returns the broker order dict when found, ``None`` when the broker
        consistently reports it unknown, and ``False`` when undetermined.
        """
        not_found = 0
        for attempt in range(ORDER_LOOKUP_ATTEMPTS):
            if attempt:
                await asyncio.sleep(ORDER_LOOKUP_DELAY_S)
            try:
                res = await asyncio.to_thread(
                    lambda: groww.get_order_status_by_reference(
                        segment="CASH",
                        order_reference_id=order_reference_id,
                        timeout=ORDER_WRITE_TIMEOUT_S,
                    )
                )
                if isinstance(res, dict) and (res.get("groww_order_id") or res.get("order_id")):
                    return res
                not_found += 1
            except Exception as exc:
                if str(getattr(exc, "code", "")) == "404":
                    not_found += 1
                else:
                    logger.warning(
                        "Order lookup ref=%s failed (attempt %d/%d): %s",
                        order_reference_id, attempt + 1, ORDER_LOOKUP_ATTEMPTS, exc,
                    )
        if not_found == ORDER_LOOKUP_ATTEMPTS:
            return None
        return False

    @staticmethod
    def _extract_order_id(result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        return str(
            result.get("groww_order_id") or result.get("order_id") or result.get("orderId") or ""
        )

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
        order_reference_id: Optional[str] = None,
    ) -> str:
        """Place an order and return the broker order ID.

        The request is sent exactly once with a unique ``order_reference_id``
        (generated when not supplied); an ambiguous failure is resolved by a
        reference lookup, never by resubmitting.  Any price / trigger price is
        snapped to the instrument tick (floored for a SELL, ceiled for a BUY).
        ``tag`` is used for logging only (the SDK has no free-text tag field).
        """
        if self._settings.paper_trading:
            paper_id = f"paper_{uuid.uuid4().hex[:10]}"
            logger.info(
                "[PAPER] Order simulated: %s %s %s qty=%d product=%s → %s",
                transaction_type, exchange, tradingsymbol, quantity, product, paper_id,
            )
            return paper_id

        groww = await self.get_groww()
        ref_id = order_reference_id or new_order_reference_id()
        tick = get_tick_size(tradingsymbol)
        snap = floor_to_tick if str(transaction_type).upper() == "SELL" else ceil_to_tick

        params: Dict[str, Any] = {
            "validity": "DAY",
            "exchange": exchange,
            "segment": "CASH",
            "trading_symbol": tradingsymbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "product": _to_groww_product(product),
            "order_type": order_type,
            "order_reference_id": ref_id,
        }
        if price is not None:
            params["price"] = snap(price, tick)
        if trigger_price is not None:
            params["trigger_price"] = snap(trigger_price, tick)

        try:
            result = await self._submit_order_write(groww, params, ref_id)
            order_id = self._extract_order_id(result)
            logger.info(
                "Order placed [%s]: %s %s %s qty=%d product=%s ref=%s → order_id=%s",
                tag, transaction_type, exchange, tradingsymbol, quantity, product, ref_id, order_id,
            )
            self._last_failure = 0.0
            return order_id
        except Exception as exc:
            await self._handle_failure(exc)
            raise

    async def place_stop_loss_order(
        self,
        tradingsymbol: str,
        quantity: int,
        trigger_price: float,
        exchange: str = "NSE",
        product: str = "MIS",
        transaction_type: str = "SELL",
    ) -> str:
        """Place the broker-side protective SL-limit SELL and return the FULL
        raw broker order id as a string (audit R1 / C1).

        The raw id is what must be persisted (``Trade.sl_order_id``) and used
        for cancel / modify / status calls -- never a truncated integer.

          * trigger = ``trigger_price`` floored to the instrument tick
          * limit   = trigger minus ``SL_LIMIT_BUFFER_PCT`` (and at least one
            tick lower), floored to the tick, so a fast move through the
            trigger still fills instead of resting unfilled (audit R12).
            SL-M is not used: Groww does not document SL_M for CASH/MIS.

        Sent once with a unique ``order_reference_id`` (no blind retry, R5).
        """
        if self._settings.paper_trading:
            paper_id = f"PAPER-SL-{uuid.uuid4().hex[:10]}"
            logger.info(
                "[PAPER] Broker stop simulated: %s qty=%d trigger=%s → %s",
                tradingsymbol, quantity, trigger_price, paper_id,
            )
            return paper_id

        groww = await self.get_groww()

        tick = get_tick_size(tradingsymbol)
        trigger = floor_to_tick(trigger_price, tick)
        limit = sl_limit_price(trigger, tick, SL_LIMIT_BUFFER_PCT)
        ref_id = new_order_reference_id()

        params: Dict[str, Any] = {
            "validity": "DAY",
            "exchange": exchange,
            "segment": "CASH",
            "trading_symbol": tradingsymbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "product": _to_groww_product(product),
            "order_type": "SL",
            "trigger_price": trigger,
            "price": limit,
            "order_reference_id": ref_id,
        }

        try:
            result = await self._submit_order_write(groww, params, ref_id)
            raw_id = self._extract_order_id(result)
            logger.info(
                "Broker SL order placed for %s qty=%d trigger=%.2f limit=%.2f ref=%s → order_id=%s",
                tradingsymbol, quantity, trigger, limit, ref_id, raw_id,
            )
            self._last_failure = 0.0
            return raw_id
        except Exception as exc:
            await self._handle_failure(exc)
            raise

    async def modify_stop_loss_order(
        self,
        order_id: str,
        tradingsymbol: str,
        quantity: int,
        trigger_price: float,
    ) -> None:
        """Move a resting broker SL-limit order to a new trigger (trailing /
        breakeven, audit R1).  Sent once; a failure is raised to the caller,
        which may leave the (lower, still protective) old stop in place."""
        if self._settings.paper_trading:
            logger.info(
                "[PAPER] Broker stop modify simulated: %s → trigger=%s", order_id, trigger_price,
            )
            return

        groww = await self.get_groww()
        tick = get_tick_size(tradingsymbol)
        trigger = floor_to_tick(trigger_price, tick)
        limit = sl_limit_price(trigger, tick, SL_LIMIT_BUFFER_PCT)

        def _modify():
            return groww.modify_order(
                order_type="SL",
                segment="CASH",
                groww_order_id=order_id,
                quantity=quantity,
                price=limit,
                trigger_price=trigger,
                timeout=ORDER_WRITE_TIMEOUT_S,
            )

        try:
            await asyncio.to_thread(_modify)
            logger.info(
                "Broker SL order modified: %s %s qty=%d trigger=%.2f limit=%.2f",
                order_id, tradingsymbol, quantity, trigger, limit,
            )
            self._last_failure = 0.0
        except Exception as exc:
            await self._handle_failure(exc)
            raise

    async def cancel_order(self, order_id: str) -> None:
        """Send ONE cancel request for a broker order by its raw order id.

        Never retried blindly -- callers confirm the outcome with
        ``get_order_state`` (an already-executed order cannot be cancelled).
        """
        if self._settings.paper_trading:
            logger.info("[PAPER] Order cancel simulated: %s", order_id)
            return

        groww = await self.get_groww()

        def _cancel():
            return groww.cancel_order(
                groww_order_id=order_id, segment="CASH", timeout=ORDER_WRITE_TIMEOUT_S,
            )

        try:
            await asyncio.to_thread(_cancel)
            logger.info("Cancel requested for broker order %s", order_id)
            self._last_failure = 0.0
        except Exception as exc:
            await self._handle_failure(exc)
            raise

    async def get_order_state(self, order_id: str) -> Dict[str, Any]:
        """Return the normalised state of one broker order (a READ).

        Shape: ``{"order_id", "status", "filled_quantity", "quantity",
        "average_price"}`` where status is one of COMPLETE / REJECTED /
        CANCELLED (terminal) or the broker's raw pending status
        (OPEN, NEW, ACKED, TRIGGER_PENDING, ...).  ``average_price`` is None
        when the broker reports no fill price.
        """
        groww = await self.get_groww()

        @_retry_sync
        def _fetch():
            return groww.get_order_detail(
                segment="CASH", groww_order_id=order_id, timeout=READ_TIMEOUT_S,
            )

        raw = await asyncio.to_thread(_fetch)
        self._last_failure = 0.0
        return normalise_order_state(order_id, raw)

    async def find_order_by_reference(self, order_reference_id: str) -> Optional[str]:
        """Resolve an ``order_reference_id`` to the broker order id (a READ).

        Returns the raw broker order id, or None when the broker consistently
        reports no such order.  Raises ``OrderStateUnknownError`` when the
        lookup cannot decide (the order may exist).
        """
        groww = await self.get_groww()
        found = await self._lookup_order_by_reference(groww, order_reference_id)
        if found is None:
            return None
        order_id = self._extract_order_id(found) if found else ""
        if not order_id:
            raise OrderStateUnknownError(
                f"Order ref {order_reference_id} could not be resolved", order_reference_id,
            )
        return order_id

    # ── Position & Order Queries ──────────────────────────────────────────────

    async def get_positions(self) -> Dict[str, Any]:
        """Fetch current positions.

        Uses ``get_positions_for_user(segment="CASH")`` (growwapi 1.5.0 has no
        ``get_positions``), normalised to ``{"net": [{"tradingsymbol",
        "exchange", "quantity", "average_price", "product"}]}``: ``quantity`` is
        the signed net quantity (``_position_net_qty``) and ``product`` is
        upper-cased.  A row with no recognised quantity field raises, so the
        broker quantity reads as UNKNOWN, never as flat.

        NOTE: the response field names follow Groww's published docs; they
        have not been verified against a live call from this codebase.
        """
        groww = await self.get_groww()

        @_retry_sync
        def _fetch():
            return groww.get_positions_for_user(segment="CASH", timeout=READ_TIMEOUT_S)

        raw = await asyncio.to_thread(_fetch)
        self._last_failure = 0.0
        positions = raw if isinstance(raw, list) else (raw or {}).get(
            "positions", (raw or {}).get("data", [])
        )

        normalised = []
        for pos in positions or []:
            net_qty = _position_net_qty(pos)
            avg_price = float(
                pos.get("average_price") or pos.get("averagePrice") or
                pos.get("net_price") or pos.get("buy_avg") or 0
            )
            normalised.append({
                "tradingsymbol": pos.get("trading_symbol") or pos.get("tradingSymbol") or "",
                "exchange": pos.get("exchange", "NSE"),
                "quantity": net_qty,
                "average_price": avg_price,
                "product": str(pos.get("product") or "MIS").upper(),
            })

        return {"net": normalised, "day": normalised}

    async def get_orders(self) -> List[Dict[str, Any]]:
        """Fetch all of today's CASH orders (``get_order_list``, every page)."""
        groww = await self.get_groww()

        @_retry_sync
        def _fetch(page: int):
            return groww.get_order_list(page=page, segment="CASH", timeout=READ_TIMEOUT_S)

        orders: List[Dict[str, Any]] = []
        seen: set = set()
        for page in range(ORDER_LIST_MAX_PAGES):
            raw = await asyncio.to_thread(_fetch, page)
            if isinstance(raw, list):
                batch = raw
            else:
                raw = raw or {}
                batch = raw.get("order_list", raw.get("data", raw.get("orders", [])))
            batch = [o for o in (batch or []) if isinstance(o, dict)]
            new = []
            for o in batch:
                oid = self._extract_order_id(o)
                if oid and oid in seen:
                    continue
                seen.add(oid)
                new.append(o)
            if not new:
                break  # empty page, or the API ignored paging and repeated itself
            orders.extend(new)
            if len(batch) < ORDER_LIST_PAGE_SIZE:
                break
        self._last_failure = 0.0
        return orders

    async def get_ltp(self, instruments: List[str]) -> Dict[str, Any]:
        """Fetch last traded price for a list of instruments (e.g. ['NSE:RELIANCE']).

        Groww's bulk LTP API: get_ltp(segment=, exchange_trading_symbols=)
        where exchange_trading_symbols is a tuple of strings like 'NSE_RELIANCE'.
        Response: {"NSE_RELIANCE": 2500.5, "NSE_NIFTY": 22962.10}  (flat dict).

        Returns a dict keyed by 'NSE:SYMBOL' → {'last_price': float} (the
        ``BrokerPort`` quote shape).

        Contract (audit R2 / C2): this NEVER returns 0.0 (or any non-positive /
        non-numeric price) as a sentinel.  Instruments that are missing from
        the response or have an invalid price are OMITTED from the result.  A
        transport / API failure raises — callers must handle the exception.
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
                timeout=READ_TIMEOUT_S,
            )

        try:
            raw = await asyncio.to_thread(_fetch)
        except Exception as exc:
            logger.warning("get_ltp failed for %s: %s", instruments, exc)
            raise
        # raw = {"NSE_RELIANCE": 2500.5, ...}  — values are plain floats
        result: Dict[str, Any] = {}
        for et_sym, price in (raw or {}).items():
            orig_key = sym_map.get(et_sym, et_sym.replace("_", ":", 1))
            valid = _positive_price(price)
            if valid is not None:
                result[orig_key] = {"last_price": valid}
        missing = [i for i in instruments if i not in result]
        if missing:
            logger.warning("get_ltp: no valid price for %s — omitted", missing)
        return result

    async def get_quote(self, instruments: List[str]) -> Dict[str, Any]:
        """Fetch full market quote including circuit limits.

        Groww's quote API returns upper/lower circuit limits under different
        field names — we normalise them for the circuit check (A3).
        """
        if self._settings.paper_trading:
            # No broker quote in paper mode: every field is unknown (None),
            # never a 0.0 sentinel (audit R2).
            return {instr: {"last_price": None, "upper_circuit_limit": None,
                            "lower_circuit_limit": None} for instr in instruments}

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
                return groww.get_quote(
                    exchange=exch, segment="CASH", trading_symbol=sym, timeout=READ_TIMEOUT_S,
                )

            try:
                raw = await asyncio.to_thread(_fetch)
                # Missing / invalid values are None, never 0.0 (audit R2).
                result[instrument] = {
                    "last_price": _positive_price(raw.get("last_price") or raw.get("ltp")),
                    "upper_circuit_limit": _positive_price(
                        raw.get("upper_circuit_limit") or raw.get("upper_circuit") or
                        raw.get("upperCircuit")
                    ),
                    "lower_circuit_limit": _positive_price(
                        raw.get("lower_circuit_limit") or raw.get("lower_circuit") or
                        raw.get("lowerCircuit")
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
            return groww.get_quote(
                trading_symbol=symbol, exchange="NSE", segment="CASH", timeout=READ_TIMEOUT_S,
            )

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

        ``instrument_token`` is the Groww exchange_token integer.

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

        # Map interval names to Groww CANDLE_INTERVAL constants
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
                timeout=READ_TIMEOUT_S,
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
        """Create and return the callback-style GrowwFeed adapter
        (``_GrowwFeedTicker``) that scanner.py drives.
        """
        groww = await self.get_groww()
        # IMPORTANT: do not construct GrowwFeed on the FastAPI event loop thread.
        # The adapter creates it lazily inside connect() (executor thread).
        # Never log any part of the session token (cluster D secret hygiene).
        logger.debug("GrowwFeed adapter created (token cached=%s)", bool(self._cached_token))
        return _GrowwFeedTicker(groww=groww)

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

        # A definitive 4xx / business rejection (e.g. cancelling an order that
        # already executed, a trailing modify rejected for its trigger) proves
        # the API is reachable: it must not arm the "unreachable" breaker.
        if _is_definitive_rejection(exc):
            return

        # --- Network / API failure: apply circuit-breaker logic ---
        now = time.time()
        if self._last_failure == 0.0:
            self._last_failure = now
        elif now - self._last_failure > CIRCUIT_BREAKER_SECONDS:
            logger.critical(
                "Groww API unreachable for >%ds — halting trading", CIRCUIT_BREAKER_SECONDS
            )
            await set_halt(f"Groww API unreachable for >{CIRCUIT_BREAKER_SECONDS}s")
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
