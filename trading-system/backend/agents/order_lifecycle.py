"""
Broker order-lifecycle helpers shared by the TradingAgent (entries), the
RiskManager (automatic exits / partial booking / trailing) and the manual
close API (audit Part A R1 / R3 / R4, Part B C1 / C5).

These functions talk to the broker only — they never touch the DB.  Callers
persist the outcome.  They are used in LIVE mode only; paper trading keeps
its simulated-fill behaviour.

Invariants enforced here:
  * An order is only treated as filled from the broker's own terminal state
    (COMPLETE / REJECTED / CANCELLED) and its ``filled_quantity`` /
    ``average_price`` — never from an acknowledgement.
  * Before any exit SELL the resting broker SL order is cancelled AND the
    cancel is confirmed.  If the stop already executed, no further SELL is
    sent; the stop's fill is booked instead.  If the cancel cannot be
    confirmed, nothing is sold.
  * An exit never sells more than min(DB quantity, broker-held quantity), and
    sells NOTHING while the broker-held quantity cannot be read.
  * Whenever shares remain after an exit attempt, a broker stop sized to the
    remainder is (re-)placed.
  * A stop whose placement was ambiguous is persisted as ``ref:<reference>``
    and resolved through the broker before any SELL (never treated as "no
    stop").
  * Every in-flight exit registers its trade id (``exit_in_flight``) so CLOSING
    reconciliation never acts on a row another closer is still working on.
    Entries register too (``trade_in_flight``), so the reconciler never
    resolves a PENDING row whose order is still being worked.
  * No market exit is sent into a lower-circuit lock (no buyers): the stop
    stays resting and the exit is retried (``CIRCUIT_LOCKED``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional, Set

from integrations.groww_client import OrderStateUnknownError, TERMINAL_ORDER_STATUSES

logger = logging.getLogger(__name__)

ORDER_POLL_INTERVAL_S = 0.5     # delay between order-status reads
ENTRY_FILL_TIMEOUT_S = 10.0     # wait for an entry BUY to reach a terminal state
EXIT_FILL_TIMEOUT_S = 10.0      # wait for an exit SELL to reach a terminal state
CANCEL_CONFIRM_TIMEOUT_S = 5.0  # wait for a cancel to be reflected in the order state

# Prefix of an ``sl_order_id`` whose placement was ambiguous: the value after it
# is the order_reference_id, resolved through the broker before any exit.
UNRESOLVED_STOP_PREFIX = "ref:"


def is_unresolved_stop(sl_order_id: Any) -> bool:
    """True when *sl_order_id* is an ambiguous stop placement (``ref:<id>``)."""
    return isinstance(sl_order_id, str) and sl_order_id.startswith(UNRESOLVED_STOP_PREFIX)


# ── In-flight exits ──────────────────────────────────────────────────────────
#
# Trade ids whose exit (close / scale-out / manual close) is between its
# OPEN→CLOSING claim and its final DB write.  The RiskManager, the manual close
# API and state recovery all run in the single backend process (one uvicorn
# worker), so an in-process registry is authoritative; after a crash it is
# empty, which is correct (no closer is running any more).
_EXITS_IN_FLIGHT: Set[int] = set()


@contextmanager
def exit_in_flight(trade_id: Any) -> Iterator[None]:
    """Mark *trade_id* as owned by a running exit for the duration of the block."""
    tid = int(trade_id) if trade_id is not None else None
    if tid is not None:
        _EXITS_IN_FLIGHT.add(tid)
    try:
        yield
    finally:
        if tid is not None:
            _EXITS_IN_FLIGHT.discard(tid)


def is_exit_in_flight(trade_id: Any) -> bool:
    """True while a closer is working on *trade_id* (see ``exit_in_flight``)."""
    try:
        return int(trade_id) in _EXITS_IN_FLIGHT
    except (TypeError, ValueError):
        return False


# The registry is not exit-specific: an entry working a PENDING row uses it too.
trade_in_flight = exit_in_flight
is_trade_in_flight = is_exit_in_flight


# ── Order state ──────────────────────────────────────────────────────────────


@dataclass
class OrderFill:
    """Normalised outcome of one broker order."""

    order_id: str
    status: str                       # COMPLETE / REJECTED / CANCELLED / <pending> / UNKNOWN
    filled_qty: int = 0
    average_price: Optional[float] = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_STATUSES


def _to_fill(order_id: str, state: Dict[str, Any]) -> OrderFill:
    return OrderFill(
        order_id=order_id,
        status=str(state.get("status") or "UNKNOWN"),
        filled_qty=int(state.get("filled_quantity") or 0),
        average_price=state.get("average_price"),
    )


async def read_order(groww: Any, order_id: str) -> Optional[OrderFill]:
    """One status read; None when the broker cannot be queried."""
    try:
        return _to_fill(order_id, await groww.get_order_state(order_id))
    except Exception as exc:
        logger.warning("Order status read failed for %s: %s", order_id, exc)
        return None


async def wait_for_terminal(
    groww: Any,
    order_id: str,
    timeout_s: float,
    poll_s: float = ORDER_POLL_INTERVAL_S,
) -> Optional[OrderFill]:
    """Poll an order until it reaches a terminal state or *timeout_s* elapses.

    Returns the last state read (terminal or not), or None if no read
    succeeded at all.
    """
    deadline = time.monotonic() + max(0.0, timeout_s)
    last: Optional[OrderFill] = None
    while True:
        fill = await read_order(groww, order_id)
        if fill is not None:
            last = fill
            if fill.terminal:
                return fill
        if time.monotonic() >= deadline:
            return last
        await asyncio.sleep(poll_s)


async def cancel_and_confirm(groww: Any, order_id: str) -> Optional[OrderFill]:
    """Cancel an order and wait until the broker shows it terminal.

    The cancel request is sent once.  A cancel error is not fatal by itself:
    the order may already be executed / cancelled, which the follow-up status
    read reveals.  Returns the final state (terminal when confirmed), or None
    when the state could not be read at all.
    """
    try:
        await groww.cancel_order(order_id)
    except Exception as exc:
        logger.warning("Cancel request for %s failed: %s — checking order state", order_id, exc)
    return await wait_for_terminal(groww, order_id, CANCEL_CONFIRM_TIMEOUT_S)


async def confirm_fill(
    groww: Any,
    order_id: str,
    timeout_s: float,
) -> Optional[OrderFill]:
    """Wait for an order to fill; on timeout cancel the unfilled remainder.

    Returns the final state.  ``terminal`` is False only when the remainder
    could not be confirmed cancelled (the order may still fill).
    """
    fill = await wait_for_terminal(groww, order_id, timeout_s)
    if fill is not None and fill.terminal:
        return fill
    logger.warning(
        "Order %s not terminal after %.1fs (status=%s, filled=%s) — cancelling remainder",
        order_id, timeout_s,
        fill.status if fill else "UNREADABLE", fill.filled_qty if fill else "?",
    )
    after = await cancel_and_confirm(groww, order_id)
    return after if after is not None else fill


# ── Broker stop + positions ──────────────────────────────────────────────────


async def broker_net_qty(groww: Any, symbol: str, product: str = "MIS") -> Optional[int]:
    """Net *product* (MIS) quantity the broker holds for *symbol* (0 if absent);
    None if unknown.  A CNC holding in the same account is NOT counted: exits
    are MIS SELLs and must never be sized against delivery shares."""
    try:
        positions = await groww.get_positions()
    except Exception as exc:
        logger.warning("Positions fetch failed for %s: %s — broker qty unknown", symbol, exc)
        return None
    total = 0
    for pos in (positions or {}).get("net", []) or []:
        if str(pos.get("tradingsymbol") or "") != symbol:
            continue
        if str(pos.get("product") or product).upper() != product:
            continue
        try:
            total += int(pos.get("quantity") or 0)
        except (TypeError, ValueError):
            logger.warning("Unparseable position quantity for %s — broker qty unknown", symbol)
            return None
    return total


def _first(order: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        v = order.get(k)
        if v not in (None, ""):
            return v
    return None


async def _symbol_sell_orders(groww: Any, symbol: str) -> Optional[list]:
    """Today's SELL orders for *symbol* from the broker order book (oldest first
    when a timestamp is available).  None when the order book cannot be read."""
    try:
        orders = await groww.get_orders()
    except Exception as exc:
        logger.warning("Order book fetch failed for %s: %s", symbol, exc)
        return None
    out = []
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        sym = str(_first(o, "trading_symbol", "tradingsymbol", "tradingSymbol", "symbol") or "")
        side = str(_first(o, "transaction_type", "transactionType", "side") or "").upper()
        if sym == symbol and side == "SELL":
            out.append(o)
    ts_keys = ("created_at", "order_timestamp", "exchange_time", "update_time", "createdAt")
    if out and all(_first(o, *ts_keys) is not None for o in out):
        out.sort(key=lambda o: str(_first(o, *ts_keys)))
    return out


def _order_id_of(order: Dict[str, Any]) -> str:
    return str(_first(order, "groww_order_id", "order_id", "orderId") or "")


async def has_pending_sell(
    groww: Any, symbol: str, exclude_order_id: Optional[str] = None,
) -> Optional[bool]:
    """True if an exit SELL for *symbol* is still working at the broker (any
    non-terminal status, unknown statuses included); None if unreadable.

    *exclude_order_id* is the trade's own resting protective stop: a working
    stop is protection, not an in-flight exit, so it is not counted.
    """
    from integrations.groww_client import normalise_order_status

    orders = await _symbol_sell_orders(groww, symbol)
    if orders is None:
        return None
    for o in orders:
        if exclude_order_id and _order_id_of(o) == str(exclude_order_id):
            continue
        status = normalise_order_status(_first(o, "order_status", "status"))
        if status not in TERMINAL_ORDER_STATUSES:
            return True
    return False


async def working_sell_stops(groww: Any, symbol: str) -> Optional[list]:
    """Raw broker ids of SELL stop orders (SL / SL-M) still working for
    *symbol*, oldest first; None when the order book cannot be read."""
    from integrations.groww_client import normalise_order_status

    orders = await _symbol_sell_orders(groww, symbol)
    if orders is None:
        return None
    out = []
    for o in orders:
        otype = str(_first(o, "order_type", "orderType") or "").upper().replace("-", "_")
        if otype not in ("SL", "SL_M", "STOP_LOSS", "STOP_LOSS_MARKET"):
            continue
        if normalise_order_status(_first(o, "order_status", "status")) in TERMINAL_ORDER_STATUSES:
            continue
        oid = _order_id_of(o)
        if oid:
            out.append(oid)
    return out


async def working_sell_stops_by_symbol(groww: Any) -> Optional[Dict[str, list]]:
    """Every working SELL stop in today's order book: symbol → raw order ids
    (oldest first).  None when the order book cannot be read."""
    from integrations.groww_client import normalise_order_status

    try:
        orders = await groww.get_orders()
    except Exception as exc:
        logger.warning("Order book fetch failed: %s", exc)
        return None
    out: Dict[str, list] = {}
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        side = str(_first(o, "transaction_type", "transactionType", "side") or "").upper()
        otype = str(_first(o, "order_type", "orderType") or "").upper().replace("-", "_")
        if side != "SELL" or otype not in ("SL", "SL_M", "STOP_LOSS", "STOP_LOSS_MARKET"):
            continue
        if normalise_order_status(_first(o, "order_status", "status")) in TERMINAL_ORDER_STATUSES:
            continue
        sym = str(_first(o, "trading_symbol", "tradingsymbol", "tradingSymbol", "symbol") or "")
        oid = _order_id_of(o)
        if sym and oid:
            out.setdefault(sym, []).append(oid)
    return out


async def last_sell_fill_price(groww: Any, symbol: str) -> Optional[float]:
    """Average price of the most recent COMPLETE SELL for *symbol* in today's
    order book (best-effort exit-fill lookup for reconciliation); None if
    there is none or the order book cannot be read."""
    from integrations.groww_client import normalise_order_status

    orders = await _symbol_sell_orders(groww, symbol)
    for o in reversed(orders or []):
        if normalise_order_status(_first(o, "order_status", "status")) != "COMPLETE":
            continue
        try:
            price = float(_first(o, "average_fill_price", "average_price", "avg_price", "averagePrice") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return None


async def place_stop(
    groww: Any,
    symbol: str,
    exchange: str,
    quantity: int,
    stop_price: float,
) -> Optional[str]:
    """Place a protective SL SELL (always MIS) and return its raw order id.

    Returns None (after logging) when placement definitely failed; the caller
    alerts.  When the placement was AMBIGUOUS (the stop may be resting at the
    broker) it returns ``ref:<order_reference_id>`` instead of None, so the
    caller persists it and ``release_stop`` resolves / cancels it before any
    SELL.  Use ``is_unresolved_stop`` to tell the two apart.
    """
    if quantity < 1:
        return None
    try:
        order_id = await groww.place_stop_loss_order(
            tradingsymbol=symbol,
            quantity=int(quantity),
            trigger_price=stop_price,
            exchange=exchange or "NSE",
            product="MIS",
        )
        return str(order_id) if order_id else None
    except OrderStateUnknownError as exc:
        logger.critical(
            "Broker stop placement for %s qty=%d is AMBIGUOUS (ref %s) — it may be resting",
            symbol, quantity, exc.order_reference_id,
        )
        return f"{UNRESOLVED_STOP_PREFIX}{exc.order_reference_id}"
    except Exception as exc:
        logger.error("Broker stop placement failed for %s qty=%d: %s", symbol, quantity, exc)
        return None


async def adopt_or_place_stop(
    groww: Any, symbol: str, exchange: str, qty: int, stop_price: float,
) -> Optional[str]:
    """Broker stop for a position whose stop id was never persisted (state
    recovery, a stranded PENDING entry).

    A crash between the stop placement and the DB write leaves that stop
    resting at the broker: adopt the oldest working SL SELL for the symbol and
    cancel any extras (two stops for the same shares could open a short).
    Only when none exists is a new stop placed.  Returns None when the order
    book is unreadable (placing blindly could double the stop), an extra stop
    could not be confirmed cancelled, or placement failed.
    """
    existing = await working_sell_stops(groww, symbol)
    if existing is None:
        logger.critical("Order book unreadable for %s — not placing a stop blindly", symbol)
        return None
    if existing:
        keep, extras = existing[0], existing[1:]
        for extra in extras:
            fill = await cancel_and_confirm(groww, extra)
            if fill is None or not fill.terminal:
                logger.critical(
                    "Extra stop %s for %s could not be confirmed cancelled", extra, symbol,
                )
                return None
        logger.warning(
            "Adopted resting broker stop %s for %s (cancelled extras: %s)", keep, symbol, extras,
        )
        return keep
    return await place_stop(groww, symbol, exchange, qty, stop_price)


# A quote within this fraction above the lower circuit counts as locked.
LOWER_CIRCUIT_TOLERANCE_PCT = 0.001


async def at_lower_circuit(groww: Any, symbol: str) -> bool:
    """True when *symbol* trades at its lower circuit limit (no buyers, so a
    market SELL cannot fill).  An unreadable / incomplete quote is False: an
    exit must never be blocked just because the quote failed."""
    key = f"NSE:{symbol}"
    try:
        quote = (await groww.get_quote([key]) or {}).get(key) or {}
    except Exception as exc:
        logger.warning("Circuit check quote failed for %s: %s — exiting anyway", symbol, exc)
        return False
    try:
        lower = float(quote.get("lower_circuit_limit") or 0)
        ltp = float(quote.get("last_price") or 0)
    except (TypeError, ValueError):
        return False
    if lower <= 0 or ltp <= 0:
        return False
    return ltp <= lower * (1 + LOWER_CIRCUIT_TOLERANCE_PCT)


@dataclass
class StopRelease:
    """Result of cancelling the resting broker stop before an exit."""

    outcome: str                 # NONE / CANCELLED / EXECUTED / UNCONFIRMED
    filled_qty: int = 0          # shares the stop itself sold (EXECUTED or partly filled)
    average_price: Optional[float] = None


async def release_stop(groww: Any, trade: Any) -> StopRelease:
    """Cancel the trade's resting broker stop and CONFIRM it (audit R1 / C1).

    * NONE        — no broker stop recorded for this trade (or an ambiguous
      placement the broker confirms never existed).
    * CANCELLED   — confirmed cancelled (``filled_qty`` > 0 if it part-filled first).
    * EXECUTED    — the stop already filled; do NOT send another SELL.
    * UNCONFIRMED — the stop's state is unknown; do NOT sell (it may still fire).

    An ambiguous placement (``ref:<reference>``) is first resolved through the
    broker's reference lookup; if it cannot be resolved the result is
    UNCONFIRMED.
    """
    sl_id = getattr(trade, "sl_order_id", None)
    if is_unresolved_stop(sl_id):
        ref = str(sl_id)[len(UNRESOLVED_STOP_PREFIX):]
        try:
            resolved = await groww.find_order_by_reference(ref)
        except Exception as exc:
            logger.error(
                "Ambiguous broker stop ref %s for %s could not be resolved: %s",
                ref, trade.stock, exc,
            )
            return StopRelease("UNCONFIRMED")
        if not resolved:
            logger.warning("Ambiguous broker stop ref %s for %s was never placed", ref, trade.stock)
            return StopRelease("NONE")
        sl_id = resolved
    if not sl_id:
        return StopRelease("NONE")

    fill = await cancel_and_confirm(groww, str(sl_id))
    if fill is None or not fill.terminal:
        logger.error(
            "Broker stop %s for %s: cancel NOT confirmed (status=%s)",
            sl_id, trade.stock, fill.status if fill else "UNREADABLE",
        )
        return StopRelease("UNCONFIRMED")
    if fill.status == "COMPLETE":
        return StopRelease("EXECUTED", fill.filled_qty or int(trade.quantity), fill.average_price)
    # CANCELLED / REJECTED (a rejected stop never sold anything unless part-filled)
    return StopRelease("CANCELLED", fill.filled_qty, fill.average_price)


# ── Exit ─────────────────────────────────────────────────────────────────────


@dataclass
class ExitResult:
    """Broker-side outcome of an exit attempt (see ``execute_exit``)."""

    outcome: str                         # DONE / STOP_UNCONFIRMED / POSITION_UNKNOWN / SELL_UNKNOWN / CIRCUIT_LOCKED
    sold_qty: int = 0                    # shares sold (stop fill + exit SELL fill)
    exit_price: Optional[float] = None   # quantity-weighted average of the fills
    remaining_qty: int = 0               # shares still held after the exit
    stop_fired: bool = False             # the resting broker stop sold shares
    sl_order_id: Optional[str] = None    # broker stop protecting remaining_qty
    stop_restore_failed: bool = False    # shares remain but no stop could be placed
    broker_qty: Optional[int] = None     # broker-held qty seen before the SELL
    message: str = ""


async def execute_exit(
    groww: Any,
    trade: Any,
    sell_qty: int,
    restore_stop_price: float,
    tag: str = "exit",
    unfilled_stop_price: Optional[float] = None,
) -> ExitResult:
    """Run one exit (full close or partial scale-out) against the broker.

    0. Read the broker-held (MIS) quantity.  Unreadable → POSITION_UNKNOWN:
       nothing is cancelled or sold (the resting stop stays in place).
       At the lower circuit → CIRCUIT_LOCKED: likewise nothing is touched.
    1. Cancel the resting stop and confirm (``release_stop``).  Unconfirmed →
       nothing is sold.  Executed → its fill is booked and no SELL is sent for
       the shares it sold.
    2. SELL ``min(sell_qty, DB qty − stop fill, broker-held qty)`` at market.
    3. Wait for the SELL to reach a terminal state (cancel any remainder on
       timeout) and book ``filled_quantity`` @ ``average_price``.  An
       accepted SELL with no order id, or a COMPLETE SELL reporting no filled
       quantity, is SELL_UNKNOWN (never re-protected, never re-sold).
    4. If shares remain held, place a fresh broker stop sized to them at
       ``restore_stop_price`` (or ``unfilled_stop_price`` when nothing at all
       was sold, e.g. a scale-out whose SELL was rejected keeps its old stop).
    """
    from integrations.groww_client import new_order_reference_id

    db_qty = int(trade.quantity)
    pre_qty = await broker_net_qty(groww, trade.stock)
    if pre_qty is None:
        return ExitResult(
            outcome="POSITION_UNKNOWN",
            remaining_qty=db_qty,
            sl_order_id=getattr(trade, "sl_order_id", None),
            message="broker positions unreadable — nothing cancelled or sold",
        )

    if pre_qty > 0 and await at_lower_circuit(groww, trade.stock):
        return ExitResult(
            outcome="CIRCUIT_LOCKED",
            remaining_qty=db_qty,
            sl_order_id=getattr(trade, "sl_order_id", None),
            broker_qty=pre_qty,
            message="stock is locked at its lower circuit — no SELL sent, broker stop left resting",
        )

    release = await release_stop(groww, trade)
    if release.outcome == "UNCONFIRMED":
        return ExitResult(
            outcome="STOP_UNCONFIRMED",
            remaining_qty=db_qty,
            sl_order_id=getattr(trade, "sl_order_id", None),
            message="broker stop cancel could not be confirmed — nothing sold",
        )

    stop_sold = min(int(release.filled_qty or 0), db_qty)
    proceeds = stop_sold * (release.average_price or 0.0)
    priced_qty = stop_sold if release.average_price else 0
    held = db_qty - stop_sold

    broker_qty = await broker_net_qty(groww, trade.stock)
    if broker_qty is None:
        # Re-read failed after the stop was released: fall back to the
        # pre-release reading less what the stop itself sold.
        broker_qty = max(0, pre_qty - stop_sold)
    held = max(0, min(held, broker_qty))

    want = min(max(0, int(sell_qty) - stop_sold), held)
    sell_filled = 0
    if want > 0:
        ref_id = new_order_reference_id()
        try:
            order_id = await groww.place_order(
                tradingsymbol=trade.stock,
                exchange=getattr(trade, "exchange", None) or "NSE",
                transaction_type="SELL",
                quantity=want,
                product="MIS",
                order_type="MARKET",
                tag=tag,
                order_reference_id=ref_id,
            )
        except OrderStateUnknownError as exc:
            # The SELL may be live.  Do not re-place a stop (it could create
            # a short) and do not resend: leave for manual review.
            return ExitResult(
                outcome="SELL_UNKNOWN",
                sold_qty=stop_sold,
                exit_price=(proceeds / priced_qty) if priced_qty else None,
                remaining_qty=held,
                stop_fired=stop_sold > 0,
                broker_qty=broker_qty,
                message=f"exit SELL state unknown (ref {exc.order_reference_id})",
            )
        except Exception as exc:
            logger.error("Exit SELL for %s rejected: %s", trade.stock, exc)
            order_id = None
        else:
            if not order_id:
                # Accepted by the broker (no exception) but no order id came
                # back: the SELL may fill — never re-protect or re-sell.
                return ExitResult(
                    outcome="SELL_UNKNOWN",
                    sold_qty=stop_sold,
                    exit_price=(proceeds / priced_qty) if priced_qty else None,
                    remaining_qty=held,
                    stop_fired=stop_sold > 0,
                    broker_qty=broker_qty,
                    message=f"exit SELL accepted without an order id (ref {ref_id})",
                )

        if order_id:
            fill = await confirm_fill(groww, str(order_id), EXIT_FILL_TIMEOUT_S)
            if fill is None or not fill.terminal:
                return ExitResult(
                    outcome="SELL_UNKNOWN",
                    sold_qty=stop_sold + (fill.filled_qty if fill else 0),
                    exit_price=(proceeds / priced_qty) if priced_qty else None,
                    remaining_qty=held,
                    stop_fired=stop_sold > 0,
                    broker_qty=broker_qty,
                    message=f"exit SELL {order_id} not confirmed terminal",
                )
            if fill.status == "COMPLETE" and int(fill.filled_qty or 0) <= 0:
                # COMPLETE but no filled quantity reported: the fill size is
                # unknown.  Treating it as "nothing sold" would re-place a stop
                # on a possibly-flat account and re-sell next poll.
                return ExitResult(
                    outcome="SELL_UNKNOWN",
                    sold_qty=stop_sold,
                    exit_price=(proceeds / priced_qty) if priced_qty else None,
                    remaining_qty=held,
                    stop_fired=stop_sold > 0,
                    broker_qty=broker_qty,
                    message=f"exit SELL {order_id} COMPLETE but reported no filled quantity",
                )
            sell_filled = min(int(fill.filled_qty or 0), want)
            if sell_filled > 0 and fill.average_price:
                proceeds += sell_filled * fill.average_price
                priced_qty += sell_filled
            if sell_filled < want:
                logger.error(
                    "Exit SELL %s for %s: %s, filled %d/%d",
                    order_id, trade.stock, fill.status, sell_filled, want,
                )

    sold = stop_sold + sell_filled
    remaining = max(0, held - sell_filled)
    result = ExitResult(
        outcome="DONE",
        sold_qty=sold,
        exit_price=(proceeds / priced_qty) if priced_qty else None,
        remaining_qty=remaining,
        stop_fired=stop_sold > 0,
        broker_qty=broker_qty,
    )

    if remaining > 0:
        # The old stop is gone (cancelled or executed): protect what is left.
        stop_price = restore_stop_price
        if sold == 0 and unfilled_stop_price is not None:
            stop_price = unfilled_stop_price
        new_id = await place_stop(
            groww, trade.stock, getattr(trade, "exchange", None) or "NSE",
            remaining, stop_price,
        )
        result.sl_order_id = new_id
        # An ambiguous placement is persisted (``ref:``) but is NOT a confirmed stop.
        result.stop_restore_failed = new_id is None or is_unresolved_stop(new_id)
    return result
