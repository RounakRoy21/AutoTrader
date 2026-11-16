"""
SQLAlchemy model for the trades table.
Tracks every trade from entry through exit with full context.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    Time,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class Trade(Base):
    """Individual trade record — one row per entry/exit cycle."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # Broker order id of the entry BUY.
    entry_order_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    stock: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(10), nullable=False, default="NSE")
    direction: Mapped[str] = mapped_column(String(4), nullable=False)  # BUY / SELL
    product_type: Mapped[str] = mapped_column(String(5), nullable=False)  # MIS / CNC
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss_price: Mapped[float] = mapped_column(Float, nullable=False)
    target_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(
        String(20), nullable=True
    )  # TARGET_HIT / STOP_LOSS_HIT / TRAILING_SL / BREAKEVEN / MANUAL / EOD_CLOSE / PARTIAL_BOOK / RECONCILED
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="OPEN", index=True
    )  # PENDING / OPEN / CLOSING / CLOSED / REJECTED
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    entry_time: Mapped[time] = mapped_column(Time, nullable=False)
    exit_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    decision_rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Full raw broker order id of the resting protective SL SELL (audit R1 / C1).
    # Every exit path cancels-and-confirms this order before selling.
    sl_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # How the stop was last moved (audit R14): NULL = initial stop, TRAILING,
    # BREAKEVEN.  Drives the exit reason of a stop-out (STOP_LOSS_HIT /
    # TRAILING_SL / BREAKEVEN).
    stop_kind: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    # order_reference_id of the entry BUY, written with the PENDING row before
    # the order is sent (audit C9); the reconciler resolves stranded rows by it.
    entry_order_ref: Mapped[Optional[str]] = mapped_column(String(32), nullable=True, unique=True, index=True)
    # ── Partial profit-booking / scale-out ────────────────────────────────────
    # original_quantity preserves the full entry size for auditing once `quantity`
    # is reduced to the remaining position after a partial book.  partial_target_price
    # is the (immutable) price at which the scale-out fires; partial_booked guards
    # against booking more than once; booked_pnl accumulates the net realised P&L of
    # the booked leg(s) and is folded into realized_pnl when the position fully closes.
    original_quantity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    partial_target_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    partial_booked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    booked_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True, default=0.0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<Trade {self.stock} {self.direction} qty={self.quantity} "
            f"status={self.status} pnl={self.realized_pnl}>"
        )
