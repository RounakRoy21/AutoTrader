"""
Tick-size snapping for every price that is sent to the broker (audit R10 / C6).

NSE rejects order prices that are not an exact multiple of the instrument's
tick size.  The rest of the backend computes SL / target prices with
``round(x, 2)`` (paise precision) which is NOT tick-aligned, e.g. an ATR-derived
stop of ₹1423.37 is rejected by the exchange.

Rules (long-only intraday system):
  * stop-loss trigger and SL limit price → floored (``floor_to_tick``)
  * target price                         → ceiled  (``ceil_to_tick``)
  * any other limit price                → caller chooses the conservative side

The per-instrument tick size is taken from ``instrument_service`` when it was
loaded from the Groww instrument CSV; otherwise ``DEFAULT_TICK_SIZE`` (₹0.05)
is used.  Arithmetic is done in ``Decimal`` so 0.05 multiples are exact.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Optional

DEFAULT_TICK_SIZE: float = 0.05

# Sanity bounds for a tick size coming from the instrument CSV, in rupees.
# Anything outside this range is treated as unknown (e.g. a CSV that expresses
# ticks in paise would otherwise snap prices to whole ₹5 steps).
_MIN_VALID_TICK = 0.001
_MAX_VALID_TICK = 1.0


def _valid_tick(tick: Optional[float]) -> Optional[float]:
    try:
        t = float(tick)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if _MIN_VALID_TICK <= t <= _MAX_VALID_TICK:
        return t
    return None


def get_tick_size(symbol: Optional[str] = None) -> float:
    """Return the tick size for *symbol* (instrument CSV value or ₹0.05 default)."""
    if symbol:
        try:
            from integrations.instrument_service import get_tick_size as _instr_tick

            t = _valid_tick(_instr_tick(symbol))
            if t is not None:
                return t
        except Exception:
            pass
    return DEFAULT_TICK_SIZE


def _snap(price: float, tick: Optional[float], rounding: str) -> float:
    t = _valid_tick(tick) or DEFAULT_TICK_SIZE
    # Round to 6 dp first so float noise (100.04999999999) does not drop a tick.
    d = Decimal(str(round(float(price), 6)))
    step = Decimal(str(t))
    snapped = (d / step).to_integral_value(rounding=rounding) * step
    return float(snapped)


def floor_to_tick(price: float, tick: Optional[float] = None) -> float:
    """Largest tick multiple <= price (used for SL trigger / SL limit)."""
    return _snap(price, tick, ROUND_FLOOR)


def ceil_to_tick(price: float, tick: Optional[float] = None) -> float:
    """Smallest tick multiple >= price (used for targets)."""
    return _snap(price, tick, ROUND_CEILING)


def round_to_tick(price: float, tick: Optional[float] = None) -> float:
    """Nearest tick multiple (half-up)."""
    return _snap(price, tick, ROUND_HALF_UP)


def sl_limit_price(trigger: float, tick: Optional[float] = None, buffer_pct: float = 0.005) -> float:
    """Limit price for a SELL stop-loss-limit order (audit R12).

    The limit is placed ``buffer_pct`` below the (already tick-aligned) trigger
    and floored to the tick, and is always at least one tick below the trigger
    so that a fast move through the trigger still fills.
    """
    t = _valid_tick(tick) or DEFAULT_TICK_SIZE
    trig = floor_to_tick(trigger, t)
    limit = floor_to_tick(trig * (1 - max(buffer_pct, 0.0)), t)
    if limit >= trig:
        limit = floor_to_tick(trig - t, t)
    if limit <= 0:
        limit = t
    return limit
