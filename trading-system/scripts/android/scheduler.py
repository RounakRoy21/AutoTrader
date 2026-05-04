#!/usr/bin/env python3
"""
AutoTrader daily scheduler — runs persistently inside tmux.

Schedule (IST):
  05:30  Mon–Fri  →  run startup.sh  (unless NSE holiday)
  16:00  Mon–Fri  →  run shutdown.sh (unless NSE holiday)

The scheduler sleeps precisely until the next trigger, so it has
zero CPU overhead between events.  On a non-trading day it wakes,
logs the skip, then sleeps until the next trigger without calling
any scripts.

Usage:
    python3 /root/autotrader/trading-system/scripts/android/scheduler.py
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# ── Bootstrap: make the backend importable ───────────────────────────────────
_REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "backend")
)
sys.path.insert(0, _REPO_ROOT)

try:
    from core.nse_calendar import is_nse_holiday  # type: ignore
except ImportError:
    # Graceful fallback if the backend venv isn't active — weekends only
    def is_nse_holiday(d: date | None = None) -> bool:  # type: ignore[misc]
        return False

# ── Config ────────────────────────────────────────────────────────────────────
IST = ZoneInfo("Asia/Kolkata")
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))

STARTUP_TIME  = (5, 30)   # HH, MM
SHUTDOWN_TIME = (16, 0)   # HH, MM

LOG_FILE = "/root/autotrader/logs/scheduler.log"

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger("scheduler")


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_trading_day(d: date) -> bool:
    """True if `d` is a weekday that is not an NSE holiday."""
    if d.weekday() >= 5:  # Sat=5, Sun=6
        return False
    return not is_nse_holiday(d)


def next_event() -> tuple[datetime, str, date]:
    """Return (trigger_datetime_IST, action, event_date) for the next event."""
    now = datetime.now(IST)
    schedule = [
        (STARTUP_TIME,  "startup"),
        (SHUTDOWN_TIME, "shutdown"),
    ]

    # Scan up to 8 days ahead (covers any long weekend / holiday run)
    for day_offset in range(8):
        candidate_date = (now + timedelta(days=day_offset)).date()
        for (hh, mm), action in schedule:
            trigger = datetime(
                candidate_date.year, candidate_date.month, candidate_date.day,
                hh, mm, 0,
                tzinfo=IST,
            )
            if trigger > now:
                return trigger, action, candidate_date

    raise RuntimeError("Could not find a next event within 8 days — check system clock")


def _seconds_until(target: datetime) -> float:
    return max(0.0, (target - datetime.now(IST)).total_seconds())


def run_script(name: str) -> None:
    script_path = os.path.join(SCRIPTS_DIR, f"{name}.sh")
    log.info("Running %s", script_path)
    try:
        result = subprocess.run(
            ["bash", script_path],
            timeout=300,  # 5-minute max — if startup takes longer, something is wrong
        )
        if result.returncode == 0:
            log.info("%s completed (exit 0)", name)
        else:
            log.error("%s exited with code %d", name, result.returncode)
    except subprocess.TimeoutExpired:
        log.error("%s timed out after 300s", name)
    except Exception as exc:
        log.error("%s failed: %s", name, exc)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 50)
    log.info("AutoTrader scheduler starting")
    log.info("Startup:  %02d:%02d IST on weekday trading days", *STARTUP_TIME)
    log.info("Shutdown: %02d:%02d IST on weekday trading days", *SHUTDOWN_TIME)
    log.info("=" * 50)

    while True:
        trigger_dt, action, event_date = next_event()
        secs = _seconds_until(trigger_dt)

        log.info(
            "Next: %-10s on %s at %s IST  (%.0f s away)",
            action,
            event_date.strftime("%a %Y-%m-%d"),
            trigger_dt.strftime("%H:%M"),
            secs,
        )

        # Sleep in 60-second chunks so log output stays alive and
        # the process is easy to interrupt with Ctrl-C.
        while True:
            remaining = _seconds_until(trigger_dt)
            if remaining <= 0:
                break
            chunk = min(60.0, remaining)
            time.sleep(chunk)

        # We woke up — check if it is still the right day
        # (phone was rebooted or time jumped)
        now_date = datetime.now(IST).date()
        if now_date != event_date:
            log.warning(
                "Date mismatch after sleep (expected %s, now %s) — re-computing",
                event_date, now_date,
            )
            continue

        if not is_trading_day(event_date):
            day_name = event_date.strftime("%a %Y-%m-%d")
            log.info("Skipping %s: %s is not a trading day (weekend or NSE holiday)", action, day_name)
            # Small buffer so next_event() doesn't return the same trigger
            time.sleep(61)
            continue

        log.info("Firing: %s", action)
        run_script(action)

        # Small buffer before re-computing next event
        time.sleep(61)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Scheduler stopped by user")
        sys.exit(0)
