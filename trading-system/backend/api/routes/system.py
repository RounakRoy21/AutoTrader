"""
System-level REST endpoints: agent status, health, trading halt/resume.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from core.config import get_settings
from core.database import check_db_health
from core.redis_client import check_redis_health, get_redis, get_value, set_value
from core.redis_keys import (
    HALT_KEY,
    GROWW_TOKEN_KEY,
    RESEARCH_STATUS_KEY,
    TRADING_STATUS_KEY,
    RESEARCH_STEP_KEY,
    RESEARCH_LAST_BIAS_KEY,
    RESEARCH_LAST_CONFIDENCE_KEY,
    RESEARCH_LAST_RUN_COMPLETED_KEY,
    DAILY_TRADE_COUNT_KEY,
    TRADING_LAST_SIGNAL_STOCK_KEY,
    TRADING_LAST_SIGNAL_TIME_KEY,
    RISK_STATUS_KEY,
    RISK_DAILY_LOSS_KEY,
    RISK_DRAWDOWN_PCT_KEY,
    ANTHROPIC_CALLS_RESEARCH_KEY,
    ANTHROPIC_CALLS_DECISION_KEY,
    DECISION_FEED_KEY,
)
from core.nse_calendar import get_market_status
from agents.trading_agent_manager import get_trading_agent_manager

logger = logging.getLogger(__name__)
router = APIRouter(tags=["System"])

# ── API Key authentication ─────────────────────────────────────────────────────
_api_key_header = APIKeyHeader(name="X-Api-Key", auto_error=False)


async def _require_api_key(api_key: str = Security(_api_key_header)) -> None:
    """Dependency that enforces the admin API key on trading-control endpoints.

    When ADMIN_API_KEY is empty (dev / paper-trading mode), the check is skipped.
    In production, every caller must supply the matching key in the X-Api-Key header.
    """
    expected = get_settings().admin_api_key
    if not expected:
        return  # No key configured — open access (development only)
    if api_key != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Api-Key")


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
        RESEARCH_STEP_KEY,              # 3
        RESEARCH_LAST_BIAS_KEY,         # 4
        RESEARCH_LAST_CONFIDENCE_KEY,   # 5
        RESEARCH_LAST_RUN_COMPLETED_KEY,  # 6
        DAILY_TRADE_COUNT_KEY,          # 7
        TRADING_LAST_SIGNAL_STOCK_KEY,  # 8
        TRADING_LAST_SIGNAL_TIME_KEY,   # 9
        RISK_STATUS_KEY,                # 10
        RISK_DAILY_LOSS_KEY,            # 11
        RISK_DRAWDOWN_PCT_KEY,          # 12
        ANTHROPIC_CALLS_RESEARCH_KEY,   # 13
        ANTHROPIC_CALLS_DECISION_KEY,   # 14
    ]
    try:
        r = await get_redis()
        values = await asyncio.gather(*[r.get(k) for k in keys])
    except Exception:
        values = [None] * len(keys)

    settings = get_settings()
    calls_research = int(values[13]) if values[13] else 0
    calls_decision = int(values[14]) if values[14] else 0

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
        "anthropic": {
            "calls_research_today": calls_research,
            "calls_decision_today": calls_decision,
            "calls_total_today": calls_research + calls_decision,
        },
        "market_status": get_market_status(),
        "config": {
            "paper_trading": settings.paper_trading,
            "max_trades_per_day": settings.max_trades_per_day,
            "max_open_positions": settings.max_open_positions,
            "daily_drawdown_limit_pct": settings.daily_drawdown_limit_pct,
            "daily_drawdown_limit": settings.daily_drawdown_limit,
        },
    })


@router.post("/api/trading/halt", dependencies=[Depends(_require_api_key)])
async def halt_trading():
    """Manually set the HALT flag — stops all new trade entries."""
    await set_value(HALT_KEY, "TRUE")
    logger.warning("Trading manually halted via API")
    return _envelope(True, {"trading_halted": True})


@router.post("/api/trading/resume", dependencies=[Depends(_require_api_key)])
async def resume_trading():
    """Clear the HALT flag — allows trading to resume."""
    await set_value(HALT_KEY, "FALSE")
    logger.info("Trading resumed via API")
    return _envelope(True, {"trading_halted": False})


@router.post("/api/agent/trading/start", dependencies=[Depends(_require_api_key)])
async def manual_start_trading():
    """
    Manually start the Trading Agent outside of scheduled hours.
    Useful for testing, catch-up after a restart, or weekend ops.
    """
    manager = get_trading_agent_manager()
    result = await manager.start_session(source="manual")
    success = result in ("started", "already_running")
    return _envelope(success, {"result": result})


@router.post("/api/agent/trading/stop", dependencies=[Depends(_require_api_key)])
async def manual_stop_trading():
    """Manually stop the Trading Agent (overrides the 15:30 scheduler)."""
    manager = get_trading_agent_manager()
    result = await manager.stop_session(source="manual")
    return _envelope(True, {"result": result})


@router.get("/api/agent/decisions")
async def get_decision_feed(limit: int = 50):
    """Return the last N decision engine events (pre-check + LLM decisions)."""
    import json as _json
    try:
        r = await get_redis()
        raw_entries = await r.lrange(DECISION_FEED_KEY, 0, min(limit, 100) - 1)
        decisions = []
        for raw in raw_entries:
            try:
                decisions.append(_json.loads(raw))
            except Exception:
                pass
        return _envelope(True, {"decisions": decisions, "count": len(decisions)})
    except Exception as exc:
        logger.error("Failed to read decision feed: %s", exc)
        return _envelope(False, {"decisions": [], "count": 0}, error=str(exc))


@router.get("/api/agent/scanner-debug")
async def scanner_debug(request: Request):
    """Dump live scanner indicator values for the first few symbols (dev/testing only)."""
    manager = get_trading_agent_manager()
    agent = getattr(manager, "_agent", None)
    scanner = getattr(agent, "_scanner", None) if agent else None
    if scanner is None or not scanner._stores:
        return _envelope(True, {"message": "scanner not running or no stores yet", "stores": 0})
    out = {}
    for symbol, store in list(scanner._stores.items())[:6]:
        ticks = len(store.ticks)
        candles_df = store._candles_1m.completed_df
        n_candles = len(candles_df) if candles_df is not None else 0
        ltp = store.ltp
        vwap = store.compute_vwap()
        rsi = store.compute_rsi()
        rsi_5m = store.compute_rsi_htf()
        out[symbol] = {
            "ticks": ticks,
            "candles_1m": n_candles,
            "ltp": round(ltp, 2),
            "vwap": round(vwap, 2),
            "ltp_gt_vwap": ltp > vwap,
            "rsi": round(rsi, 2),
            "rsi_ok": 45 <= rsi <= 65,
            "rsi_5m": round(rsi_5m, 2) if rsi_5m is not None else None,
            "rsi_5m_ok": (rsi_5m is None) or (45 <= rsi_5m <= 72),
        }
    agent = getattr(manager, "_agent", None)
    signal_q = getattr(agent, "_signal_queue", None)
    q_size = signal_q.qsize() if signal_q else -1
    return _envelope(True, {"stores": len(scanner._stores), "signal_queue_depth": q_size, "sample": out})


@router.get("/api/health")
async def health_check():
    """System health check: database, Redis, and Groww API reachability."""
    db_ok = await check_db_health()
    redis_ok = await check_redis_health()

    # Groww health: check if we have a valid session token in Redis
    groww_token = await get_value(GROWW_TOKEN_KEY)
    groww_ok = groww_token is not None and len(groww_token) > 0

    all_ok = db_ok and redis_ok
    return _envelope(
        success=all_ok,
        data={
            "database": "healthy" if db_ok else "unhealthy",
            "redis": "healthy" if redis_ok else "unhealthy",
            "groww_api": "authenticated" if groww_ok else "no_token",
        },
    )
