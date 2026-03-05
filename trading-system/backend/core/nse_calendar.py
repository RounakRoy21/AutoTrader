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

from datetime import date

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
        d = date.today()
    year_holidays = _CALENDAR.get(d.year, frozenset())
    return d in year_holidays
