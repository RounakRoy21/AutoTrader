"""
NSE trading calendar — exchange-declared holidays for the current year.

Source: https://www.nseindia.com/resources/exchange-communication-holidays

The scheduler fires on every weekday (mon-fri) but NSE is closed on certain
public and exchange holidays.  Both the Research Agent and the Trading Agent
must call ``is_nse_holiday()`` before starting any real work so that no
orders are attempted or market data is fetched on non-trading days.

Update this file each December with the official holiday list for the next year.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")


def ist_today() -> date:
    """Return the current date in IST (UTC+5:30).

    All trading logic must use this instead of ``date.today()`` because the
    Docker container runs in UTC.  At 18:30 UTC (00:00 IST the next day) the
    UTC date would already be tomorrow while Indian markets have not yet opened.
    """
    return datetime.now(tz=_IST).date()

# ── NSE Equity Market Holidays 2025 ───────────────────────────────────────────
# Source: NSE circular NSCCL/ITP/F&O/2024-25/0166
NSE_HOLIDAYS_2025: frozenset[date] = frozenset({
    date(2025, 2, 26),   # Mahashivratri
    date(2025, 3, 14),   # Holi
    date(2025, 3, 31),   # Id-Ul-Fitr (Ramzan Eid) — subject to moon sighting
    date(2025, 4, 10),   # Shri Ram Navami
    date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti / Good Friday
    date(2025, 4, 18),   # Good Friday (BSE holiday — NSE may differ; keep for safety)
    date(2025, 5, 1),    # Maharashtra Day
    date(2025, 8, 15),   # Independence Day
    date(2025, 8, 27),   # Ganesh Chaturthi
    date(2025, 10, 2),   # Mahatma Gandhi Jayanti
    date(2025, 10, 2),   # Dussehra (tentative — confirm closer to date)
    date(2025, 10, 24),  # Diwali — Laxmi Puja (Muhurat trading day — markets open briefly; treat as holiday for automated trading)
    date(2025, 10, 27),  # Diwali Balipratipada
    date(2025, 11, 5),   # Prakash Gurpurb Sri Guru Nanak Dev Ji
    date(2025, 12, 25),  # Christmas
})

# ── NSE Equity Market Holidays 2026 ───────────────────────────────────────────
# Placeholder — update with official list when NSE publishes it (typically Nov–Dec 2025)
NSE_HOLIDAYS_2026: frozenset[date] = frozenset()

_CALENDAR: dict[int, frozenset[date]] = {
    2025: NSE_HOLIDAYS_2025,
    2026: NSE_HOLIDAYS_2026,
}


def is_nse_holiday(d: date | None = None) -> bool:
    """Return True if *d* (defaults to today) is an NSE exchange holiday.

    Weekends are NOT checked here — the scheduler's ``day_of_week='mon-fri'``
    already filters those out.  This function only catches the extra exchange
    holidays that fall on weekdays.
    """
    if d is None:
        d = ist_today()
    year_holidays = _CALENDAR.get(d.year, frozenset())
    return d in year_holidays


# ── Market Session Status ─────────────────────────────────────────────────────
_MARKET_OPEN  = time(9, 15)
_MARKET_CLOSE = time(15, 30)
_PRE_OPEN_START = time(9, 0)

# Status literals
_STATUS_OPEN     = "OPEN"
_STATUS_PRE_OPEN = "PRE_OPEN"
_STATUS_CLOSED   = "CLOSED"
_STATUS_HOLIDAY  = "HOLIDAY"
_STATUS_WEEKEND  = "WEEKEND"


def get_market_status() -> dict:
    """Return the current NSE market session status based on IST clock + holiday calendar.

    Returns a dict:
        {
            "status":  "OPEN" | "PRE_OPEN" | "CLOSED" | "HOLIDAY" | "WEEKEND",
            "is_open": bool,   # True only during the continuous trading session
            "label":   str,    # Human-readable label for the dashboard
        }

    Session times (IST):
        09:00 – 09:15  Pre-Open (call auction)
        09:15 – 15:30  Continuous trading (OPEN)
        otherwise      Closed
    """
    now_ist = datetime.now(tz=_IST)
    today = now_ist.date()
    t = now_ist.time()

    if today.weekday() >= 5:  # Saturday=5, Sunday=6
        return {"status": _STATUS_WEEKEND, "is_open": False, "label": "Weekend"}

    if is_nse_holiday(today):
        return {"status": _STATUS_HOLIDAY, "is_open": False, "label": "NSE Holiday"}

    if _PRE_OPEN_START <= t < _MARKET_OPEN:
        return {"status": _STATUS_PRE_OPEN, "is_open": False, "label": "Pre-Open"}

    if _MARKET_OPEN <= t <= _MARKET_CLOSE:
        return {"status": _STATUS_OPEN, "is_open": True, "label": "Market Live"}

    return {"status": _STATUS_CLOSED, "is_open": False, "label": "Market Closed"}
