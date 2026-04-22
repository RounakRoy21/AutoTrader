"""
REST endpoints for the Market Brief.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.research_agent import run_research_agent
from core.database import get_db
from core.nse_calendar import ist_today
from core.redis_client import get_redis
from core.redis_keys import LATEST_MARKET_BRIEF_KEY
from models.market_brief import MarketBrief
from schemas.market_brief import MarketBriefResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/market-brief", tags=["Market Brief"])

# ── Status ─────────────────────────────────────────────────────────────────────

@router.get("/status")
async def get_research_agent_status():
    """Return the current status of the Research Agent from Redis."""
    r = await get_redis()
    keys = [
        "agent:research:status",
        "agent:research:step",
        "agent:research:last_run_started",
        "agent:research:last_run_completed",
        "agent:research:last_bias",
        "agent:research:last_confidence",
    ]
    values = await asyncio.gather(*[r.get(k) for k in keys])
    return {
        "status": values[0] or "INACTIVE",
        "step": values[1] or "IDLE",
        "last_run_started": values[2],
        "last_run_completed": values[3],
        "last_bias": values[4],
        "last_confidence": float(values[5]) if values[5] else None,
    }


# ── Manual trigger ──────────────────────────────────────────────────────────────

@router.post("/run", status_code=202)
async def trigger_research_agent(background_tasks: BackgroundTasks):
    """
    Manually trigger the Research Agent.
    Useful for testing outside the 6 AM scheduled window.
    Returns immediately; check /status or /today for the result.
    """
    background_tasks.add_task(run_research_agent)
    return {
        "status": "accepted",
        "message": "Research Agent triggered — check /api/market-brief/status for progress",
    }


# ── Queries ─────────────────────────────────────────────────────────────────────

@router.get("/today", response_model=MarketBriefResponse)
async def get_today_brief(db: AsyncSession = Depends(get_db)):
    """Return today's Market Brief (IST date). If absent from DB, check Redis cache."""
    today = ist_today()
    result = await db.execute(
        select(MarketBrief)
        .where(MarketBrief.date == today)
        .order_by(MarketBrief.created_at.desc())
        .limit(1)
    )
    brief = result.scalar_one_or_none()
    if brief is not None:
        return brief

    # Fallback: check Redis for a cached brief generated today
    r = await get_redis()
    cached = await r.get(LATEST_MARKET_BRIEF_KEY)
    if cached:
        data = json.loads(cached)
        brief_date = data.get("date", "")
        if brief_date == today.isoformat():
            return MarketBriefResponse(
                id=-1,
                date=today,
                generated_at=data.get("generated_at", "00:00:00"),
                market_bias=data.get("market_bias", "NEUTRAL"),
                bias_confidence=data.get("bias_confidence", 0.0),
                sgx_nifty_signal=data.get("sgx_nifty", {}).get("signal"),
                fii_signal=data.get("fii_dii", {}).get("signal"),
                dxy_signal=data.get("dxy", {}).get("signal"),
                us_markets_signal=data.get("us_markets", {}).get("signal"),
                watchlist=data.get("watchlist_today", []),
                avoid_list=data.get("avoid_today", []),
                recommended_stance=data.get("recommended_stance"),
                raw_json=data,
            )

    raise HTTPException(
        status_code=404,
        detail="No market brief found for today — trigger one via POST /api/market-brief/run",
    )


@router.get("/latest", response_model=MarketBriefResponse)
async def get_latest_brief(db: AsyncSession = Depends(get_db)):
    """Return the most recently generated Market Brief regardless of date."""
    result = await db.execute(
        select(MarketBrief)
        .order_by(MarketBrief.created_at.desc())
        .limit(1)
    )
    brief = result.scalar_one_or_none()
    if brief is None:
        raise HTTPException(status_code=404, detail="No market briefs exist yet")
    return brief
