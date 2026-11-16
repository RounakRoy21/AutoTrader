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
from datetime import datetime, timedelta
from typing import Optional

import pytz

from core.nse_calendar import ist_today

_IST = pytz.timezone("Asia/Kolkata")


def _secs_until_midnight_ist() -> int:
    """Seconds from now until the next midnight IST (minimum 60)."""
    now = datetime.now(_IST)
    tomorrow_midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(60, int((tomorrow_midnight - now).total_seconds()))

from sqlalchemy import func, select

from core.database import get_db_context
from core.redis_client import publish, set_value
from core.redis_keys import DAILY_TRADE_COUNT_KEY, RISK_STATUS_KEY, TRADING_STATUS_KEY
from core.nse_calendar import is_nse_holiday
from models.trade import Trade

logger = logging.getLogger(__name__)

TRADE_COUNT_KEY = DAILY_TRADE_COUNT_KEY


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


async def _get_redis_trading_status() -> str:
    """Read the current trading status from Redis. Returns 'INACTIVE' on any error."""
    try:
        from core.redis_client import get_value
        val = await get_value(TRADING_STATUS_KEY)
        return val or "INACTIVE"
    except Exception:
        return "INACTIVE"


async def _clear_trading_status_in_redis(exc: Optional[Exception]) -> None:
    """Set agent:trading:status to INACTIVE in Redis. Called after task ends."""
    try:
        from datetime import datetime, timezone
        await set_value(TRADING_STATUS_KEY, "INACTIVE")
        if exc:
            await publish("system_alerts", {
                "type": "critical",
                "message": f"Trading agent task ended unexpectedly: {exc}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        logger.info("[Manager] Redis trading status set to INACTIVE after task end")
    except Exception as e:
        logger.error("[Manager] Failed to clear Redis trading status: %s", e)

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

    async def start_session(self, source: str = "scheduler") -> str:
        """
        Start a new trading session.
        Idempotent — returns 'already_running' if a session is active.
        NSE holiday guard: returns 'nse_holiday' without starting if today is a non-trading day.

        *source* describes what triggered the start and is included in the system alert:
          - 'scheduler'  — 09:15 IST APScheduler cron job (normal market open)
          - 'startup'    — backend restarted during market hours (catch-up auto-start)
          - 'manual'     — user pressed Start Agent in the dashboard / called the API
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

        _start_messages = {
            "scheduler": "Trading session started (09:15 auto-scheduler)",
            "startup":   "Trading session started (backend restarted during market hours)",
            "manual":    "Trading session started manually via dashboard",
        }
        await publish("system_alerts", {
            "type": "info",
            "message": _start_messages.get(source, f"Trading session started ({source})"),
            "timestamp": _now_iso(),
        })
        logger.info("[Manager] Trading session started ✅ (source=%s)", source)
        return "started"

    async def stop_session(self, source: str = "scheduler") -> str:
        """
        Stop the current trading session gracefully.
        Returns 'stopped', 'not_running', or 'stale_cleared'.

        *source* describes what triggered the stop and is included in the system alert:
          - 'scheduler'  — 15:30 IST APScheduler cron job (normal market close)
          - 'manual'     — user pressed Stop Agent in the dashboard / called the API
        """
        if not self.is_running or self._agent is None:
            # No live task — but Redis may still say ACTIVE from a previous crash or
            # from a session that ended before stop() was called (e.g. scanner crash).
            # Always force-clear both statuses so the dashboard reflects reality,
            # regardless of whether this is a manual or scheduled stop.
            current = await _get_redis_trading_status()
            if current == "ACTIVE":
                await set_value(TRADING_STATUS_KEY, "INACTIVE")
                await set_value(RISK_STATUS_KEY, "INACTIVE")
                msg = "Stale ACTIVE status cleared — trading session had already ended (backend may have restarted or scanner crashed)"
                await publish("system_alerts", {
                    "type": "warning",
                    "message": msg,
                    "timestamp": _now_iso(),
                })
                logger.warning("[Manager] Cleared stale ACTIVE status from Redis (no live task, source=%s)", source)
                return "stale_cleared"
            logger.info("[Manager] No active trading session — ignoring stop request (source=%s)", source)
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

        _stop_messages = {
            "scheduler": "Trading session stopped (15:30 auto-scheduler)",
            "manual":    "Trading session stopped manually via dashboard",
        }
        await publish("system_alerts", {
            "type": "info",
            "message": _stop_messages.get(source, f"Trading session stopped ({source})"),
            "timestamp": _now_iso(),
        })
        logger.info("[Manager] Trading session stopped ✅ (source=%s)", source)
        return "stopped"

    # ── Internal helpers ──────────────────────────────────────────────

    def _on_session_ended(self, task: asyncio.Task) -> None:
        """
        Callback fired when the agent Task finishes — normally, via exception,
        or cancelled.  Clears internal state so the next day's start works.
        Also schedules an async Redis cleanup so the dashboard status reflects
        reality when the task ends without going through stop_session().
        """
        exc = task.exception() if not task.cancelled() else None
        self._agent = None
        self._task = None

        if exc:
            logger.error("[Manager] Trading session ended with error: %s", exc)
        else:
            logger.info("[Manager] Trading session ended cleanly")

        # Ensure Redis status is set to INACTIVE regardless of how the task ended.
        # TradingAgent.stop() normally handles this, but if the task crashed or was
        # cancelled before reaching stop(), Redis is left stale at ACTIVE.
        # asyncio.ensure_future schedules the coroutine on the running event loop
        # from this sync callback.
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(_clear_trading_status_in_redis(exc))
        except RuntimeError:
            pass  # No event loop available at teardown — best effort only

    async def _restore_trade_count(self) -> None:
        """
        Read today's executed trade count from the database and write it
        into Redis.  This ensures a mid-day restart doesn't reset the counter,
        which would allow more trades than the daily maximum permits.

        The halt flag / loss streak / pause / locks are date-scoped and are
        never reset here (audit R7 / C8) — see ``_restore_risk_state``.
        """
        try:
            today = ist_today()
            async with get_db_context() as session:
                result = await session.execute(
                    select(func.count()).select_from(Trade).where(
                        Trade.trade_date == today,
                        Trade.status != "REJECTED",
                    )
                )
                count = result.scalar() or 0
            _ttl = _secs_until_midnight_ist()
            await set_value(TRADE_COUNT_KEY, str(count), ttl=_ttl)
            logger.info(
                "[Manager] Restored daily_trade_count = %d from DB (TTL %ds until midnight IST)",
                count, _ttl,
            )
        except Exception as exc:
            logger.error("[Manager] Failed to restore trade count from DB: %s — defaulting to 0", exc)
            await set_value(TRADE_COUNT_KEY, "0", ttl=_secs_until_midnight_ist())

        # Audit R7 / C8: NEVER reset the halt flag, loss streak, pause or stock
        # locks here.  They are date-scoped (see core.redis_keys), so a new
        # trading day starts clean on its own while a restart / same-day
        # session start keeps today's state.  Rebuild it from today's DB rows
        # in case Redis lost it.
        await self._restore_risk_state()

    async def _restore_risk_state(self) -> None:
        """Re-derive today's loss streak, pause, stock locks and drawdown halt
        from today's CLOSED trades (audit R7 / C8).

        Only ever makes the state STRICTER: a halt is set, never cleared; the
        streak / pause / locks are (re)written from the DB, which is the source
        of truth for realised outcomes.  On a DB failure the session still
        starts, but new entries are halted until an operator resumes.
        """
        from core.risk_state import compute_daily_risk_state, set_halt  # noqa: PLC0415
        from core.config import get_settings  # noqa: PLC0415
        from core.redis_keys import (  # noqa: PLC0415
            RISK_STATE_TTL_S, consecutive_losses_key, loss_pause_until_key, stock_lock_key,
        )

        settings = get_settings()
        today = ist_today()
        try:
            async with get_db_context() as session:
                result = await session.execute(
                    select(Trade).where(Trade.trade_date == today, Trade.status == "CLOSED")
                )
                closed = list(result.scalars().all())
        except Exception as exc:
            logger.error("[Manager] Risk-state rebuild failed (DB): %s — halting new entries", exc)
            await set_halt(f"risk-state rebuild failed on session start: {exc}")
            return

        state = compute_daily_risk_state(
            closed,
            today,
            pause_threshold=settings.consecutive_loss_pause_threshold,
            pause_minutes=settings.consecutive_loss_pause_minutes,
            lock_after_loss=settings.stock_lock_after_sl,
        )

        await set_value(consecutive_losses_key(today), str(state.streak), ttl=RISK_STATE_TTL_S)
        if state.pause_until is not None and state.pause_until > datetime.now(_IST):
            await set_value(
                loss_pause_until_key(today), state.pause_until.isoformat(), ttl=RISK_STATE_TTL_S,
            )
        for sym in state.locked_stocks:
            await set_value(stock_lock_key(sym, today), "TRUE", ttl=RISK_STATE_TTL_S)

        limit = settings.daily_drawdown_limit
        if limit > 0 and state.realized_loss >= limit:
            await set_halt(
                f"daily drawdown ₹{state.realized_loss:.2f} >= ₹{limit:.2f} (rebuilt on session start)"
            )
        logger.info(
            "[Manager] Restored risk state from DB: streak=%d pause_until=%s locks=%s realised_loss=₹%.2f",
            state.streak, state.pause_until, sorted(state.locked_stocks), state.realized_loss,
        )


# ── Module-level singleton ────────────────────────────────────────────

_manager: Optional[TradingAgentManager] = None


def get_trading_agent_manager() -> TradingAgentManager:
    """Return the process-wide TradingAgentManager singleton."""
    global _manager
    if _manager is None:
        _manager = TradingAgentManager()
    return _manager
