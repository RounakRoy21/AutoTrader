"""
REST endpoints for trade records.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from models.trade import Trade
from schemas.trade import TradeResponse

router = APIRouter(prefix="/api/trades", tags=["Trades"])


@router.get("", response_model=List[TradeResponse])
async def get_trades(
    trade_date: Optional[date] = Query(None, alias="date"),
    db: AsyncSession = Depends(get_db),
):
    """Return all trades, optionally filtered by date."""
    stmt = select(Trade).order_by(Trade.created_at.desc())
    if trade_date:
        stmt = stmt.where(Trade.trade_date == trade_date)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/open", response_model=List[TradeResponse])
async def get_open_trades(db: AsyncSession = Depends(get_db)):
    """Return all currently open positions (including those mid-close)."""
    result = await db.execute(
        select(Trade)
        .where(Trade.status.in_(["OPEN", "CLOSING"]))
        .order_by(Trade.entry_time.desc())
    )
    return result.scalars().all()
