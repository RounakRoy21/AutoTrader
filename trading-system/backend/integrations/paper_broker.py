"""
PaperBroker — in-memory ``BrokerPort`` for paper trading (audit Part B D1,
Part A V8).

Paper mode used to skip the broker paths entirely (no stop order, no fill
polling, no cancel + confirm), so it could not reveal order-lifecycle bugs.
This broker simulates them so paper trades run through exactly the same
entry / exit / reconciliation code as live trades:

  * MARKET orders fill immediately at the current price from ``ltp_store``
    (the WebSocket / mock feed).  A BUY with no price is REJECTED; a SELL with
    no price fills at the last known price for the symbol.
  * Protective stops are SL-limit SELLs (trigger floored to the tick, limit
    buffered below it, exactly like ``GrowwClient``).  A stop whose trigger is
    not below the current price is REJECTED, as at the exchange.
  * Resting orders are matched lazily against the current price whenever the
    broker is read or written, so a stop can fire "behind the back" of the
    RiskManager — which the reconciler then has to catch, as in live.
  * Positions are signed net quantities per (symbol, product); an exit that
    sells more than is held produces a short, as a real MIS account would.
  * Day rollover clears orders and positions (MIS never carries overnight).

State is process memory only: ``adopt_position`` re-creates open positions
(and their stops) from the DB after a restart.
"""

from __future__ import annotations

import itertools
import logging
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.nse_calendar import ist_today
from core.tick_size import ceil_to_tick, floor_to_tick, get_tick_size, sl_limit_price
from integrations import ltp_store
from integrations.groww_client import SL_LIMIT_BUFFER_PCT, TERMINAL_ORDER_STATUSES

logger = logging.getLogger(__name__)

_PENDING_STOP = "TRIGGER_PENDING"
_OPEN = "OPEN"


class PaperBrokerError(RuntimeError):
    """A definitive rejection of a paper order write (never ambiguous)."""


class PaperBroker:
    def __init__(
        self,
        price_source: Optional[Callable[[str], Optional[float]]] = None,
        today: Optional[Callable[[], date]] = None,
    ) -> None:
        self._price_source = price_source or ltp_store.get_ltp
        self._today = today or ist_today
        self._seq = itertools.count(1)
        self._day: Optional[date] = None
        self._orders: Dict[str, Dict[str, Any]] = {}
        self._refs: Dict[str, str] = {}
        self._positions: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._last_px: Dict[str, float] = {}
        self._circuits: Dict[str, Tuple[Optional[float], Optional[float]]] = {}
        self._roll_day()

    # ── Housekeeping ─────────────────────────────────────────────────────────

    def reset(self) -> None:
        self._orders.clear()
        self._refs.clear()
        self._positions.clear()
        self._last_px.clear()
        self._circuits.clear()
        self._day = self._today()

    def _roll_day(self) -> None:
        today = self._today()
        if self._day != today:
            if self._day is not None:
                logger.info("[PAPER] New trading day %s — clearing paper orders and positions", today)
            self.reset()

    def set_circuit_limits(
        self, symbol: str, lower: Optional[float], upper: Optional[float],
    ) -> None:
        """Simulated circuit band for *symbol* (reported by ``get_quote``)."""
        self._circuits[symbol] = (lower, upper)

    def _price(self, symbol: str) -> Optional[float]:
        try:
            px = self._price_source(symbol)
        except Exception:
            px = None
        try:
            px = float(px) if px is not None else None
        except (TypeError, ValueError):
            px = None
        if px is None or px != px or px <= 0:
            return None
        self._last_px[symbol] = px
        return px

    def _new_id(self) -> str:
        return f"PAPER{next(self._seq):010d}"

    def _stamp(self) -> str:
        return f"{datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%f')}-{next(self._seq):08d}"

    # ── Fills / positions ────────────────────────────────────────────────────

    def _apply_fill(self, order: Dict[str, Any], qty: int, px: float) -> None:
        order["filled_quantity"] = int(order["filled_quantity"]) + qty
        order["average_fill_price"] = px
        order["order_status"] = "COMPLETE"
        self._last_px[order["trading_symbol"]] = px
        key = (order["trading_symbol"], order["product"])
        pos = self._positions.setdefault(
            key, {"quantity": 0, "average_price": 0.0, "exchange": order["exchange"]},
        )
        signed = qty if order["transaction_type"] == "BUY" else -qty
        old = int(pos["quantity"])
        new = old + signed
        if old == 0 or (old > 0) == (signed > 0):
            pos["average_price"] = (abs(old) * pos["average_price"] + qty * px) / abs(new)
        elif new != 0 and (old > 0) != (new > 0):
            pos["average_price"] = px
        pos["quantity"] = new
        logger.info(
            "[PAPER] %s %s %d @ ₹%.2f (%s) → net %d",
            order["transaction_type"], order["trading_symbol"], qty, px,
            order["groww_order_id"], new,
        )

    def _match(self) -> None:
        """Fill resting orders whose conditions the current price satisfies."""
        self._roll_day()
        for order in self._orders.values():
            status = order["order_status"]
            if status in TERMINAL_ORDER_STATUSES:
                continue
            px = self._price(order["trading_symbol"])
            if px is None:
                continue
            remaining = int(order["quantity"]) - int(order["filled_quantity"])
            if status == _PENDING_STOP:
                if px > order["trigger_price"]:
                    continue
                order["order_status"] = _OPEN  # triggered: now a resting limit
            if order["transaction_type"] == "SELL" and px >= order["price"]:
                self._apply_fill(order, remaining, px)
            elif order["transaction_type"] == "BUY" and px <= order["price"]:
                self._apply_fill(order, remaining, px)

    def _new_order(
        self,
        symbol: str,
        exchange: str,
        side: str,
        qty: int,
        product: str,
        order_type: str,
        ref: Optional[str],
        price: Optional[float] = None,
        trigger: Optional[float] = None,
    ) -> Dict[str, Any]:
        if int(qty) < 1:
            raise PaperBrokerError(f"invalid quantity {qty}")
        if ref is not None and ref in self._refs:
            raise PaperBrokerError(f"duplicate order_reference_id {ref}")
        oid = self._new_id()
        order = {
            "groww_order_id": oid,
            "order_reference_id": ref,
            "trading_symbol": symbol,
            "exchange": exchange or "NSE",
            "transaction_type": side,
            "order_type": order_type,
            "product": str(product or "MIS").upper(),
            "quantity": int(qty),
            "filled_quantity": 0,
            "average_fill_price": None,
            "price": price,
            "trigger_price": trigger,
            "order_status": _OPEN,
            "created_at": self._stamp(),
        }
        self._orders[oid] = order
        if ref is not None:
            self._refs[ref] = oid
        return order

    # ── BrokerPort: writes ───────────────────────────────────────────────────

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
        self._match()
        side = str(transaction_type).upper()
        otype = str(order_type).upper()
        if otype not in ("MARKET", "LIMIT"):
            raise PaperBrokerError(f"unsupported paper order_type {order_type}")
        if otype == "LIMIT":
            if price is None:
                raise PaperBrokerError("LIMIT order without a price")
            snap = floor_to_tick if side == "SELL" else ceil_to_tick
            price = snap(price, get_tick_size(tradingsymbol))
        order = self._new_order(
            tradingsymbol, exchange, side, quantity, product, otype, order_reference_id, price=price,
        )
        px = self._price(tradingsymbol)
        if otype == "MARKET":
            if px is None and side == "SELL":
                px = self._last_px.get(tradingsymbol) or self._position_avg(tradingsymbol, order["product"])
            if px is None:
                order["order_status"] = "REJECTED"
                logger.warning("[PAPER] %s %s rejected: no price available", side, tradingsymbol)
            else:
                self._apply_fill(order, int(quantity), px)
        else:
            self._match()
        return order["groww_order_id"]

    async def place_stop_loss_order(
        self,
        tradingsymbol: str,
        quantity: int,
        trigger_price: float,
        exchange: str = "NSE",
        product: str = "MIS",
        transaction_type: str = "SELL",
    ) -> str:
        self._match()
        tick = get_tick_size(tradingsymbol)
        trigger = floor_to_tick(trigger_price, tick)
        limit = sl_limit_price(trigger, tick, SL_LIMIT_BUFFER_PCT)
        order = self._new_order(
            tradingsymbol, exchange, str(transaction_type).upper(), quantity, product, "SL",
            None, price=limit, trigger=trigger,
        )
        px = self._price(tradingsymbol)
        if px is not None and trigger >= px:
            order["order_status"] = "REJECTED"
            logger.warning(
                "[PAPER] Stop for %s rejected: trigger ₹%.2f not below LTP ₹%.2f",
                tradingsymbol, trigger, px,
            )
        else:
            order["order_status"] = _PENDING_STOP
        return order["groww_order_id"]

    async def modify_stop_loss_order(
        self, order_id: str, tradingsymbol: str, quantity: int, trigger_price: float,
    ) -> None:
        self._match()
        order = self._order(order_id)
        if order["order_status"] != _PENDING_STOP:
            raise PaperBrokerError(f"order {order_id} is {order['order_status']} — cannot modify")
        tick = get_tick_size(tradingsymbol)
        trigger = floor_to_tick(trigger_price, tick)
        px = self._price(tradingsymbol)
        if px is not None and trigger >= px:
            raise PaperBrokerError(f"trigger ₹{trigger:.2f} not below LTP ₹{px:.2f}")
        order.update(
            trigger_price=trigger,
            price=sl_limit_price(trigger, tick, SL_LIMIT_BUFFER_PCT),
            quantity=int(quantity),
        )

    async def cancel_order(self, order_id: str) -> None:
        self._match()
        order = self._order(order_id)
        if order["order_status"] in TERMINAL_ORDER_STATUSES:
            raise PaperBrokerError(f"order {order_id} is {order['order_status']} — not cancellable")
        order["order_status"] = "CANCELLED"

    # ── BrokerPort: reads ────────────────────────────────────────────────────

    def _order(self, order_id: str) -> Dict[str, Any]:
        order = self._orders.get(str(order_id))
        if order is None:
            raise PaperBrokerError(f"unknown order {order_id}")
        return order

    def _position_avg(self, symbol: str, product: str) -> Optional[float]:
        pos = self._positions.get((symbol, product))
        avg = pos["average_price"] if pos else None
        return avg if avg and avg > 0 else None

    async def get_order_state(self, order_id: str) -> Dict[str, Any]:
        self._match()
        o = self._order(order_id)
        return {
            "order_id": o["groww_order_id"],
            "status": o["order_status"],
            "filled_quantity": int(o["filled_quantity"]),
            "quantity": int(o["quantity"]),
            "average_price": o["average_fill_price"],
        }

    async def find_order_by_reference(self, order_reference_id: str) -> Optional[str]:
        self._match()
        return self._refs.get(order_reference_id)

    async def get_positions(self) -> Dict[str, Any]:
        self._match()
        net = [
            {
                "tradingsymbol": sym,
                "exchange": pos["exchange"],
                "quantity": int(pos["quantity"]),
                "average_price": float(pos["average_price"]),
                "product": product,
            }
            for (sym, product), pos in self._positions.items()
        ]
        return {"net": net, "day": net}

    async def get_orders(self) -> List[Dict[str, Any]]:
        self._match()
        return [dict(o) for o in sorted(self._orders.values(), key=lambda o: o["created_at"])]

    async def get_ltp(self, instruments: List[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for instrument in instruments:
            symbol = instrument.split(":", 1)[-1]
            px = self._price(symbol)
            if px is not None:
                out[instrument] = {"last_price": px}
        return out

    async def get_quote(self, instruments: List[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for instrument in instruments:
            symbol = instrument.split(":", 1)[-1]
            lower, upper = self._circuits.get(symbol, (None, None))
            out[instrument] = {
                "last_price": self._price(symbol),
                "upper_circuit_limit": upper,
                "lower_circuit_limit": lower,
            }
        return out

    # ── Restart support ──────────────────────────────────────────────────────

    def adopt_position(
        self,
        symbol: str,
        quantity: int,
        average_price: float,
        exchange: str = "NSE",
        stop_order_id: Optional[str] = None,
        stop_price: Optional[float] = None,
    ) -> Optional[str]:
        """Re-create a paper MIS position (and its resting stop) after a restart.

        Returns the id of the stop protecting it: *stop_order_id* re-registered
        when given, else a new stop at *stop_price*, else None.  Idempotent for
        a position / stop the broker already knows.
        """
        self._roll_day()
        key = (symbol, "MIS")
        pos = self._positions.get(key)
        if pos is None or int(pos["quantity"]) == 0:
            self._positions[key] = {
                "quantity": int(quantity),
                "average_price": float(average_price),
                "exchange": exchange or "NSE",
            }
        if stop_price is None:
            return stop_order_id if stop_order_id in self._orders else None
        if stop_order_id and stop_order_id in self._orders:
            return stop_order_id
        tick = get_tick_size(symbol)
        trigger = floor_to_tick(stop_price, tick)
        oid = str(stop_order_id) if stop_order_id else self._new_id()
        self._orders[oid] = {
            "groww_order_id": oid,
            "order_reference_id": None,
            "trading_symbol": symbol,
            "exchange": exchange or "NSE",
            "transaction_type": "SELL",
            "order_type": "SL",
            "product": "MIS",
            "quantity": int(quantity),
            "filled_quantity": 0,
            "average_fill_price": None,
            "price": sl_limit_price(trigger, tick, SL_LIMIT_BUFFER_PCT),
            "trigger_price": trigger,
            "order_status": _PENDING_STOP,
            "created_at": self._stamp(),
        }
        return oid


_paper_broker: Optional[PaperBroker] = None


def get_paper_broker() -> PaperBroker:
    global _paper_broker
    if _paper_broker is None:
        _paper_broker = PaperBroker()
    return _paper_broker
