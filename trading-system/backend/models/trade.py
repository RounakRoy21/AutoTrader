"""
SQLAlchemy model for the trades table.
Tracks every trade from entry through exit with full context.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from sqlalchemy import (
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
    kite_order_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
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
    )  # TARGET_HIT / STOP_LOSS_HIT / MANUAL / EOD_CLOSE
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="OPEN", index=True
    )  # OPEN / CLOSING / CLOSED
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    entry_time: Mapped[time] = mapped_column(Time, nullable=False)
    exit_time: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    decision_rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    gtt_trigger_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<Trade {self.stock} {self.direction} qty={self.quantity} "
            f"status={self.status} pnl={self.realized_pnl}>"
        )
