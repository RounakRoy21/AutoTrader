"""
System-level REST endpoints: agent status, health, trading halt/resume.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from core.config import get_settings
from core.database import check_db_health
from core.redis_client import check_redis_health, get_redis, get_value, set_value
from agents.trading_agent_manager import get_trading_agent_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["System"])

# ── Redis keys for agent state ────────────────────
HALT_KEY = "trading_halt"
RESEARCH_STATUS_KEY = "agent:research:status"
TRADING_STATUS_KEY = "agent:trading:status"


def _envelope(success: bool, data=None, error=None):
    return {
        "success": success,
        "data": data,
        "error": error,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/agent/status")
async def get_agent_status():
    """Return current status of all agents and key counters."""
    keys = [
        RESEARCH_STATUS_KEY,            # 0
        TRADING_STATUS_KEY,             # 1
        HALT_KEY,                       # 2
        "agent:research:step",          # 3
        "agent:research:last_bias",     # 4
        "agent:research:last_confidence",  # 5
        "agent:research:last_run_completed",  # 6
        "daily_trade_count",            # 7
        "agent:trading:last_signal_stock",  # 8
        "agent:trading:last_signal_time",   # 9
        "agent:risk:status",            # 10
        "agent:risk:daily_loss",        # 11
        "agent:risk:drawdown_pct",      # 12
    ]
    try:
        r = await get_redis()
        values = await asyncio.gather(*[r.get(k) for k in keys])
    except Exception:
        values = [None] * len(keys)

    return _envelope(True, {
        "research_agent": {
            "status": values[0] or "INACTIVE",
            "step": values[3] or "IDLE",
            "last_bias": values[4],
            "last_confidence": float(values[5]) if values[5] else None,
            "last_completed": values[6],
        },
        "trading_agent": {
            "status": values[1] or "INACTIVE",
            "trading_halted": (values[2] or "FALSE") == "TRUE",
            "daily_trade_count": int(values[7]) if values[7] else 0,
            "last_signal_stock": values[8],
            "last_signal_time": values[9],
        },
        "risk_manager": {
            "status": values[10] or "INACTIVE",
            "daily_loss": float(values[11]) if values[11] else 0.0,
            "drawdown_pct": float(values[12]) if values[12] else 0.0,
        },
    })


@router.post("/api/trading/halt")
async def halt_trading():
    """Manually set the HALT flag — stops all new trade entries."""
    await set_value(HALT_KEY, "TRUE")
    logger.warning("Trading manually halted via API")
    return _envelope(True, {"trading_halted": True})


@router.post("/api/trading/resume")
async def resume_trading():
    """Clear the HALT flag — allows trading to resume."""
    await set_value(HALT_KEY, "FALSE")
    logger.info("Trading resumed via API")
    return _envelope(True, {"trading_halted": False})


@router.post("/api/agent/trading/start")
async def manual_start_trading():
    """
    Manually start the Trading Agent outside of scheduled hours.
    Useful for testing, catch-up after a restart, or weekend ops.
    """
    manager = get_trading_agent_manager()
    result = await manager.start_session()
    success = result in ("started", "already_running")
    return _envelope(success, {"result": result})


@router.post("/api/agent/trading/stop")
async def manual_stop_trading():
    """Manually stop the Trading Agent (overrides the 15:30 scheduler)."""
    manager = get_trading_agent_manager()
    result = await manager.stop_session()
    return _envelope(True, {"result": result})


@router.get("/api/health")
async def health_check():
    """System health check: database, Redis, and Kite API reachability."""
    db_ok = await check_db_health()
    redis_ok = await check_redis_health()

    # Kite health: just check if we have a valid token in Redis
    kite_token = await get_value("kite_access_token")
    kite_ok = kite_token is not None and len(kite_token) > 0

    all_ok = db_ok and redis_ok
    return _envelope(
        success=all_ok,
        data={
            "database": "healthy" if db_ok else "unhealthy",
            "redis": "healthy" if redis_ok else "unhealthy",
            "kite_api": "authenticated" if kite_ok else "no_token",
        },
    )
