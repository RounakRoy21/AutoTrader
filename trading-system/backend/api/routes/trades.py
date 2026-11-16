"""
REST endpoints for trade records.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.position_service import PositionDeps, PositionService
from agents.risk_manager import _transaction_costs, record_close_outcome
from core.config import get_settings
from core.database import get_db, get_db_context
from core.redis_client import publish
from core.security import require_api_key
from integrations import ltp_store
from integrations.broker import get_broker
from integrations.groww_client import get_groww_client
from integrations.telegram_client import send_telegram
from models.trade import Trade
from schemas.trade import TradeResponse

router = APIRouter(prefix="/api/trades", tags=["Trades"])

logger = logging.getLogger(__name__)


@router.get("", response_model=List[TradeResponse])
async def get_trades(
    trade_date: Optional[date] = Query(None, alias="date"),
    limit: int = Query(default=100, ge=1, le=500, description="Max rows to return"),
    offset: int = Query(default=0, ge=0, description="Rows to skip"),
    db: AsyncSession = Depends(get_db),
):
    """Return trades, optionally filtered by date. Paginated via limit/offset."""
    stmt = select(Trade).order_by(Trade.created_at.desc()).limit(limit).offset(offset)
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


# Sends a live MARKET SELL in live mode — admin API key required (audit R1).
@router.post(
    "/{trade_id}/close",
    response_model=TradeResponse,
    dependencies=[Depends(require_api_key)],
)
async def close_trade_manually(trade_id: int, db: AsyncSession = Depends(get_db)):
    """Manually close an open position at market.

    Runs the same ``PositionService.close`` as the Risk Manager: OPEN →
    CLOSING claim (CAS), cancel + confirm the broker stop, SELL, book from the
    broker fill, CLOSING → CLOSED.  A rejected / unfilled SELL leaves the
    trade OPEN; an unknown SELL state leaves it CLOSING for reconciliation.
    """
    settings = get_settings()

    result = await db.execute(select(Trade).where(Trade.id == trade_id))
    trade = result.scalar_one_or_none()
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status != "OPEN":
        raise HTTPException(
            status_code=409,
            detail=f"Trade is not OPEN (current status: {trade.status})",
        )
    # PositionService writes through its own sessions with status guards; this
    # request session must never flush the in-memory copy over them.
    db.expunge(trade)

    # Booking estimate only — a broker fill price always takes precedence.
    if settings.paper_trading:
        exit_price = ltp_store.get_ltp(trade.stock) or trade.entry_price
    else:
        try:
            ltp_data = await get_groww_client().get_ltp([f"NSE:{trade.stock}"])
            exit_price = ltp_data[f"NSE:{trade.stock}"]["last_price"]
        except Exception as exc:
            logger.warning("Manual close LTP fetch failed for %s: %s", trade.stock, exc)
            raise HTTPException(
                status_code=502, detail="Failed to fetch LTP — nothing sold, retry shortly",
            ) from exc

    positions = PositionService(settings, PositionDeps(
        db_context=get_db_context,
        broker=lambda: get_broker(settings),
        publish=publish,
        notify=send_telegram,
        costs=_transaction_costs,
        record_outcome=record_close_outcome,
    ))
    outcome = await positions.close(trade, exit_price, "MANUAL")
    if not outcome:
        raise HTTPException(
            status_code=409 if not outcome.claimed else 502,
            detail=outcome.detail,
        )
    return trade

