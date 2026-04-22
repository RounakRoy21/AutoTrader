"""
AutoTrader — FastAPI application entry point.

Central control tower for the automated trading system.
Manages scheduling, inter-agent communication, REST + WebSocket endpoints.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import get_settings
from core.database import check_db_health, dispose_engine, get_engine, Base
from core.redis_client import check_redis_health, close_redis, get_redis
from core.scheduler import (
    schedule_cron,
    shutdown_scheduler,
    start_scheduler,
)

# ── Route imports ──────────────────────────────────
from api.routes.market_brief import router as market_brief_router
from api.routes.trades import router as trades_router
from api.routes.pnl import router as pnl_router
from api.routes.system import router as system_router
from api.routes.auth import router as auth_router
from api.websocket import router as ws_router, start_ws_background_tasks

# ── Agent imports ──────────────────────────────────
from agents.research_agent import run_research_agent
from agents.trading_agent_manager import get_trading_agent_manager
from integrations.instrument_service import load_instrument_map

# ─────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────
settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("autotrader")


# ─────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for the FastAPI application."""
    logger.info("═══ AutoTrader starting up ═══")

    # 1. Test database connectivity
    db_ok = await check_db_health()
    if db_ok:
        logger.info("✅ Database connected")
    else:
        logger.error("❌ Database unreachable — some features will be degraded")

    # 2. Test Redis connectivity
    try:
        await get_redis()
        logger.info("✅ Redis connected")
    except Exception:
        logger.warning("⚠️ Redis unreachable — running in fallback mode")

    # 3. Load instrument token map (Groww API → Redis → hardcoded fallback)
    try:
        token_map = await load_instrument_map()
        logger.info("✅ Instrument map loaded (%d symbols)", len(token_map))
    except Exception as exc:
        logger.warning("⚠️ Instrument map load failed: %s — using fallback", exc)

    # 4. Schedule Research Agent at 6:00 AM IST (Mon-Fri)
    schedule_cron(
        func=run_research_agent,
        job_id="research_agent",
        hour=6,
        minute=0,
    )

    # 5. (Groww TOTP tokens do not expire — no daily token refresh job needed)

    # 6. Trading Agent — auto-start at 09:15 IST, auto-stop at 15:30 IST (Mon-Fri)
    trading_manager = get_trading_agent_manager()
    schedule_cron(
        func=trading_manager.start_session,
        job_id="trading_agent_start",
        hour=9,
        minute=15,
    )
    schedule_cron(
        func=trading_manager.stop_session,
        job_id="trading_agent_stop",
        hour=15,
        minute=30,
    )

    start_scheduler()
    logger.info("✅ Scheduler started")

    # 7. Start WebSocket background tasks (Redis relay + LTP broadcaster)
    #    These must start after the event loop is running, hence here not at import time.
    start_ws_background_tasks()

    # Expose manager on app.state so API routes can reach it
    app.state.trading_manager = trading_manager

    logger.info("═══ AutoTrader ready ═══ (paper_trading=%s)", settings.paper_trading)

    yield

    # ── Shutdown ──────────────────────────────────
    logger.info("═══ AutoTrader shutting down ═══")

    # Stop the trading session first (closes open WebSocket, joins threads)
    if trading_manager.is_running:
        logger.info("Stopping active trading session…")
        await trading_manager.stop_session()

    shutdown_scheduler()
    await close_redis()
    await dispose_engine()
    logger.info("═══ Shutdown complete ═══")


# ─────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────
app = FastAPI(
    title="AutoTrader",
    description="Fully automated stock trading system for the Indian equity market (NSE)",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────
# CORS origins: configurable via CORS_ORIGINS env var (comma-separated)
_cors_origins = [
    o.strip() for o in getattr(settings, "cors_origins", "http://localhost:4200,http://localhost:4201").split(",")
    if o.strip()
] or ["http://localhost:4200", "http://localhost:4201"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Register routers ─────────────────────────────
app.include_router(market_brief_router)
app.include_router(trades_router)
app.include_router(pnl_router)
app.include_router(system_router)
app.include_router(auth_router)
app.include_router(ws_router)
