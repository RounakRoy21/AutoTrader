"""
TradingAgentManager — Singleton lifecycle controller for TradingAgent.

Responsible for:
  • Starting / stopping the TradingAgent as an asyncio background Task
  • Restoring today's DB trade count into Redis on session start
    (avoids resetting the counter if the process restarts mid-day)
  • Idempotent start/stop (safe to call when already in desired state)

Called by:
  • APScheduler  — session start at 09:15 IST, stop at 15:30 IST (Mon-Fri)
  • REST API      — POST /api/agent/trading/start  and  /api/agent/trading/stop
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Optional

from sqlalchemy import func, select

from core.database import get_db_context
from core.redis_client import publish, set_value
from core.redis_keys import DAILY_TRADE_COUNT_KEY, HALT_KEY
from core.nse_calendar import is_nse_holiday
from models.trade import Trade

logger = logging.getLogger(__name__)

TRADE_COUNT_KEY = DAILY_TRADE_COUNT_KEY

class TradingAgentManager:
    """
    Single owner of the TradingAgent lifecycle.
    Thread-safe for reads; start/stop must be called from the event loop.
    """

    def __init__(self) -> None:
        self._agent: Optional["TradingAgent"] = None  # type: ignore[name-defined]
        self._task: Optional[asyncio.Task] = None

    # ── Public interface ──────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """True when a trading session task is alive."""
        return self._task is not None and not self._task.done()

    @property
    def agent(self):
        """Expose the inner TradingAgent (for status queries, etc.)."""
        return self._agent

    async def start_session(self) -> str:
        """
        Start a new trading session.
        Idempotent — returns 'already_running' if a session is active.
        NSE holiday guard: returns 'nse_holiday' without starting if today is a non-trading day.
        """
        if is_nse_holiday():
            logger.info("[Manager] Today is an NSE holiday — trading session will not start")
            return "nse_holiday"

        if self.is_running:
            logger.info("[Manager] Trading session already active — ignoring start request")
            return "already_running"

        # Late import prevents circular dependency at module load time
        from agents.trading_agent import TradingAgent  # noqa: PLC0415

        # Sync today's trade count from DB before agent initialises
        await self._restore_trade_count()

        self._agent = TradingAgent()
        self._task = asyncio.create_task(
            self._agent.start(),
            name="trading_agent_session",
        )
        self._task.add_done_callback(self._on_session_ended)

        await publish("system_alerts", {
            "type": "info",
            "message": "Trading session started (09:15 scheduler)",
            "timestamp": _now_iso(),
        })
        logger.info("[Manager] Trading session started ✅")
        return "started"

    async def stop_session(self) -> str:
        """
        Stop the current trading session gracefully.
        Returns 'stopped' or 'not_running'.
        """
        if not self.is_running or self._agent is None:
            logger.info("[Manager] No active trading session — ignoring stop request")
            return "not_running"

        await self._agent.stop()

        # Give the task up to 15 s to wind down cleanly
        if self._task is not None and not self._task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=15.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

        self._agent = None
        self._task = None

        await publish("system_alerts", {
            "type": "info",
            "message": "Trading session stopped (15:30 scheduler)",
            "timestamp": _now_iso(),
        })
        logger.info("[Manager] Trading session stopped ✅")
        return "stopped"

    # ── Internal helpers ──────────────────────────────────────────────

    def _on_session_ended(self, task: asyncio.Task) -> None:
        """
        Callback fired when the agent Task finishes — normally, via exception,
        or cancelled.  Clears internal state so the next day's start works.
        """
        exc = task.exception() if not task.cancelled() else None
        self._agent = None
        self._task = None

        if exc:
            logger.error("[Manager] Trading session ended with error: %s", exc)
        else:
            logger.info("[Manager] Trading session ended cleanly")

    async def _restore_trade_count(self) -> None:
        """
        Read today's executed trade count from the database and write it
        into Redis.  This ensures a mid-day restart doesn't reset the counter,
        which would allow more trades than the daily maximum permits.

        Also resets the trading halt flag — a drawdown halt applies to a single
        trading day and must not carry over into the next session.
        """
        try:
            today = date.today()
            async with get_db_context() as session:
                result = await session.execute(
                    select(func.count()).select_from(Trade).where(
                        Trade.trade_date == today
                    )
                )
                count = result.scalar() or 0
            await set_value(TRADE_COUNT_KEY, str(count))
            logger.info("[Manager] Restored daily_trade_count = %d from DB", count)
        except Exception as exc:
            logger.error("[Manager] Failed to restore trade count from DB: %s — defaulting to 0", exc)
            await set_value(TRADE_COUNT_KEY, "0")

        # Reset the daily halt flag so yesterday's drawdown halt doesn't block today
        await set_value(HALT_KEY, "FALSE")
        logger.info("[Manager] Reset trading halt flag for new session")

        # Reset consecutive loss counter — yesterday's streak should not penalise today
        await set_value("consecutive_losses", "0")
        await set_value("consecutive_loss_pause_until", "")
        logger.info("[Manager] Reset consecutive loss counter for new session")


# ── Module-level singleton ────────────────────────────────────────────

_manager: Optional[TradingAgentManager] = None


def get_trading_agent_manager() -> TradingAgentManager:
    """Return the process-wide TradingAgentManager singleton."""
    global _manager
    if _manager is None:
        _manager = TradingAgentManager()
    return _manager


# ── Small helper ──────────────────────────────────────────────────────

def _now_iso() -> str:
    import pytz
    from datetime import datetime
    return datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
