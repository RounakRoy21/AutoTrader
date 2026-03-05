"""
APScheduler setup using AsyncIOScheduler.
All scheduled jobs (Research Agent, Risk Manager polls) are registered here.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

_scheduler: Optional[AsyncIOScheduler] = None


def get_scheduler() -> AsyncIOScheduler:
    """Return the singleton scheduler instance, creating it if necessary."""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=IST)
        logger.info("APScheduler initialised with IST timezone")
    return _scheduler


def schedule_cron(
    func: Callable,
    job_id: str,
    hour: int,
    minute: int = 0,
    day_of_week: str = "mon-fri",
    **kwargs,
) -> None:
    """Register a cron-triggered job (e.g. Research Agent at 06:00 IST)."""
    scheduler = get_scheduler()
    trigger = CronTrigger(
        hour=hour,
        minute=minute,
        day_of_week=day_of_week,
        timezone=IST,
    )
    scheduler.add_job(func, trigger, id=job_id, replace_existing=True, **kwargs)
    logger.info(
        "Scheduled cron job '%s' at %02d:%02d IST (%s)",
        job_id, hour, minute, day_of_week,
    )


def schedule_interval(
    func: Callable,
    job_id: str,
    seconds: int = 5,
    **kwargs,
) -> None:
    """Register an interval-triggered job (e.g. Risk Manager every 5s)."""
    scheduler = get_scheduler()
    trigger = IntervalTrigger(seconds=seconds, timezone=IST)
    scheduler.add_job(func, trigger, id=job_id, replace_existing=True, **kwargs)
    logger.info("Scheduled interval job '%s' every %ds", job_id, seconds)


def start_scheduler() -> None:
    """Start the scheduler if it is not already running."""
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started")


def shutdown_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler shut down")
        _scheduler = None
