"""
REST endpoints for P&L reporting.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from models.daily_pnl import DailyPnl
from models.trade import Trade

router = APIRouter(prefix="/api/pnl", tags=["P&L"])


class DailyPnlResponse(BaseModel):
    id: int
    date: date
    starting_capital: float
    ending_capital: float
    realized_pnl: float
    unrealized_pnl: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    return_pct: float
    trading_halted: bool
    profit_factor: Optional[float] = None
    sharpe_ratio: Optional[float] = None
    avg_trade_duration_min: Optional[float] = None
    max_consecutive_losses: Optional[int] = None

    class Config:
        from_attributes = True


@router.get("/daily", response_model=List[DailyPnlResponse])
async def get_daily_pnl(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Return daily P&L summaries for the last *days* days.

    If today has no EOD row yet (intraday), a synthetic live row is prepended
    computed from today's CLOSED trades, so the dashboard shows real-time P&L.
    """
    today = date.today()

    result = await db.execute(
        select(DailyPnl).order_by(DailyPnl.date.desc()).limit(days)
    )
    rows: list[Union[DailyPnl, DailyPnlResponse]] = list(result.scalars().all())

    # Prepend a live intraday row if no EOD row exists for today
    if not rows or rows[0].date != today:  # type: ignore[union-attr]
        closed_result = await db.execute(
            select(Trade).where(
                and_(Trade.trade_date == today, Trade.status == "CLOSED")
            )
        )
        trades_today = list(closed_result.scalars().all())

        realized = sum(t.realized_pnl or 0.0 for t in trades_today)
        total = len(trades_today)
        wins = sum(1 for t in trades_today if (t.realized_pnl or 0.0) > 0)

        # Always insert the intraday row (even when total==0) so the dashboard
        # shows ₹0 intentionally rather than carrying yesterday's figure.
        intraday = DailyPnlResponse(
            id=0,
            date=today,
            starting_capital=0.0,
            ending_capital=0.0,
            realized_pnl=realized,
            unrealized_pnl=0.0,
            total_trades=total,
            winning_trades=wins,
            losing_trades=total - wins,
            return_pct=0.0,
            trading_halted=False,
        )
        rows.insert(0, intraday)

    return rows
