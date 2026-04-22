"""
REST endpoints for trade records.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import List, Optional

import pytz
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agents.risk_manager import _transaction_costs
from core.config import get_settings
from core.database import get_db
from core.redis_client import publish
from integrations import ltp_store
from integrations.groww_client import get_groww_client
from models.trade import Trade
from schemas.trade import TradeResponse

router = APIRouter(prefix="/api/trades", tags=["Trades"])

IST = pytz.timezone("Asia/Kolkata")


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


@router.post("/{trade_id}/close", response_model=TradeResponse)
async def close_trade_manually(trade_id: int, db: AsyncSession = Depends(get_db)):
    """Manually close an open position at the current market price.

    Follows the same two-phase DB pattern as the Risk Manager:
    OPEN → CLOSING → CLOSED.  If the broker call fails the row stays
    CLOSING and can be reconciled at EOD.
    """
    settings = get_settings()
    now_ist = datetime.now(IST)

    # ── Fetch and validate the trade ──────────────────────────
    result = await db.execute(select(Trade).where(Trade.id == trade_id))
    trade = result.scalar_one_or_none()
    if trade is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    if trade.status != "OPEN":
        raise HTTPException(
            status_code=409,
            detail=f"Trade is not OPEN (current status: {trade.status})",
        )

    # ── Phase 1: mark CLOSING (CAS — only if still OPEN) ─────
    updated = await db.execute(
        update(Trade)
        .where(Trade.id == trade_id, Trade.status == "OPEN")
        .values(status="CLOSING")
        .returning(Trade.id)
    )
    if updated.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=409,
            detail="Concurrent close detected — trade is no longer OPEN",
        )
    await db.commit()

    # ── Phase 1b: cancel any GTT stop-loss (live mode only) ──
    if not settings.paper_trading and trade.gtt_trigger_id:
        try:
            groww = get_groww_client()
            await groww.delete_gtt(trade.gtt_trigger_id)
        except Exception:
            pass  # Non-fatal; GTT may already be triggered or expired

    # ── Determine exit price ──────────────────────────────────
    exit_price: float
    if settings.paper_trading:
        exit_price = ltp_store.get_ltp(trade.stock) or trade.entry_price
    else:
        try:
            groww = get_groww_client()
            ltp_data = await groww.get_ltp([f"NSE:{trade.stock}"])
            exit_price = ltp_data[f"NSE:{trade.stock}"]["last_price"]
        except Exception as exc:
            # Revert to OPEN so the operator can retry
            await db.execute(
                update(Trade).where(Trade.id == trade_id).values(status="OPEN")
            )
            await db.commit()
            raise HTTPException(status_code=502, detail=f"Failed to fetch LTP: {exc}") from exc

    # ── Phase 2: place SELL order ─────────────────────────────
    try:
        if not settings.paper_trading:
            groww = get_groww_client()
            await groww.place_order(
                tradingsymbol=trade.stock,
                exchange=trade.exchange,
                transaction_type="SELL",
                quantity=trade.quantity,
                product=trade.product_type,
                order_type="MARKET",
                tag="manual_close",
            )
    except Exception as exc:
        # Leave as CLOSING — do NOT revert (order may have been sent)
        raise HTTPException(
            status_code=502,
            detail=f"Broker order failed — position stays CLOSING for manual review: {exc}",
        ) from exc

    # ── Phase 3: compute P&L and mark CLOSED ──────────────────
    gross_pnl = (exit_price - trade.entry_price) * trade.quantity
    costs = _transaction_costs(trade.entry_price, exit_price, trade.quantity, trade.product_type)
    pnl = round(gross_pnl - costs, 2)

    await db.execute(
        update(Trade)
        .where(Trade.id == trade_id)
        .values(
            exit_price=exit_price,
            exit_reason="MANUAL",
            exit_time=now_ist.time(),
            realized_pnl=pnl,
            status="CLOSED",
        )
    )
    await db.commit()
    await db.refresh(trade)

    await publish("trade_events", {
        "type": "TRADE_CLOSED",
        "stock": trade.stock,
        "exit_price": exit_price,
        "exit_reason": "MANUAL",
        "pnl": pnl,
        "timestamp": now_ist.isoformat(),
    })

    return trade

