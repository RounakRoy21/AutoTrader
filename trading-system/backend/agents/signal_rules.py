"""
Entry-signal rules shared by the live Scanner and the backtest (audit V1 / D4).

The backtest used to carry a hand copy of ``Scanner._check_signal`` with
"MUST stay in sync" comments.  Both now call these functions, so a threshold
or gate change applies to live and replay at once.  The clock (``now``), the
market bias and the NIFTY state are passed in; nothing here reads the wall
clock, Redis or the network.

Live-only data-quality gates (the OHLCV freshness gate) stay in the Scanner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

SIGNAL_COOLDOWN_SECS = 1800       # suppress repeat signals for the same stock
SIGNAL_CUTOFF = (15, 15)          # no new signals at / after 15:15 IST
MIN_TICKS_FOR_SIGNAL = 50         # ticks before tick-level indicators are meaningful
MIN_CANDLES_FOR_INDICATORS = 15   # completed 1m candles before candle indicators
MIN_CANDLES_FOR_TREND = 26        # EMA-cross + MACD gates (MACD minimum)
HTF_RSI_BAND = (45.0, 72.0)       # 5-minute RSI band
EARLY_SESSION_RSI_TIGHTEN = 5.0   # band narrowed by this before 09:45
EMA21_TREND_FLOOR = 0.99          # reject when price is >1% below EMA(21)


@dataclass(frozen=True)
class BiasThresholds:
    rsi_lo: float
    rsi_hi: float
    vol_min: float
    gap_max_pct: float
    nifty_drift_min: float


def bias_thresholds(bias: str, settings: Any) -> BiasThresholds:
    """Bias-modulated gate thresholds (BULLISH loosens, BEARISH tightens)."""
    neutral = BiasThresholds(
        rsi_lo=50.0, rsi_hi=65.0, vol_min=1.5,
        gap_max_pct=settings.gap_filter_pct,
        nifty_drift_min=settings.nifty_trend_filter_pct,
    )
    return {
        "BULLISH": BiasThresholds(45.0, 68.0, 1.3, 2.0, -0.008),
        "NEUTRAL": neutral,
        "BEARISH": BiasThresholds(50.0, 63.0, 2.0, 1.0, -0.003),
    }.get((bias or "NEUTRAL").upper(), neutral)


def completed_candles(store: Any) -> int:
    df = store._candles_1m.completed_df
    return len(df) if df is not None else 0


def pre_gate(store: Any, now: datetime, last_signal_at: Optional[datetime]) -> Optional[str]:
    """Clock / warm-up / cooldown gates.  Returns a reject reason or None."""
    if (now.hour, now.minute) >= SIGNAL_CUTOFF:
        return "CUTOFF"
    if now.weekday() == 4 and now.hour >= 14:
        return "FRIDAY_AFTERNOON"
    if len(store.ticks) < MIN_TICKS_FOR_SIGNAL:
        return "WARMUP_TICKS"
    if completed_candles(store) < MIN_CANDLES_FOR_INDICATORS:
        return "WARMUP_CANDLES"
    if last_signal_at is not None and (now - last_signal_at).total_seconds() < SIGNAL_COOLDOWN_SECS:
        return "COOLDOWN"
    return None


@dataclass
class EntryEvaluation:
    passed: bool = False
    reason: Optional[str] = None
    # None when no NIFTY data; else whether the NIFTY trend filter suppressed longs.
    nifty_suppressed: Optional[bool] = None
    nifty_drift: Optional[float] = None
    n_candles: int = 0
    ltp: float = 0.0
    vwap: float = 0.0
    rsi: float = 0.0
    vol_ratio: float = 0.0
    ema_9: float = 0.0
    ema_21: float = 0.0
    macd_hist: float = 0.0
    atr: float = 0.0
    rsi_5m: Optional[float] = None


def evaluate_indicators(
    store: Any,
    now: datetime,
    thresholds: BiasThresholds,
    nifty_open: float = 0.0,
    nifty_ltp: float = 0.0,
    check_volume: bool = True,
) -> EntryEvaluation:
    """Indicator gates, in order; candle indicators are only computed once the
    cheaper tick-level gates pass.  ``reason`` names the first failing gate."""
    ev = EntryEvaluation(n_candles=completed_candles(store))
    ev.ltp = store.ltp
    ev.vwap = store.compute_vwap()
    ev.rsi = store.compute_rsi()
    ev.vol_ratio = store.compute_volume_ratio()

    def _reject(reason: str) -> EntryEvaluation:
        ev.reason = reason
        return ev

    if nifty_open > 0 and nifty_ltp > 0:
        ev.nifty_drift = (nifty_ltp - nifty_open) / nifty_open
        ev.nifty_suppressed = ev.nifty_drift < thresholds.nifty_drift_min
        if ev.nifty_suppressed:
            return _reject("NIFTY_TREND")

    if ev.ltp <= ev.vwap:
        return _reject("BELOW_VWAP")
    if not (thresholds.rsi_lo <= ev.rsi <= thresholds.rsi_hi):
        return _reject("RSI_BAND")
    if now.hour == 9 and now.minute < 45:
        lo = thresholds.rsi_lo + EARLY_SESSION_RSI_TIGHTEN
        hi = thresholds.rsi_hi - EARLY_SESSION_RSI_TIGHTEN
        if not (lo <= ev.rsi <= hi):
            return _reject("EARLY_RSI_BAND")
    if check_volume and store.avg_volume_20d > 0 and ev.vol_ratio < thresholds.vol_min:
        return _reject("VOLUME")
    if store._prev_close > 0 and store._open_price > 0:
        gap_pct = (store._open_price - store._prev_close) / store._prev_close * 100
        if gap_pct > thresholds.gap_max_pct:
            return _reject("GAP_UP")

    ev.ema_9 = store.compute_ema(9)
    ev.ema_21 = store.compute_ema(21)
    ev.macd_hist = store.compute_macd_histogram()
    ev.atr = store.compute_atr()
    if ev.ema_21 > 0 and ev.ltp < ev.ema_21 * EMA21_TREND_FLOOR:
        return _reject("BELOW_EMA21")
    if ev.n_candles >= MIN_CANDLES_FOR_TREND:
        if ev.ema_9 > 0 and ev.ema_21 > 0 and ev.ema_9 <= ev.ema_21:
            return _reject("EMA_CROSS")
        if ev.macd_hist <= 0:
            return _reject("MACD")

    ev.rsi_5m = store.compute_rsi_htf()
    if ev.rsi_5m is not None and not (HTF_RSI_BAND[0] <= ev.rsi_5m <= HTF_RSI_BAND[1]):
        return _reject("RSI_5M_BAND")

    ev.passed = True
    return ev


def size_position(ltp: float, atr: float, n_candles: int, settings: Any) -> int:
    """Production position size: risk budget / stop distance, capped so the
    notional fits ``total_capital / max_open_positions``.  ATR is trusted only
    after 45 completed 1m candles (the opening-range ATR is noisy)."""
    max_loss = settings.total_capital * settings.max_loss_per_trade_pct
    if atr > 0 and n_candles >= 45:
        sl_distance = atr * settings.atr_sl_multiplier
    else:
        sl_distance = ltp * settings.stop_loss_pct
    if sl_distance <= 0:
        return 0
    qty_from_risk = max(1, int(max_loss / sl_distance))
    if ltp > 0 and settings.max_open_positions > 0:
        qty_from_capital = max(1, int(settings.total_capital / settings.max_open_positions / ltp))
        return min(qty_from_risk, qty_from_capital)
    return qty_from_risk
