"""
SQLAlchemy model for the daily_pnl table.
End-of-day summary of trading performance.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, func
from sqlalchemy.orm import Mapped, mapped_column
from typing import Optional

from core.database import Base


class DailyPnl(Base):
    """Daily profit-and-loss summary record."""

    __tablename__ = "daily_pnl"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    starting_capital: Mapped[float] = mapped_column(Float, nullable=False)
    ending_capital: Mapped[float] = mapped_column(Float, nullable=False)
    realized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    return_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    trading_halted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ── Analytics (computed at EOD, persisted for strategy analysis) ──────────
    profit_factor: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sharpe_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    avg_trade_duration_min: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_consecutive_losses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<DailyPnl date={self.date} pnl={self.realized_pnl} "
            f"trades={self.total_trades} halted={self.trading_halted}>"
        )
