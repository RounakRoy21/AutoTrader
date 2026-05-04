"""
REST endpoints for P&L reporting.
"""

from __future__ import annotations

from datetime import date
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, distinct, select
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
    avg_realised_rr: Optional[float] = None
    losses_before_1030: Optional[int] = None
    losses_1030_to_1330: Optional[int] = None
    losses_after_1330: Optional[int] = None

    class Config:
        from_attributes = True


@router.get("/daily", response_model=List[DailyPnlResponse])
async def get_daily_pnl(
    days: int = Query(default=30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
):
    """Return daily P&L summaries for the last *days* days.

    Handles two gap cases automatically:
    1. TODAY — no EOD row yet: prepends a live intraday row computed from
       today's CLOSED trades so the dashboard shows real-time P&L.
    2. PAST DAYS — EOD row missing (system was down at 15:30 IST): synthesises
       a summary row from closed trades so days are never silently absent from
       the P&L chart even when the risk manager didn't complete an EOD run.
    """
    today = date.today()

    result = await db.execute(
        select(DailyPnl).order_by(DailyPnl.date.desc()).limit(days)
    )
    rows: list[Union[DailyPnl, DailyPnlResponse]] = list(result.scalars().all())

    # ── Case 1: live intraday row for today ─────────────────────────────────
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

    # ── Case 2: backfill past days that have trades but no DailyPnl row ─────
    # Find which past trade dates already have a DailyPnl row.
    existing_dates = {r.date for r in rows}  # type: ignore[union-attr]

    # Query all distinct past trade dates that are NOT in the DailyPnl table.
    orphan_result = await db.execute(
        select(distinct(Trade.trade_date)).where(
            and_(Trade.trade_date < today, Trade.status == "CLOSED")
        )
    )
    orphan_dates = [d for (d,) in orphan_result.all() if d not in existing_dates]

    if orphan_dates:
        # Fetch all trades for those dates in one query.
        trades_result = await db.execute(
            select(Trade).where(
                and_(Trade.trade_date.in_(orphan_dates), Trade.status == "CLOSED")
            )
        )
        all_orphan_trades = trades_result.scalars().all()

        # Group by date and synthesise a summary row for each.
        from collections import defaultdict
        by_date: dict[date, list] = defaultdict(list)
        for t in all_orphan_trades:
            by_date[t.trade_date].append(t)

        for d, trades in by_date.items():
            realized = sum(t.realized_pnl or 0.0 for t in trades)
            total = len(trades)
            wins = sum(1 for t in trades if (t.realized_pnl or 0.0) > 0)
            rows.append(DailyPnlResponse(
                id=0,
                date=d,
                starting_capital=0.0,
                ending_capital=0.0,
                realized_pnl=realized,
                unrealized_pnl=0.0,
                total_trades=total,
                winning_trades=wins,
                losing_trades=total - wins,
                return_pct=0.0,
                trading_halted=False,
            ))

    # Re-sort descending after any backfill insertions.
    rows.sort(key=lambda r: r.date, reverse=True)  # type: ignore[union-attr]

    return rows[:days]
