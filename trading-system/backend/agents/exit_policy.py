"""
Exit policy shared by the live RiskManager and the backtest (audit V4 / D2).

Pure step functions, evaluated in the RiskManager's poll order:

    stop_hit → partial_due → roi_decay_target → target_hit → trailing_stop
    → square_off_due

The RiskManager performs the side effects (broker exits, DB writes) between
steps; the backtest applies the same steps to a simulated position on each
replayed price.  ``initial_levels`` / ``partial_target`` are the entry-time
stop / target / scale-out levels computed from the actual fill.
"""

from __future__ import annotations

from datetime import datetime, time as dt_time
from typing import Any, Optional, Tuple

import pytz

from core.tick_size import ceil_to_tick, floor_to_tick

IST = pytz.timezone("Asia/Kolkata")

MIS_CLOSE_START = dt_time(15, 0)       # MIS square-off starts
ROI_DECAY_FLOOR_PCT = 0.005            # minimum late-stage decayed target (0.5%)
ROI_DECAY_ATR_FRACTION = 0.4           # late-stage floor = max(0.5%, 0.4 × ATR%)


# ── Entry-time levels ────────────────────────────────────────────────────────

def initial_levels(entry: float, atr: Optional[float], settings: Any, tick: float) -> Tuple[float, float]:
    """(stop, target) from the fill: ATR multiples when ATR is known, else the
    fixed-percentage fallback.  Stop floored, target ceiled to the tick."""
    if atr and atr > 0:
        return (
            floor_to_tick(entry - atr * settings.atr_sl_multiplier, tick),
            ceil_to_tick(entry + atr * settings.atr_target_multiplier, tick),
        )
    return (
        floor_to_tick(entry * (1 - settings.stop_loss_pct), tick),
        ceil_to_tick(entry * (1 + settings.min_target_pct), tick),
    )


def partial_target(entry: float, stop: float, target: float, qty: int, settings: Any) -> Optional[float]:
    """Scale-out level entry + R × trigger, only when enabled, splittable and
    strictly below the full target."""
    risk = entry - stop
    if settings.partial_booking_enabled and qty >= 2 and risk > 0:
        level = round(entry + risk * settings.partial_booking_trigger_r, 2)
        if level < target:
            return level
    return None


# ── Per-price steps ──────────────────────────────────────────────────────────

def stop_hit(pos: Any, ltp: float) -> bool:
    return ltp <= pos.stop_loss_price


def partial_due(pos: Any, ltp: float, settings: Any) -> bool:
    return bool(
        settings.partial_booking_enabled
        and not pos.partial_booked
        and pos.partial_target_price
        and pos.quantity >= 2
        and ltp >= pos.partial_target_price
    )


def partial_split(qty: int, settings: Any) -> Tuple[int, int]:
    """(shares to book, shares left as the runner)."""
    book = max(1, int(qty * settings.partial_booking_fraction))
    return book, qty - book


def breakeven_stop(pos: Any, settings: Any, tick: float) -> float:
    """The runner's stop after a scale-out: raised to entry (never lowered)."""
    if settings.partial_booking_move_sl_to_breakeven and pos.entry_price > pos.stop_loss_price:
        return max(pos.stop_loss_price, floor_to_tick(pos.entry_price, tick))
    return pos.stop_loss_price


def minutes_in_trade(pos: Any, now: datetime) -> float:
    entry_dt = IST.localize(datetime.combine(pos.trade_date, pos.entry_time))
    return (now - entry_dt).total_seconds() / 60


def roi_decay_target(pos: Any, now: datetime, atr: Optional[float], tick: float) -> Optional[float]:
    """Time-decayed target, or None when unchanged.

    > 20 min → 1.5 %, > 35 min → 1.0 %, > 50 min → max(0.5 %, 0.4 × ATR%).
    Never at / below the (possibly trailed) stop and never above the current
    target, so the stop / target corridor cannot invert.
    """
    if not pos.entry_time:
        return None
    elapsed = minutes_in_trade(pos, now)
    entry = pos.entry_price
    if elapsed > 50:
        floor_pct = ROI_DECAY_FLOOR_PCT
        if atr and atr > 0 and entry > 0:
            floor_pct = max(ROI_DECAY_FLOOR_PCT, ROI_DECAY_ATR_FRACTION * atr / entry)
        decayed = ceil_to_tick(entry * (1 + floor_pct), tick)
    elif elapsed > 35:
        decayed = ceil_to_tick(entry * 1.010, tick)
    elif elapsed > 20:
        decayed = ceil_to_tick(entry * 1.015, tick)
    else:
        return None
    if pos.stop_loss_price < decayed < pos.target_price:
        return decayed
    return None


def target_hit(pos: Any, ltp: float) -> bool:
    return ltp >= pos.target_price


def trailing_stop(pos: Any, ltp: float, settings: Any, tick: float) -> Optional[float]:
    """Raised stop once price is past the activation threshold; None when the
    stop would not move up."""
    if ltp >= pos.entry_price * (1 + settings.trailing_sl_activation_pct):
        trailed = floor_to_tick(ltp * (1 - settings.trailing_sl_trail_pct), tick)
        if trailed > pos.stop_loss_price:
            return trailed
    return None


def square_off_due(pos: Any, now_time: dt_time) -> bool:
    return pos.product_type == "MIS" and now_time >= MIS_CLOSE_START
