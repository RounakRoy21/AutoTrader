"""
REST endpoints for P&L reporting.
"""

from __future__ import annotations

from datetime import date
from typing import List

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from models.daily_pnl import DailyPnl

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

    class Config:
        from_attributes = True


@router.get("/daily", response_model=List[DailyPnlResponse])
async def get_daily_pnl(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Return daily P&L summaries for the last *days* days."""
    result = await db.execute(
        select(DailyPnl).order_by(DailyPnl.date.desc()).limit(days)
    )
    return result.scalars().all()
