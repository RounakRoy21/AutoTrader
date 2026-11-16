"""
Durable, date-scoped daily risk state (audit Part A R7 / Part B C8).

The trading halt, consecutive-loss streak, loss pause and per-stock locks are
stored under IST-date-suffixed Redis keys (see ``core.redis_keys``).  They are
therefore:

  * never reset by a process restart or a same-day session start — nothing
    writes "FALSE" / "0" on startup any more;
  * reset automatically on a new trading day (a new date → new, empty keys);
  * rebuilt from today's DB rows on session start
    (``compute_daily_risk_state``), so a flushed / lost Redis cannot silently
    re-arm trading after a loss streak or a drawdown breach;
  * read FAIL-CLOSED: if the halt flag cannot be read (Redis down), callers
    treat trading as halted for new entries (``is_halted_strict``).

Clearing a halt requires the explicit, API-key-protected
``POST /api/trading/resume`` call (``clear_halt``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, List, Optional, Set

import pytz

from core.redis_client import get_redis, get_value_strict
from core.redis_keys import RISK_STATE_TTL_S, halt_key, halt_reason_key

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


# ── Halt flag ────────────────────────────────────────────────────────────────


async def set_halt(reason: str) -> None:
    """Set today's HALT flag (and its reason).  Never raises; logs CRITICAL on failure.

    A failed write is not silent: if Redis is down, every new-entry gate reads
    the halt flag fail-closed anyway (``is_halted_strict``).
    """
    try:
        r = await get_redis()
        await r.setex(halt_key(), RISK_STATE_TTL_S, "TRUE")
        await r.setex(halt_reason_key(), RISK_STATE_TTL_S, reason or "unspecified")
        logger.warning("Trading HALT set: %s", reason)
    except Exception as exc:
        logger.critical(
            "FAILED to persist trading HALT (%s): %s — entry gates fail closed while "
            "Redis is unreachable", reason, exc,
        )


async def set_halt_strict(reason: str) -> None:
    """Set today's HALT flag and VERIFY it by reading it back.

    Raises on any Redis error or when the read-back is not ``TRUE``.  Used by
    the operator's emergency-halt endpoint, which must never report success
    for a halt that was not persisted (a transient write failure followed by a
    readable Redis would otherwise let trading continue).
    """
    r = await get_redis()
    await r.setex(halt_key(), RISK_STATE_TTL_S, "TRUE")
    await r.setex(halt_reason_key(), RISK_STATE_TTL_S, reason or "unspecified")
    stored = await get_value_strict(halt_key())
    if stored != "TRUE":
        raise RuntimeError(f"halt flag read back as {stored!r}")
    logger.warning("Trading HALT set (verified): %s", reason)


async def clear_halt() -> None:
    """Clear today's HALT flag.  Only the authenticated resume endpoint calls this."""
    r = await get_redis()
    await r.setex(halt_key(), RISK_STATE_TTL_S, "FALSE")
    await r.delete(halt_reason_key())
    logger.warning("Trading HALT cleared (explicit resume)")


async def is_halted_strict() -> bool:
    """True when today's HALT flag is set OR cannot be read (fail closed)."""
    try:
        return (await get_value_strict(halt_key())) == "TRUE"
    except Exception as exc:
        logger.error("Halt flag unreadable (%s) — treating trading as HALTED", exc)
        return True


# ── Rebuild from today's DB rows ─────────────────────────────────────────────


@dataclass
class DailyRiskState:
    """Risk state derived from today's CLOSED trades."""

    streak: int = 0                              # trailing consecutive net losses
    pause_until: Optional[datetime] = None       # set when streak >= threshold
    locked_stocks: Set[str] = field(default_factory=set)
    realized_loss: float = 0.0                   # sum of |net P&L| of losing trades


def compute_daily_risk_state(
    closed_trades: Iterable,
    trade_date: date,
    pause_threshold: int,
    pause_minutes: int,
    lock_after_loss: bool = True,
) -> DailyRiskState:
    """Derive streak / pause / locks / realised loss from today's CLOSED trades.

    Uses the same rules as the live RiskManager (audit R14): the SIGN of the
    net ``realized_pnl`` drives everything — a net loss advances the streak
    and locks the stock, a net win resets the streak, zero is neutral.
    """
    rows: List = [t for t in closed_trades if getattr(t, "realized_pnl", None) is not None]
    rows.sort(key=lambda t: (getattr(t, "exit_time", None) is None, getattr(t, "exit_time", None) or 0))

    state = DailyRiskState()
    last_loss_time = None
    for t in rows:
        pnl = float(t.realized_pnl or 0.0)
        if pnl < 0:
            state.streak += 1
            state.realized_loss += abs(pnl)
            last_loss_time = getattr(t, "exit_time", None) or last_loss_time
            if lock_after_loss:
                state.locked_stocks.add(t.stock)
        elif pnl > 0:
            state.streak = 0

    if state.streak >= pause_threshold and last_loss_time is not None:
        state.pause_until = IST.localize(datetime.combine(trade_date, last_loss_time)) + timedelta(
            minutes=pause_minutes
        )
    state.realized_loss = round(state.realized_loss, 2)
    return state
