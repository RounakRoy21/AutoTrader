"""
PositionService — the single owner of every position (``trades`` row) state
change (audit Part B §3.1, C4 / C9).

Both the RiskManager (automatic exits, scale-outs, stop / target moves), the
manual-close API, the TradingAgent (entries) and the Reconciler go through it.
Every transition is a compare-and-swap on ``status``:

    (insert) PENDING ──fill──▶ OPEN ◀──revert── CLOSING ──finalise──▶ CLOSED
                  └──nothing filled──▶ REJECTED     ▲
                                        OPEN ──claim┘

Broker work is delegated to ``agents.order_lifecycle`` against a
``BrokerPort`` — ``PaperBroker`` in paper mode, ``GrowwBroker`` live — so
paper and live run the same exit path.

IO dependencies are injected (``PositionDeps``) by the caller, which builds
them from its own module (composition at the call site).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Optional, Set, Tuple

import pytz
from sqlalchemy import select, update

from core.tick_size import get_tick_size
from models.trade import Trade

from . import exit_policy, order_lifecycle

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

_STOP_KIND_EXIT_REASON = {
    "TRAILING": "TRAILING_SL",
    "BREAKEVEN": "BREAKEVEN",
}

# Outcomes after which nothing was cancelled or sold: the row goes back to OPEN.
_UNTOUCHED_OUTCOMES = ("STOP_UNCONFIRMED", "POSITION_UNKNOWN", "CIRCUIT_LOCKED")

# Trades already alerted for a lower-circuit lock (one alert per trade per day,
# the exit is retried every poll).
_circuit_alerted: Set[Tuple[int, date]] = set()


def stop_exit_reason(trade) -> str:
    """Exit reason for a stop-triggered exit given how the stop was last moved."""
    return _STOP_KIND_EXIT_REASON.get(getattr(trade, "stop_kind", None) or "", "STOP_LOSS_HIT")


@dataclass
class PositionDeps:
    db_context: Callable[..., Any]                      # core.database.get_db_context
    broker: Callable[[], Any]                           # () -> BrokerPort
    publish: Callable[[str, Any], Awaitable[None]]
    notify: Callable[[str], Awaitable[Any]]             # Telegram
    costs: Callable[[float, float, int, str], float]    # round-trip transaction costs
    record_outcome: Callable[[str, float, Any, datetime], Awaitable[None]]


@dataclass
class CloseOutcome:
    """Result of a close attempt; ``detail`` explains a close that did not finish."""

    closed: bool
    detail: str = ""
    claimed: bool = True   # False when another closer owned the row (nothing was done)

    def __bool__(self) -> bool:
        return self.closed


_KEEP = object()


class PositionService:
    def __init__(self, settings: Any, deps: PositionDeps) -> None:
        self._settings = settings
        self._deps = deps

    # ── Entry outbox (audit C9) ──────────────────────────────────────────────

    async def create_pending(
        self,
        *,
        stock: str,
        exchange: str,
        quantity: int,
        entry_price: float,
        stop_loss_price: float,
        target_price: float,
        order_reference_id: str,
        rationale: Optional[str],
        now_ist: datetime,
    ) -> Trade:
        """Insert the PENDING row for an entry BEFORE its order is sent.

        Raises on a DB failure — the caller must then NOT send the order.
        """
        trade = Trade(
            stock=stock,
            exchange=exchange or "NSE",
            direction="BUY",
            product_type="MIS",
            quantity=int(quantity),
            original_quantity=int(quantity),
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            target_price=target_price,
            status="PENDING",
            trade_date=now_ist.date(),
            entry_time=now_ist.time(),
            decision_rationale=rationale,
            entry_order_ref=order_reference_id,
            booked_pnl=0.0,
            partial_booked=False,
        )
        async with self._deps.db_context() as session:
            session.add(trade)
            await session.flush()
        return trade

    async def confirm_entry(
        self,
        trade: Trade,
        *,
        order_id: str,
        quantity: int,
        entry_price: float,
        stop_loss_price: float,
        target_price: float,
        partial_target_price: Optional[float],
        sl_order_id: Optional[str],
    ) -> bool:
        """PENDING → OPEN with the broker fill.  False when the row was no
        longer PENDING (another writer resolved it)."""
        values = dict(
            status="OPEN",
            entry_order_id=str(order_id),
            quantity=int(quantity),
            original_quantity=int(quantity),
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            target_price=target_price,
            partial_target_price=partial_target_price,
            sl_order_id=sl_order_id,
        )
        async with self._deps.db_context() as session:
            res = await session.execute(
                update(Trade)
                .where(Trade.id == trade.id, Trade.status == "PENDING")
                .values(**values)
                .returning(Trade.id)
            )
            if res.scalar_one_or_none() is None:
                logger.critical(
                    "Entry %s#%s was no longer PENDING at fill confirmation — not opened",
                    trade.stock, trade.id,
                )
                return False
        for k, v in values.items():
            setattr(trade, k, v)
        return True

    async def reject_entry(self, trade: Trade, reason: str) -> bool:
        """PENDING → REJECTED: the entry order filled nothing (or was never placed)."""
        async with self._deps.db_context() as session:
            res = await session.execute(
                update(Trade)
                .where(Trade.id == trade.id, Trade.status == "PENDING")
                .values(status="REJECTED", exit_reason="ENTRY_FAILED", realized_pnl=0.0)
                .returning(Trade.id)
            )
            if res.scalar_one_or_none() is None:
                return False
        trade.status = "REJECTED"
        trade.exit_reason = "ENTRY_FAILED"
        trade.realized_pnl = 0.0
        logger.warning("Entry %s#%s REJECTED: %s", trade.stock, trade.id, reason)
        return True

    # ── Claim / revert ───────────────────────────────────────────────────────

    async def claim(self, trade: Trade) -> bool:
        """OPEN → CLOSING compare-and-swap; on success re-read the claimed row.

        The caller's ``trade`` is a pre-claim snapshot: quantity, broker stop id,
        booked P&L or stop may have been changed by another closer (e.g. a
        partially filled manual close) since it was read (audit C4).
        """
        async with self._deps.db_context() as session:
            claimed = await session.execute(
                update(Trade)
                .where(Trade.id == trade.id, Trade.status == "OPEN")
                .values(status="CLOSING")
                .returning(Trade.id)
            )
            if claimed.scalar_one_or_none() is None:
                return False
            fresh_res = await session.execute(select(Trade).where(Trade.id == trade.id))
            fresh = fresh_res.scalar_one_or_none()
            if isinstance(fresh, Trade) and fresh is not trade:
                for field in (
                    "quantity", "sl_order_id", "booked_pnl", "stop_loss_price",
                    "stop_kind", "partial_booked",
                ):
                    setattr(trade, field, getattr(fresh, field))
        trade.status = "CLOSING"
        return True

    async def revert_to_open(self, trade: Trade, sl_order_id=_KEEP) -> None:
        """Return a CLOSING trade to OPEN so the poll keeps managing it."""
        values = {"status": "OPEN"}
        if sl_order_id is not _KEEP:
            values["sl_order_id"] = sl_order_id
        async with self._deps.db_context() as session:
            await session.execute(
                update(Trade)
                .where(Trade.id == trade.id, Trade.status == "CLOSING")
                .values(**values)
            )
        trade.status = "OPEN"
        if sl_order_id is not _KEEP:
            trade.sl_order_id = sl_order_id

    async def mark_closing(self, trade: Trade) -> bool:
        """Park an OPEN trade in CLOSING (exit state unknown → reconciliation)."""
        async with self._deps.db_context() as session:
            res = await session.execute(
                update(Trade)
                .where(Trade.id == trade.id, Trade.status == "OPEN")
                .values(status="CLOSING")
                .returning(Trade.id)
            )
            if res.scalar_one_or_none() is None:
                return False
        trade.status = "CLOSING"
        return True

    # ── Close ────────────────────────────────────────────────────────────────

    async def close(
        self,
        trade: Trade,
        exit_price: float,
        reason: str,
        exit_result: Optional[order_lifecycle.ExitResult] = None,
    ) -> CloseOutcome:
        """Close a position: claim OPEN → CLOSING, exit at the broker, then
        finalise CLOSING → CLOSED.

        A failed claim returns without touching the broker (another closer
        owns the exit, audit C4).  If the process crashes after the SELL but
        before finalisation, the row stays CLOSING and is reconciled against
        the broker (audit R9).
        """
        if not await self.claim(trade):
            logger.info("%s (id=%s) is no longer OPEN — close skipped", trade.stock, trade.id)
            return CloseOutcome(False, "trade is no longer OPEN (another closer owns it)", claimed=False)
        with order_lifecycle.exit_in_flight(trade.id):
            return await self.close_claimed(trade, exit_price, reason, exit_result)

    async def close_claimed(
        self,
        trade: Trade,
        exit_price: float,
        reason: str,
        exit_result: Optional[order_lifecycle.ExitResult] = None,
    ) -> CloseOutcome:
        """Broker exit + finalisation for a row this caller already claimed.

        The resting broker stop is cancelled AND confirmed before any SELL (if
        it already executed, its fill is booked and no SELL is sent); the SELL
        is sized to min(DB qty, broker-held qty) and booked from the broker's
        ``average_price``.  An unfilled / rejected exit leaves the trade OPEN
        (re-protected by a fresh broker stop) and alerts.
        """
        now_ist = datetime.now(IST)
        broker = self._deps.broker()
        result = exit_result or await order_lifecycle.execute_exit(
            broker, trade, trade.quantity, trade.stop_loss_price, tag=reason.lower(),
        )
        if result.outcome in _UNTOUCHED_OUTCOMES:
            await self.revert_to_open(trade)
            detail = (
                f"{result.message or 'broker stop-loss cancel could NOT be confirmed'} — "
                f"no SELL sent, position left OPEN (will retry next poll)"
            )
            if result.outcome == "CIRCUIT_LOCKED":
                await self._alert_circuit_once(trade, reason, detail)
            else:
                await self.alert_exit_problem(trade, reason, detail)
            return CloseOutcome(False, detail)
        if result.outcome == "SELL_UNKNOWN":
            detail = (
                f"{result.message}. Broker stop is CANCELLED. Position left CLOSING — "
                f"check the broker order book NOW."
            )
            await self.alert_exit_problem(trade, reason, detail, critical=True)
            return CloseOutcome(False, detail)

        if result.sold_qty == 0 and result.remaining_qty == 0:
            # The broker was already flat and nothing was sold or priced here.
            # Never book that at zero P&L: leave CLOSING for reconciliation.
            detail = (
                "broker already flat — no fill found by this exit. Left CLOSING; "
                "reconciliation will book it from the broker order book"
            )
            await self.alert_exit_problem(trade, reason, detail, critical=True)
            return CloseOutcome(False, detail)

        if result.stop_fired and result.remaining_qty == 0:
            reason = stop_exit_reason(trade)

        if result.sold_qty == 0 and result.remaining_qty > 0:
            await self.revert_to_open(trade, sl_order_id=result.sl_order_id)
            detail = "exit SELL was rejected / not filled — position kept OPEN" + (
                " (broker stop re-placed)" if result.sl_order_id else
                " and NO broker stop could be re-placed"
            )
            await self.alert_exit_problem(trade, reason, detail, critical=result.stop_restore_failed)
            return CloseOutcome(False, detail)

        if result.exit_price:
            exit_price = result.exit_price

        if result.remaining_qty > 0:
            await self.book_exit_leg(trade, result, exit_price)
            detail = (
                f"exit only partially filled: sold {result.sold_qty}, "
                f"{result.remaining_qty} still held and kept OPEN"
                + ("" if result.sl_order_id else " with NO broker stop")
            )
            await self.alert_exit_problem(trade, reason, detail, critical=result.stop_restore_failed)
            return CloseOutcome(False, detail)

        if result.sold_qty < trade.quantity:
            logger.error(
                "Exit for %s: DB qty %d but broker held %s — booked %d",
                trade.stock, trade.quantity, result.broker_qty, result.sold_qty,
            )
            await self.alert_exit_problem(
                trade, reason,
                f"DB quantity {trade.quantity} but broker held {result.broker_qty}; "
                f"booked {result.sold_qty} — reconcile manually",
            )

        if await self.finalise_close(trade, exit_price, reason, result.sold_qty, now_ist):
            return CloseOutcome(True)
        return CloseOutcome(False, "row was finalised by another closer first")

    async def finalise_close(
        self,
        trade: Trade,
        exit_price: float,
        reason: str,
        qty_closed: int,
        now_ist: datetime,
    ) -> bool:
        """CLOSING → CLOSED with net P&L (incl. earlier booked legs).

        Conditional on ``status == 'CLOSING'`` with RETURNING checked (audit
        C4): another closer's booking is never overwritten.  The stock lock and
        loss streak are then driven by the sign of the net P&L (audit R14).
        """
        gross_pnl = (exit_price - trade.entry_price) * qty_closed
        costs = (
            self._deps.costs(trade.entry_price, exit_price, qty_closed, trade.product_type)
            if qty_closed > 0 else 0.0
        )
        booked = getattr(trade, "booked_pnl", 0.0) or 0.0
        pnl = round(gross_pnl - costs + booked, 2)

        async with self._deps.db_context() as session:
            finalised = await session.execute(
                update(Trade)
                .where(Trade.id == trade.id, Trade.status == "CLOSING")
                .values(
                    exit_price=exit_price,
                    exit_reason=reason,
                    exit_time=now_ist.time(),
                    realized_pnl=pnl,
                    status="CLOSED",
                    sl_order_id=None,
                )
                .returning(Trade.id)
            )
            if finalised.scalar_one_or_none() is None:
                logger.error(
                    "%s (id=%s) was no longer CLOSING at finalisation — another closer won; "
                    "booking skipped", trade.stock, trade.id,
                )
                return False
        trade.status = "CLOSED"
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.realized_pnl = pnl
        trade.sl_order_id = None

        logger.info(
            "%s: %s @ ₹%.2f → gross P&L: ₹%.2f | costs: ₹%.2f | booked: ₹%.2f | net P&L: ₹%.2f",
            reason, trade.stock, exit_price, gross_pnl, costs, booked, pnl,
        )
        await self._deps.publish("trade_events", {
            "type": "TRADE_CLOSED",
            "stock": trade.stock,
            "exit_price": exit_price,
            "exit_reason": reason,
            "pnl": pnl,
            "timestamp": now_ist.isoformat(),
        })
        try:
            await self._deps.record_outcome(trade.stock, pnl, self._settings, now_ist)
        except Exception as exc:
            logger.error("Loss-streak / stock-lock update failed for %s: %s", trade.stock, exc)
        return True

    async def book_exit_leg(self, trade: Trade, result, exit_price: float) -> None:
        """A partially filled exit: book the sold shares, keep the rest OPEN."""
        sold = result.sold_qty
        gross = (exit_price - trade.entry_price) * sold
        costs = self._deps.costs(trade.entry_price, exit_price, sold, trade.product_type)
        new_booked = round((getattr(trade, "booked_pnl", 0.0) or 0.0) + gross - costs, 2)
        async with self._deps.db_context() as session:
            await session.execute(
                update(Trade)
                .where(Trade.id == trade.id, Trade.status == "CLOSING")
                .values(
                    status="OPEN",
                    quantity=result.remaining_qty,
                    booked_pnl=new_booked,
                    sl_order_id=result.sl_order_id,
                )
            )
        trade.status = "OPEN"
        trade.quantity = result.remaining_qty
        trade.booked_pnl = new_booked
        trade.sl_order_id = result.sl_order_id

    # ── Partial booking ──────────────────────────────────────────────────────

    async def book_partial(self, trade: Trade, ltp: float) -> None:
        """Scale out ``partial_booking_fraction`` of the position at market.

        The row is claimed (OPEN → CLOSING) before any broker action so no
        other closer can run a concurrent exit on it (audit C4).  The resting
        stop is cancelled and confirmed before the SELL, the SELL is booked
        from the broker fill, and the stop is re-placed for the remainder (at
        breakeven when a leg was sold, at the old stop otherwise).
        """
        if not await self.claim(trade):
            logger.info("%s (id=%s) is no longer OPEN — scale-out skipped", trade.stock, trade.id)
            return
        with order_lifecycle.exit_in_flight(trade.id):
            await self._book_partial_claimed(trade, ltp)

    async def _book_partial_claimed(self, trade: Trade, ltp: float) -> None:
        settings = self._settings
        now_ist = datetime.now(IST)

        qty = trade.quantity
        book_qty, remaining = exit_policy.partial_split(qty, settings)
        if remaining < 1:
            await self.revert_to_open(trade)
            return

        new_sl = exit_policy.breakeven_stop(trade, settings, get_tick_size(trade.stock))

        broker = self._deps.broker()
        result = await order_lifecycle.execute_exit(
            broker, trade, book_qty, new_sl,
            tag="partial_book", unfilled_stop_price=trade.stop_loss_price,
        )
        if result.outcome in _UNTOUCHED_OUTCOMES:
            logger.warning("Partial book: %s for %s — aborting scale-out", result.message, trade.stock)
            await self.revert_to_open(trade)
            return
        if result.outcome == "SELL_UNKNOWN":
            trade.status = "CLOSING"
            await self.alert_exit_problem(
                trade, "PARTIAL_BOOK",
                f"{result.message}. Broker stop is CANCELLED. Position moved to CLOSING — "
                f"check the broker order book NOW.",
                critical=True,
            )
            return

        if result.remaining_qty == 0:
            # The broker stop had already executed (or the broker is flat):
            # finalise the trade from the broker fill — no extra SELL.
            await self.close_claimed(
                trade, result.exit_price or ltp,
                stop_exit_reason(trade) if result.stop_fired else "PARTIAL_BOOK",
                exit_result=result,
            )
            return

        if result.sold_qty == 0:
            await self.revert_to_open(trade, sl_order_id=result.sl_order_id)
            await self.alert_exit_problem(
                trade, "PARTIAL_BOOK",
                "scale-out SELL was rejected / not filled — position unchanged"
                + ("" if result.sl_order_id else " and NO broker stop could be re-placed"),
                critical=result.stop_restore_failed,
            )
            return

        book_qty = result.sold_qty
        remaining = result.remaining_qty
        fill_price = result.exit_price or ltp
        new_sl_order_id = result.sl_order_id
        if result.stop_restore_failed:
            await self.alert_exit_problem(
                trade, "PARTIAL_BOOK",
                f"scale-out done but NO broker stop could be placed for the "
                f"remaining {remaining} — RiskManager poll will enforce the stop",
                critical=True,
            )

        gross = (fill_price - trade.entry_price) * book_qty
        costs = self._deps.costs(trade.entry_price, fill_price, book_qty, trade.product_type)
        leg_pnl = round(gross - costs, 2)
        new_booked = round((getattr(trade, "booked_pnl", 0.0) or 0.0) + leg_pnl, 2)
        # A stop raised to breakeven exits as BREAKEVEN, not STOP_LOSS_HIT (R14).
        new_kind = "BREAKEVEN" if new_sl > trade.stop_loss_price else getattr(trade, "stop_kind", None)

        async with self._deps.db_context() as session:
            written = await session.execute(
                update(Trade)
                .where(Trade.id == trade.id, Trade.status == "CLOSING")
                .values(
                    status="OPEN",
                    quantity=remaining,
                    booked_pnl=new_booked,
                    partial_booked=True,
                    stop_loss_price=new_sl,
                    stop_kind=new_kind,
                    sl_order_id=new_sl_order_id,
                )
                .returning(Trade.id)
            )
            missed = written.scalar_one_or_none() is None
        if missed:
            await self.alert_exit_problem(
                trade, "PARTIAL_BOOK",
                f"scale-out sold {book_qty} @ ₹{fill_price:.2f} but the row was changed "
                f"concurrently — leg NOT booked; broker stop {new_sl_order_id} protects "
                f"{remaining}. Reconcile manually.",
                critical=True,
            )
            return

        trade.status = "OPEN"
        trade.quantity = remaining
        trade.booked_pnl = new_booked
        trade.partial_booked = True
        trade.stop_loss_price = new_sl
        trade.stop_kind = new_kind
        trade.sl_order_id = new_sl_order_id

        logger.info(
            "Partial book: %s sold %d/%d @ ₹%.2f | leg P&L: ₹%.2f | runner: %d @ SL ₹%.2f",
            trade.stock, book_qty, qty, fill_price, leg_pnl, remaining, new_sl,
        )
        await self._deps.notify(
            f"\U0001f4b0 Partial book: {trade.stock}\n"
            f"Sold {book_qty}/{qty} @ ₹{fill_price:.2f} | Leg P&L: ₹{leg_pnl:.2f}\n"
            f"Runner: {remaining} @ SL ₹{new_sl:.2f}"
            + (" (paper)" if settings.paper_trading else "")
        )
        await self._deps.publish("trade_events", {
            "type": "PARTIAL_BOOKED",
            "stock": trade.stock,
            "booked_qty": book_qty,
            "remaining_qty": remaining,
            "price": fill_price,
            "leg_pnl": leg_pnl,
            "new_stop_loss": new_sl,
            "timestamp": now_ist.isoformat(),
        })

    # ── Stop / target moves ──────────────────────────────────────────────────

    async def set_sl_order_id(self, trade: Trade, sl_order_id: Optional[str]) -> None:
        async with self._deps.db_context() as session:
            await session.execute(
                update(Trade).where(Trade.id == trade.id).values(sl_order_id=sl_order_id)
            )
        trade.sl_order_id = sl_order_id

    async def update_stop_loss(self, trade: Trade, new_sl: float, kind: str = "TRAILING") -> None:
        """Raise the stop (trailing / breakeven) in the DB and at the broker.

        *kind* is persisted as ``stop_kind`` so a later stop-out is labelled
        TRAILING_SL / BREAKEVEN (R14).  If the broker modify fails the old
        (lower, still protective, same-size) broker stop stays in place and the
        RiskManager poll enforces the higher DB stop client-side.
        """
        async with self._deps.db_context() as session:
            await session.execute(
                update(Trade)
                .where(Trade.id == trade.id, Trade.status == "OPEN")
                .values(stop_loss_price=new_sl, stop_kind=kind)
            )
        trade.stop_loss_price = new_sl
        trade.stop_kind = kind

        sl_id = getattr(trade, "sl_order_id", None)
        if order_lifecycle.is_unresolved_stop(sl_id):
            logger.warning(
                "Broker stop for %s is unresolved (%s) — not modified; DB stop enforced by poll",
                trade.stock, sl_id,
            )
        elif sl_id:
            try:
                await self._deps.broker().modify_stop_loss_order(
                    order_id=sl_id,
                    tradingsymbol=trade.stock,
                    quantity=int(trade.quantity),
                    trigger_price=new_sl,
                )
            except Exception as exc:
                logger.warning(
                    "Broker SL modify failed for %s (%s → ₹%.2f): %s — old broker stop "
                    "stays; DB stop enforced by poll",
                    trade.stock, sl_id, new_sl, exc,
                )

        await self._deps.publish("trade_events", {
            "type": "TRAILING_SL_UPDATE",
            "stock": trade.stock,
            "new_stop_loss": new_sl,
            "timestamp": datetime.now(IST).isoformat(),
        })

    async def update_target(self, trade: Trade, new_target: float) -> None:
        """Lower the target (ROI decay)."""
        async with self._deps.db_context() as session:
            await session.execute(
                update(Trade)
                .where(Trade.id == trade.id, Trade.status == "OPEN")
                .values(target_price=new_target)
            )
        trade.target_price = new_target
        await self._deps.publish("trade_events", {
            "type": "ROI_DECAY_UPDATE",
            "stock": trade.stock,
            "new_target": new_target,
            "timestamp": datetime.now(IST).isoformat(),
        })

    # ── Alerts ───────────────────────────────────────────────────────────────

    async def alert_exit_problem(
        self, trade: Trade, reason: str, detail: str, critical: bool = False,
    ) -> None:
        """Log + dashboard + Telegram alert for an exit that did not complete."""
        log = logger.critical if critical else logger.error
        log("Exit problem (%s) for %s: %s", reason, trade.stock, detail)
        try:
            await self._deps.publish("system_alerts", {
                "type": "critical" if critical else "danger",
                "message": f"{reason} {trade.stock}: {detail}",
                "timestamp": datetime.now(IST).isoformat(),
            })
            await self._deps.notify(
                f"{chr(0x1F6A8) if critical else chr(0x26A0)} {reason} {trade.stock}: {detail}"
            )
        except Exception as exc:
            logger.warning("Exit-problem alert failed for %s: %s", trade.stock, exc)

    async def _alert_circuit_once(self, trade: Trade, reason: str, detail: str) -> None:
        key = (int(trade.id or 0), datetime.now(IST).date())
        if key in _circuit_alerted:
            logger.warning("Exit (%s) for %s still blocked by the lower circuit", reason, trade.stock)
            return
        _circuit_alerted.add(key)
        await self.alert_exit_problem(trade, reason, detail, critical=True)
