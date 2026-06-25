"""
NIFTY 50 constituent monitor — periodic drift detection.

Runs monthly (registered in main.py).  Fetches the live official NIFTY 50
constituents and diffs them against the app's coverage registry
(core.nifty50.TRACKED_SYMBOLS) and the previously-seen snapshot.  When a stock
enters or leaves the index — or a demerger/split lands off-cycle, as happened
with TATAMOTORS → TMPV + TMCV — it publishes a system alert listing the exact
symbols to add/remove and the files that hardcode ticker maps, so the operator
can refresh them.

Why monthly rather than strictly quarterly: NSE's *scheduled* index reviews are
semi-annual (changes effective end-March and end-September), but off-cycle
corporate actions (mergers, demergers, delistings, insolvency exclusions) can
land at any time with only a few weeks' notice.  A monthly check costs a single
HTTP request and guarantees we never run more than ~30 days with a stale map.

Safe by design: the job never raises, and a fetch failure is logged + skipped so
it can never disrupt the trading day.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict

import pytz

from core.nifty50 import COVERAGE_LOCATIONS, diff_constituents
from core.redis_client import get_value, publish, set_value
from core.redis_keys import (
    NIFTY50_CONSTITUENTS_KEY,
    NIFTY50_DRIFT_KEY,
    NIFTY50_LAST_CHECK_KEY,
)
from integrations.nse_client import fetch_nifty50_constituents

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


async def check_nifty50_drift(trigger: str = "scheduler") -> Dict[str, Any]:
    """Fetch live NIFTY 50 constituents, diff against coverage + history, alert on drift.

    Args:
        trigger: "scheduler" for the monthly cron run, "manual" for an on-demand
            API call.  Recorded in the drift report for auditability.

    Returns the drift report dict (also persisted to Redis).  On fetch failure
    returns {"available": False, ...} and does not overwrite the last good snapshot.
    """
    now_ist = datetime.now(IST)
    live = await fetch_nifty50_constituents()

    if not live.get("available") or not live.get("symbols"):
        logger.warning(
            "[NIFTY50Monitor] Could not fetch live constituents (trigger=%s) — "
            "keeping previous snapshot, no alert raised", trigger,
        )
        return {"available": False, "checked_at": now_ist.isoformat(), "trigger": trigger}

    live_symbols = live["symbols"]

    # Load the previously-seen snapshot to report membership changes since last run.
    previous_symbols = None
    try:
        raw_prev = await get_value(NIFTY50_CONSTITUENTS_KEY)
        if raw_prev:
            previous_symbols = (json.loads(raw_prev) or {}).get("symbols")
    except Exception as exc:
        logger.debug("[NIFTY50Monitor] Could not load previous snapshot: %s", exc)

    report = diff_constituents(live_symbols, previous_symbols)
    report.update({
        "available": True,
        "checked_at": now_ist.isoformat(),
        "trigger": trigger,
        "live_symbols": live_symbols,
    })

    # Persist the fresh snapshot, the report, and the timestamp.
    try:
        await set_value(NIFTY50_CONSTITUENTS_KEY, json.dumps({
            "symbols": live_symbols,
            "companies": live.get("companies", {}),
            "isins": live.get("isins", {}),
            "fetched_at": now_ist.isoformat(),
        }))
        await set_value(NIFTY50_DRIFT_KEY, json.dumps(report))
        await set_value(NIFTY50_LAST_CHECK_KEY, now_ist.isoformat())
    except Exception as exc:
        logger.warning("[NIFTY50Monitor] Failed to persist snapshot/report: %s", exc)

    # Alert only when there is something actionable.
    if report["has_coverage_gap"] or report["has_membership_change"]:
        lines = [f"NIFTY 50 constituent change detected ({live['count']} live members)."]
        if report["new_entrants_uncovered"]:
            lines.append(
                "NEW entrants with NO app coverage (ADD ticker maps): "
                + ", ".join(report["new_entrants_uncovered"])
            )
        if report["index_removals_since_last"]:
            lines.append("Left the index since last check: "
                         + ", ".join(report["index_removals_since_last"]))
        if report["index_additions_since_last"]:
            lines.append("Entered the index since last check: "
                         + ", ".join(report["index_additions_since_last"]))
        if report["tracked_not_in_index"]:
            lines.append("Tracked symbols no longer in index (review/remove): "
                         + ", ".join(report["tracked_not_in_index"]))
        lines.append("Files to update: " + " | ".join(COVERAGE_LOCATIONS))
        message = "\n".join(lines)

        logger.warning("[NIFTY50Monitor] %s", message)
        await publish("system_alerts", {
            "type": "warning",
            "message": message,
            "timestamp": now_ist.isoformat(),
        })
    else:
        logger.info(
            "[NIFTY50Monitor] No NIFTY 50 changes (%d members, all covered) — trigger=%s",
            live["count"], trigger,
        )

    return report
