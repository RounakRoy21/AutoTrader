"""
Backtest Runner — replays historical 1-minute Groww candles through the
trading system's scanner signal logic to measure signal quality and
simulated trade accuracy.

Usage (from trading-system/backend/):
    python -m backtest.runner \\
        --symbols RELIANCE INFY HDFCBANK \\
        --start 2026-05-01 --end 2026-06-01

    # Skip the volume-ratio filter (avg_volume_20d unknown):
    python -m backtest.runner --symbols WIPRO --start 2026-05-01 --end 2026-05-31 \\
        --no-volume-filter

    # Pre-supply 20-day avg volume per symbol (comma-separated name=vol pairs):
    python -m backtest.runner --symbols RELIANCE --start 2026-05-01 --end 2026-05-31 \\
        --avg-vol RELIANCE=12500000

How it works
------------
For each symbol and each trading day in the range:
1. 1-minute historical candles are fetched from the Groww API.
2. Each candle is replayed as 4 synthetic ticks (O→{L,H}→{H,L}→C), preserving
   realistic OHLC paths.  The production TickDataStore processes them identically
   to live WebSocket ticks.
3. After each candle the signal conditions from _check_signal are applied
   (same thresholds, same logic; datetime.now() replaced by the candle
   timestamp so market-hours and cooldown logic is faithful to history).
4. When a signal fires, subsequent candles are inspected to determine
   whether price reached the target or stop-loss first.
5. A per-signal table and aggregate statistics are printed.

Limitations
-----------
- LLM decisions (decision_engine.py) are NOT replayed — only the rule-based
  scanner pre-filters are evaluated.  Accuracy figures therefore represent the
  *scanner* precision, not the full pipeline.
- Execution at the exact signal LTP is assumed (no slippage model).
- Signals fire from the 16th candle onward (15 completed 1m candles + 50-tick
  warmup across 4-tick-per-candle replay = ~16 minutes).
- The 30-calendar-day Groww API limit is handled by auto-chunking.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

# ── path setup so we can import from the backend package ──────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import pandas as pd

from agents.scanner import TickDataStore          # reuse production indicator code
from integrations.groww_client import get_groww_client, _retry_sync

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# ── Signal condition parameters (mirrors production defaults) ─────────────────
# These MUST stay in sync with scanner.py and decision_engine.py constants.
RSI_LO, RSI_HI           = 50.0, 65.0     # scanner band (NEUTRAL bias, floor raised from 45)
VOL_RATIO_MIN             = 1.5             # minimum prorated volume ratio
HTF_RSI_LO, HTF_RSI_HI   = 45.0, 72.0     # 5-min RSI band
GAP_MAX_PCT               = 1.5             # max gap-at-open (%)
ATR_SL_MULT               = 1.5             # stop-loss = entry − ATR × mult
ATR_TGT_MULT              = 3.0             # target    = entry + ATR × mult
STOP_LOSS_PCT             = 0.010           # fixed fallback SL  (1%)
MIN_TARGET_PCT            = 0.020           # fixed fallback tgt (2%)
MIN_TICKS_BT              = 50              # warmup ticks (same as production)
MIN_CANDLES_BT            = 15             # warmup 1m candles
SIGNAL_COOLDOWN_SECS      = 1800            # suppress repeat signals (30 min)
SIGNAL_CUTOFF             = (15, 15)        # no new signals after 3:15 PM IST
SESSION_OPEN_MIN          = 9 * 60 + 15    # 9:15 AM in minutes-from-midnight
SESSION_TOTAL_MIN         = 375             # 9:15 → 15:30 = 375 min
NIFTY_TREND_THRESH        = -0.005          # suppress longs when NIFTY drifts −0.5% from open
NIFTY_SYMBOL              = "NIFTY"         # Groww historical-candle symbol for NIFTY 50 index (NSE-NIFTY)
DAILY_EMA_PERIOD          = 21              # multi-day trend filter period (mirrors decision_engine.py)
DAILY_EMA_LOOKBACK        = 25              # daily candles to use for EMA computation (same as production)


# ── EMA helper (mirrors decision_engine._ema_of) ──────────────────────────────

def _ema_of(closes: list, period: int) -> float:
    """Exponential moving average with an SMA seed over the first `period` values."""
    if len(closes) < period:
        return 0.0
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1.0 - k)
    return ema


# ── Backtest-aware TickDataStore ──────────────────────────────────────────────

class BacktestTickDataStore(TickDataStore):
    """TickDataStore with an injectable clock for volume-ratio prorating.

    In production, compute_volume_ratio() calls datetime.now(IST) to determine
    how far through the session we are.  In a backtest we pass the candle
    timestamp instead so the ratio is computed relative to the historical time.
    """

    def __init__(self, symbol: str, avg_volume_20d: float = 0.0) -> None:
        super().__init__(symbol, avg_volume_20d)
        self._bt_now: Optional[datetime] = None  # set before each signal check

    def compute_volume_ratio(self) -> float:  # type: ignore[override]
        if self._df is None or self._df.empty or self.avg_volume_20d <= 0:
            return 0.0
        current_volume = float(self._df["cum_volume"].iloc[-1])
        now = self._bt_now or datetime.now(IST)
        minutes_since_open = max(1, (now.hour * 60 + now.minute) - SESSION_OPEN_MIN)
        session_fraction = min(1.0, minutes_since_open / SESSION_TOTAL_MIN)
        prorated_avg = self.avg_volume_20d * session_fraction
        return current_volume / prorated_avg if prorated_avg > 0 else 0.0


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    symbol: str
    trade_date: date
    signal_time: str        # HH:MM (candle open time)
    ltp: float              # entry price at signal
    vwap: float
    rsi: float
    vol_ratio: float
    rsi_5m: Optional[float]
    ema_9: float
    ema_21: float
    macd_hist: float
    atr: float
    sl: float               # stop-loss price
    target: float           # target price
    rr: float               # reward:risk ratio
    outcome: str            # WIN | LOSS | TIMEOUT
    exit_price: float
    pnl_pct: float          # (exit − entry) / entry × 100


# ── Candle fetching ───────────────────────────────────────────────────────────

def _parse_candle_ts(raw_ts) -> Optional[datetime]:
    """Parse Groww candle timestamp → timezone-aware datetime (IST).

    Groww returns naive ISO strings that are already in IST (e.g.
    '2026-06-23T09:15:00'). We attach IST directly with replace() instead
    of calling astimezone(), which would first re-interpret the naive
    value as local/UTC time and shift it by 5:30 hours.
    """
    if raw_ts is None:
        return None
    if isinstance(raw_ts, (int, float)):
        # epoch milliseconds
        return datetime.fromtimestamp(raw_ts / 1000, tz=IST)
    if isinstance(raw_ts, str):
        try:
            dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                # Naive string → Groww always sends IST, attach directly
                return dt.replace(tzinfo=IST)
            return dt.astimezone(IST)
        except ValueError:
            pass
    return None


async def fetch_candles_for_symbol(
    groww,
    symbol: str,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Fetch 1-minute candles from Groww API, chunked to the 30-day limit.

    Returns a DataFrame with columns:
        timestamp (datetime, IST), date, open, high, low, close, volume
    sorted ascending by timestamp.
    """
    MAX_DAYS = 29  # stay safely under the 30-calendar-day API limit
    all_rows: List[Dict] = []
    chunk_start = start_date

    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=MAX_DAYS), end_date)

        # Capture loop variables for the closure
        _cs = chunk_start.strftime("%Y-%m-%d")
        _ce = chunk_end.strftime("%Y-%m-%d")
        _sym = f"NSE-{symbol}"

        @_retry_sync
        def _fetch(_cs=_cs, _ce=_ce, _sym=_sym):  # noqa: B023
            return groww.get_historical_candles(
                exchange="NSE",
                segment="CASH",
                groww_symbol=_sym,
                start_time=f"{_cs} 09:15:00",
                end_time=f"{_ce} 15:30:00",
                candle_interval="1minute",
            )

        raw = await asyncio.to_thread(_fetch)
        candle_list = raw.get("candles", []) if isinstance(raw, dict) else []

        for c in candle_list:
            if isinstance(c, (list, tuple)) and len(c) >= 6:
                ts = _parse_candle_ts(c[0])
                if ts is None:
                    continue
                # Skip pre-market / post-close candles (Groww multi-day fetches
                # include 9:00–9:14 pre-opening candles with price=0 that corrupt
                # VWAP and ATR when the range spans multiple days).
                if (ts.hour, ts.minute) < (9, 15) or (ts.hour, ts.minute) > (15, 30):
                    continue
                o  = float(c[1] or 0)
                h  = float(c[2] or 0)
                l  = float(c[3] or 0)
                cl = float(c[4] or 0)
                # Skip zero-priced candles (data errors or auction candles)
                if o <= 0 or h <= 0 or l <= 0 or cl <= 0:
                    continue
                all_rows.append({
                    "timestamp": ts,
                    "date": ts.date(),
                    "open":   o,
                    "high":   h,
                    "low":    l,
                    "close":  cl,
                    "volume": int(c[5] or 0),
                })

        chunk_start = chunk_end + timedelta(days=1)

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows).sort_values("timestamp").reset_index(drop=True)
    return df


# ── 20-day average volume helper ──────────────────────────────────────────────

def compute_avg_volume_20d(df: pd.DataFrame, backtest_start: date) -> float:
    """Estimate 20-day average daily volume from candle data before the backtest start.

    Uses the 20 trading days immediately before backtest_start that appear
    in ``df``.  Returns 0.0 when not enough history is present (volume filter
    will be skipped for this symbol).
    """
    pre = df[df["date"] < backtest_start]
    if pre.empty:
        return 0.0
    daily_vol = pre.groupby("date")["volume"].sum()
    last_20 = daily_vol.tail(20)
    if len(last_20) == 0:
        return 0.0
    return float(last_20.mean())


# ── NIFTY index candles (macro trend filter) ──────────────────────────────────

async def fetch_nifty_candles(
    groww,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """Fetch NIFTY 50 index 1-minute candles for the macro trend filter.

    Attempts to fetch using the Groww historical-candle symbol ``NIFTY50``
    (→ ``NSE-NIFTY50``).  Returns an empty DataFrame on any failure so the
    caller can skip the NIFTY filter gracefully without crashing.
    """
    try:
        return await fetch_candles_for_symbol(groww, NIFTY_SYMBOL, start_date, end_date)
    except Exception as exc:
        logger.warning("NIFTY index candle fetch failed (%s) — macro trend filter disabled.", exc)
        return pd.DataFrame()


# ── Candle → ticks ────────────────────────────────────────────────────────────

def _candle_to_ticks(
    row: pd.Series,
    cum_vol_before: int,
    day_open: float,
    prev_day_close: float,
) -> List[Dict]:
    """Expand one 1-minute candle into 4 synthetic ticks (O → {L,H} → {H,L} → C).

    The intracandle price path follows the standard assumption:
      Bullish (close >= open): open → low → high → close
      Bearish (close  < open): open → high → low → close

    All 4 ticks share the candle's start timestamp so the CandleBuilder groups
    them into a single 1-minute bucket (correct behaviour — they all belong to
    that minute).  The close tick receives the full candle volume delta so that
    VWAP accumulates only once per candle (open/intermediate ticks carry 0 volume).

    ``day_open`` and ``prev_day_close`` are injected into the first tick of the
    session via tick["ohlc"] to seed TickDataStore._open_price / _prev_close.
    """
    ts = row["timestamp"]
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]
    bullish = c >= o
    intraday_path = [o, (l if bullish else h), (h if bullish else l), c]
    cum_vol_close = cum_vol_before + row["volume"]

    ticks = []
    for i, price in enumerate(intraday_path):
        is_close_tick = i == 3
        t: Dict = {
            "exchange_timestamp": ts,
            "ltp": price,
            "last_price": price,
            # Cumulative volume only advances on the close tick
            "volume_traded": cum_vol_close if is_close_tick else cum_vol_before,
        }
        # Seed open/prev_close from the very first tick of the day
        if cum_vol_before == 0 and i == 0:
            t["ohlc"] = {
                "open":  day_open,
                "high":  h,
                "low":   l,
                "close": prev_day_close,  # ← stored as _prev_close in TickDataStore
            }
        ticks.append(t)
    return ticks


# ── Signal condition check (backtest version) ─────────────────────────────────

def check_signal(
    store: BacktestTickDataStore,
    candle_ts: datetime,
    last_signal_ts: Optional[datetime],
    skip_volume: bool,
    nifty_open: float = 0.0,
    nifty_ltp: float = 0.0,
) -> Optional[BacktestResult]:
    """Apply scanner signal conditions using the historical candle timestamp.

    Mirrors scanner.py _check_signal() exactly, except:
    - datetime.now(IST) → candle_ts
    - OHLCV staleness gate omitted (data is always fresh in backtest)
    - Market bias fixed at NEUTRAL
    - NIFTY macro-trend filter uses pre-fetched index candles (nifty_open/nifty_ltp)

    Returns a BacktestResult (without outcome filled in) or None.
    """
    # Market-hours gate
    if (candle_ts.hour, candle_ts.minute) >= SIGNAL_CUTOFF:
        return None
    # Friday 14:00 cutoff
    if candle_ts.weekday() == 4 and candle_ts.hour >= 14:
        return None
    # Minimum tick warmup
    if len(store.ticks) < MIN_TICKS_BT:
        return None
    # Minimum completed 1m candles
    c1m = store._candles_1m.completed_df
    n_candles = len(c1m) if c1m is not None else 0
    if n_candles < MIN_CANDLES_BT:
        return None
    # Cooldown
    if last_signal_ts is not None:
        elapsed = (candle_ts - last_signal_ts).total_seconds()
        if elapsed < SIGNAL_COOLDOWN_SECS:
            return None

    # Inject backtest clock into the store for volume-ratio computation
    store._bt_now = candle_ts

    ltp       = store.ltp
    vwap      = store.compute_vwap()
    rsi       = store.compute_rsi()
    vol_ratio = store.compute_volume_ratio()
    ema_9     = store.compute_ema(9)
    ema_21    = store.compute_ema(21)
    macd_hist = store.compute_macd_histogram()
    atr       = store.compute_atr()
    rsi_5m    = store.compute_rsi_htf()

    # Condition 1: Price > VWAP
    if ltp <= vwap:
        return None
    # Condition 2: RSI band (NEUTRAL bias)
    if not (RSI_LO <= rsi <= RSI_HI):
        return None
    # Early-session tighter RSI: first 30 minutes (9:15–9:44) indicators warm up
    # over the first 15–30 candles; a ±5-point tighter band reduces whipsaw entries.
    if candle_ts.hour == 9 and candle_ts.minute < 45:
        if not ((RSI_LO + 5) <= rsi <= (RSI_HI - 5)):
            return None
    # NIFTY macro-trend filter (mirrors production _check_signal A2).
    # Suppress long signals when NIFTY has drifted below NIFTY_TREND_THRESH from
    # its session open.  Skip when nifty_open=0 (index data unavailable).
    if nifty_open > 0 and nifty_ltp > 0:
        nifty_drift = (nifty_ltp - nifty_open) / nifty_open
        if nifty_drift < NIFTY_TREND_THRESH:
            return None
    # Condition 3: Volume ratio (skipped when avg_volume_20d = 0)
    if not skip_volume and store.avg_volume_20d > 0 and vol_ratio < VOL_RATIO_MIN:
        return None
    # Condition 7: Gap-at-open filter
    if store._prev_close > 0 and store._open_price > 0:
        gap_pct = (store._open_price - store._prev_close) / store._prev_close * 100
        if gap_pct > GAP_MAX_PCT:
            return None
    # Stock-level intraday trend bias: suppress long signals when price is >1%
    # below EMA(21).  Complements the NIFTY filter by catching single-stock
    # downtrends on mixed or bullish market days (e.g. TATASTEEL falling while
    # NIFTY is flat).  Safe below 21 candles — compute_ema() returns 0.0 and
    # the ema_21 > 0 guard skips the check.
    if ema_21 > 0 and ltp < ema_21 * 0.99:
        return None
    # Conditions 4+5: EMA crossover + MACD momentum — enforced from ≥26 candles
    # (MACD minimum) so the filter is active from ~9:41 AM rather than ~9:50 AM.
    if n_candles >= 26:
        if ema_9 > 0 and ema_21 > 0 and ema_9 <= ema_21:
            return None
        if macd_hist <= 0:
            return None
    # Condition 6: 5m RSI band
    if rsi_5m is not None and not (HTF_RSI_LO <= rsi_5m <= HTF_RSI_HI):
        return None

    # ── Compute stop-loss and target ──────────────────────────────────────────
    if atr > 0:
        sl  = round(ltp - atr * ATR_SL_MULT, 2)
        tgt = round(ltp + atr * ATR_TGT_MULT, 2)
    else:
        sl  = round(ltp * (1 - STOP_LOSS_PCT), 2)
        tgt = round(ltp * (1 + MIN_TARGET_PCT), 2)

    sl_dist = abs(ltp - sl)
    rr = round((tgt - ltp) / sl_dist, 2) if sl_dist > 0 else float(ATR_TGT_MULT / ATR_SL_MULT)

    return BacktestResult(
        symbol=store.symbol,
        trade_date=candle_ts.date(),
        signal_time=candle_ts.strftime("%H:%M"),
        ltp=round(ltp, 2),
        vwap=round(vwap, 2),
        rsi=round(rsi, 2),
        vol_ratio=round(vol_ratio, 2),
        rsi_5m=round(rsi_5m, 2) if rsi_5m is not None else None,
        ema_9=round(ema_9, 2),
        ema_21=round(ema_21, 2),
        macd_hist=round(macd_hist, 4),
        atr=round(atr, 2),
        sl=sl,
        target=tgt,
        rr=rr,
        outcome="",      # filled in after outcome evaluation
        exit_price=0.0,
        pnl_pct=0.0,
    )


# ── Outcome evaluation ────────────────────────────────────────────────────────

def evaluate_outcome(
    signal: BacktestResult,
    future_candles: pd.DataFrame,
) -> BacktestResult:
    """Determine whether a signal hit its target or stop-loss.

    Scans forward through ``future_candles`` (rows after the signal candle)
    looking for the first candle where:
      high >= target_price  → WIN  (target hit)
      low  <= stop_loss     → LOSS (stop hit)
    If both conditions appear in the SAME candle, the conservative assumption
    is applied: LOSS (stop assumed to hit before target within the candle).

    When no subsequent candle satisfies either condition (trade runs to session
    end), the outcome is TIMEOUT and exit price is the last candle's close.
    """
    for _, row in future_candles.iterrows():
        hit_target = row["high"] >= signal.target
        hit_sl     = row["low"]  <= signal.sl

        if hit_sl and hit_target:
            # Both in the same candle: conservative → LOSS
            signal.outcome    = "LOSS"
            signal.exit_price = signal.sl
        elif hit_target:
            signal.outcome    = "WIN"
            signal.exit_price = signal.target
        elif hit_sl:
            signal.outcome    = "LOSS"
            signal.exit_price = signal.sl

        if signal.outcome:
            break
    else:
        # Never hit target or SL — session ended
        signal.outcome    = "TIMEOUT"
        signal.exit_price = round(float(future_candles["close"].iloc[-1]), 2) if not future_candles.empty else signal.ltp

    signal.pnl_pct = round((signal.exit_price - signal.ltp) / signal.ltp * 100, 3)
    return signal


# ── Daily trend filter ───────────────────────────────────────────────────────

def _build_daily_trend_filter(
    df: pd.DataFrame,
    backtest_start: date,
) -> Dict[date, bool]:
    """Return a per-day dict indicating whether the multi-day trend is up.

    For each backtest day ``d``, looks at the last ``DAILY_EMA_LOOKBACK`` daily
    closes BEFORE ``d`` (using the prior trading day's close as the reference,
    mirroring the production filter which runs intraday and sees yesterday's
    data as the freshest daily bar).

    Returns True (allow signals) when the most-recent prior close >= EMA21 of
    that lookback window, or when there is insufficient history (fail open).
    """
    # Daily close = last 1-min candle close per trading day
    daily_closes = df.groupby("date")["close"].last().sort_index()
    all_dates = list(daily_closes.index)

    trend_ok: Dict[date, bool] = {}
    for i, d in enumerate(all_dates):
        if d < backtest_start:
            continue  # pre-backtest warm-up days: no verdict needed

        # Prior trading-day closes available before day d
        prior_closes = [float(daily_closes[pd]) for pd in all_dates[:i]]

        if len(prior_closes) < DAILY_EMA_PERIOD:
            trend_ok[d] = True  # fail open: not enough history
            continue

        window = prior_closes[-DAILY_EMA_LOOKBACK:]
        ema21 = _ema_of(window, DAILY_EMA_PERIOD)
        trend_ok[d] = window[-1] >= ema21

    return trend_ok


# ── Per-day replay ────────────────────────────────────────────────────────────

def replay_day(
    day_df: pd.DataFrame,
    prev_close: float,
    symbol: str,
    avg_volume_20d: float,
    skip_volume: bool,
    nifty_day_df: Optional[pd.DataFrame] = None,
    daily_uptrend: bool = True,
) -> List[BacktestResult]:
    """Replay one trading day of 1-minute candles and return all signals fired."""
    if day_df.empty:
        return []
    # Multi-day trend filter: skip this entire day when the stock is below its
    # daily EMA21 (mirrors decision_engine._is_stock_uptrending).
    if not daily_uptrend:
        return []

    store = BacktestTickDataStore(symbol=symbol, avg_volume_20d=avg_volume_20d)
    results: List[BacktestResult] = []
    last_signal_ts: Optional[datetime] = None
    cum_vol: int = 0
    day_open = float(day_df.iloc[0]["open"])

    # NIFTY macro-trend: capture the session open from the first NIFTY candle
    nifty_open: float = 0.0
    if nifty_day_df is not None and not nifty_day_df.empty:
        nifty_open = float(nifty_day_df.iloc[0]["open"])

    for idx, row in day_df.iterrows():
        ticks = _candle_to_ticks(
            row=row,
            cum_vol_before=cum_vol,
            day_open=day_open,
            prev_day_close=prev_close,
        )
        for tick in ticks:
            store.add_tick(tick)
        cum_vol += int(row["volume"])

        # Check signal at the candle close (after all 4 ticks are fed)
        candle_ts = row["timestamp"]

        # NIFTY LTP: last available NIFTY close at or before this stock candle
        nifty_ltp: float = 0.0
        if nifty_day_df is not None and not nifty_day_df.empty:
            mask = nifty_day_df["timestamp"] <= candle_ts
            if mask.any():
                nifty_ltp = float(nifty_day_df.loc[mask, "close"].iloc[-1])

        sig = check_signal(
            store=store,
            candle_ts=candle_ts,
            last_signal_ts=last_signal_ts,
            skip_volume=skip_volume,
            nifty_open=nifty_open,
            nifty_ltp=nifty_ltp,
        )
        if sig is not None:
            # Find future candles for this day to evaluate outcome
            future = day_df[day_df["timestamp"] > candle_ts].reset_index(drop=True)
            sig = evaluate_outcome(sig, future)
            results.append(sig)
            last_signal_ts = candle_ts

    return results


# ── Reporting ─────────────────────────────────────────────────────────────────

def print_report(results: List[BacktestResult]) -> None:
    """Print a per-signal table followed by aggregate statistics."""
    if not results:
        print("\nNo signals generated for the specified parameters.")
        return

    # ── Per-signal table ──────────────────────────────────────────────────────
    header = (
        f"{'Symbol':<12} {'Date':<12} {'Time':<6} {'LTP':>8} {'VWAP':>8} "
        f"{'RSI':>6} {'VolR':>6} {'ATR':>6} {'SL':>8} {'Tgt':>8} "
        f"{'RR':>5} {'Outcome':<8} {'Exit':>8} {'PnL%':>7}"
    )
    sep = "─" * len(header)
    print(f"\n{sep}")
    print(header)
    print(sep)

    for r in results:
        outcome_label = {"WIN": "✓ WIN", "LOSS": "✗ LOSS", "TIMEOUT": "~ TMOUT"}.get(r.outcome, r.outcome)
        print(
            f"{r.symbol:<12} {str(r.trade_date):<12} {r.signal_time:<6} "
            f"{r.ltp:>8.2f} {r.vwap:>8.2f} {r.rsi:>6.1f} {r.vol_ratio:>6.2f} "
            f"{r.atr:>6.2f} {r.sl:>8.2f} {r.target:>8.2f} "
            f"{r.rr:>5.2f} {outcome_label:<8} {r.exit_price:>8.2f} {r.pnl_pct:>+7.3f}%"
        )

    print(sep)

    # ── Aggregate stats ───────────────────────────────────────────────────────
    total   = len(results)
    wins    = sum(1 for r in results if r.outcome == "WIN")
    losses  = sum(1 for r in results if r.outcome == "LOSS")
    timeouts = sum(1 for r in results if r.outcome == "TIMEOUT")
    win_rate = wins / total * 100

    decided  = [r for r in results if r.outcome in ("WIN", "LOSS")]
    decided_win_rate = (sum(1 for r in decided if r.outcome == "WIN") / len(decided) * 100) if decided else 0.0

    pnls     = [r.pnl_pct for r in results]
    avg_pnl  = sum(pnls) / len(pnls)
    total_pnl = sum(pnls)

    rrs      = [r.rr for r in results]
    avg_rr   = sum(rrs) / len(rrs)

    win_pnls  = [r.pnl_pct for r in results if r.outcome == "WIN"]
    loss_pnls = [r.pnl_pct for r in results if r.outcome == "LOSS"]
    avg_win   = sum(win_pnls) / len(win_pnls) if win_pnls else 0.0
    avg_loss  = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0.0

    # Profit factor: gross wins / gross losses (absolute)
    gross_wins   = sum(win_pnls)
    gross_losses = abs(sum(loss_pnls))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else float("inf")

    print(f"\n{'Summary':─<50}")
    print(f"  Signals total      : {total}")
    print(f"  Wins               : {wins}  ({win_rate:.1f}% of total)")
    print(f"  Losses             : {losses}")
    print(f"  Timeouts (open)    : {timeouts}")
    print(f"  Win rate (decided) : {decided_win_rate:.1f}%  (excl. timeouts)")
    print(f"  Avg P&L per trade  : {avg_pnl:+.3f}%")
    print(f"  Avg win            : {avg_win:+.3f}%")
    print(f"  Avg loss           : {avg_loss:+.3f}%")
    print(f"  Sum P&L            : {total_pnl:+.3f}%")
    print(f"  Avg R:R            : {avg_rr:.2f}")
    print(f"  Profit factor      : {profit_factor:.2f}")

    # Per-symbol breakdown
    symbols = sorted({r.symbol for r in results})
    if len(symbols) > 1:
        print(f"\n{'Per-symbol':─<50}")
        for sym in symbols:
            sym_res = [r for r in results if r.symbol == sym]
            sym_wins = sum(1 for r in sym_res if r.outcome == "WIN")
            sym_wr   = sym_wins / len(sym_res) * 100
            sym_pnl  = sum(r.pnl_pct for r in sym_res)
            print(f"  {sym:<12} signals={len(sym_res)}  wins={sym_wins}  win%={sym_wr:.1f}  sum_pnl={sym_pnl:+.3f}%")

    # ── Session-phase breakdown ───────────────────────────────────────────────
    # Groups signals by the NSE intraday session phase in which they fired so we
    # can see how win rate degrades through the day, independent of when the live
    # system happened to be running.  This is the unbiased view the live P&L can't
    # give us when the containers are only started after mid-day.
    def _phase(signal_time: str) -> str:
        hh, mm = (int(x) for x in signal_time.split(":"))
        minutes = hh * 60 + mm
        if minutes < 11 * 60 + 30:      # 09:15–11:29
            return "EARLY (09:15-11:30)"
        if minutes < 13 * 60:           # 11:30–12:59
            return "DEAD ZONE (11:30-13:00)"
        if minutes < 14 * 60 + 30:      # 13:00–14:29
            return "RECOVERY (13:00-14:30)"
        return "LATE (14:30-15:30)"

    _phase_order = [
        "EARLY (09:15-11:30)",
        "DEAD ZONE (11:30-13:00)",
        "RECOVERY (13:00-14:30)",
        "LATE (14:30-15:30)",
    ]
    print(f"\n{'Per-session-phase':─<60}")
    print(f"  {'Phase':<24} {'Sigs':>5} {'Win%':>7} {'DecWin%':>8} {'SumPnL%':>9} {'PF':>6}")
    for phase in _phase_order:
        ph_res = [r for r in results if _phase(r.signal_time) == phase]
        if not ph_res:
            continue
        ph_total   = len(ph_res)
        ph_wins    = sum(1 for r in ph_res if r.outcome == "WIN")
        ph_decided = [r for r in ph_res if r.outcome in ("WIN", "LOSS")]
        ph_dec_wr  = (sum(1 for r in ph_decided if r.outcome == "WIN") / len(ph_decided) * 100) if ph_decided else 0.0
        ph_wr      = ph_wins / ph_total * 100
        ph_pnl     = sum(r.pnl_pct for r in ph_res)
        ph_gw      = sum(r.pnl_pct for r in ph_res if r.outcome == "WIN")
        ph_gl      = abs(sum(r.pnl_pct for r in ph_res if r.outcome == "LOSS"))
        ph_pf      = ph_gw / ph_gl if ph_gl > 0 else float("inf")
        ph_pf_str  = f"{ph_pf:.2f}" if ph_pf != float("inf") else "∞"
        print(
            f"  {phase:<24} {ph_total:>5} {ph_wr:>6.1f}% {ph_dec_wr:>7.1f}% "
            f"{ph_pnl:>+8.2f}% {ph_pf_str:>6}"
        )



# ── Main orchestration ────────────────────────────────────────────────────────

async def run_backtest(
    symbols: List[str],
    start_date: date,
    end_date: date,
    avg_vol_overrides: Dict[str, float],
    skip_volume: bool,
    fetch_history_days: int,
) -> List[BacktestResult]:
    """Fetch candles, replay through scanner logic, and collect results."""
    client = get_groww_client()
    groww  = await client.get_groww()

    all_results: List[BacktestResult] = []

    # Extend the fetch window backward by ``fetch_history_days`` so we can
    # compute the 20-day average volume and have a prev_close for day 1.
    data_start = start_date - timedelta(days=fetch_history_days)

    # Fetch NIFTY 50 index candles once for the full window (A2 macro trend filter).
    print("\n[+] Fetching NIFTY50 index candles for macro trend filter ...")
    nifty_df = await fetch_nifty_candles(groww, data_start, end_date)
    if nifty_df.empty:
        print("    \u26a0 NIFTY50 candles unavailable \u2014 macro trend filter disabled.")
    else:
        print(f"    Fetched {len(nifty_df):,} NIFTY50 candles across {nifty_df['date'].nunique()} days.")
    nifty_by_date: Dict[date, pd.DataFrame] = {}
    if not nifty_df.empty:
        for _nd, _ng in nifty_df.groupby("date"):
            nifty_by_date[_nd] = _ng.reset_index(drop=True)

    for symbol in symbols:
        symbol = symbol.upper()
        print(f"\n[+] Fetching {symbol}  {data_start} → {end_date} ...")

        df = await fetch_candles_for_symbol(
            groww=groww,
            symbol=symbol,
            start_date=data_start,
            end_date=end_date,
        )

        if df.empty:
            print(f"    ⚠ No candle data returned for {symbol}. Skipping.")
            continue

        print(f"    Fetched {len(df):,} 1-min candles across {df['date'].nunique()} trading days.")

        # 20-day average volume: use the caller's override if provided, otherwise
        # compute from pre-backtest candle data.
        if symbol in avg_vol_overrides:
            avg_vol = avg_vol_overrides[symbol]
            print(f"    Using supplied avg_volume_20d = {avg_vol:,.0f}")
        else:
            avg_vol = compute_avg_volume_20d(df, start_date)
            if avg_vol > 0:
                print(f"    Computed avg_volume_20d = {avg_vol:,.0f}  (from pre-backtest data)")
            else:
                print(f"    ⚠ Not enough pre-backtest data to compute avg volume; volume filter skipped.")

        # Group by date and replay each trading day
        backtest_dates = df[df["date"] >= start_date]["date"].unique()
        # Sort the unique dates
        backtest_dates = sorted(backtest_dates)

        # Build per-day daily trend filter (mirrors decision_engine multi-day filter)
        daily_trend = _build_daily_trend_filter(df, start_date)
        days_filtered_by_trend = sum(1 for d in backtest_dates if not daily_trend.get(d, True))
        if days_filtered_by_trend:
            print(f"    Daily EMA21 filter will suppress signals on {days_filtered_by_trend}/{len(backtest_dates)} days.")

        # Build a quick lookup: last close price before each date
        all_dates_sorted = sorted(df["date"].unique())
        last_close_by_date: Dict[date, float] = {}
        prev_close = 0.0
        for d in all_dates_sorted:
            day_data = df[df["date"] == d]
            last_close_by_date[d] = float(day_data["close"].iloc[-1]) if not day_data.empty else 0.0

        for bd in backtest_dates:
            day_df = df[df["date"] == bd].reset_index(drop=True)
            if day_df.empty:
                continue

            # Previous day's close
            prev_idx = all_dates_sorted.index(bd) - 1
            prev_close = last_close_by_date[all_dates_sorted[prev_idx]] if prev_idx >= 0 else 0.0

            day_results = replay_day(
                day_df=day_df,
                prev_close=prev_close,
                symbol=symbol,
                avg_volume_20d=avg_vol,
                skip_volume=skip_volume,
                nifty_day_df=nifty_by_date.get(bd),
                daily_uptrend=daily_trend.get(bd, True),
            )

            wins   = sum(1 for r in day_results if r.outcome == "WIN")
            losses = sum(1 for r in day_results if r.outcome == "LOSS")
            tmouts = sum(1 for r in day_results if r.outcome == "TIMEOUT")
            print(
                f"    {bd}  signals={len(day_results)}  "
                f"W={wins} L={losses} T={tmouts}"
            )
            all_results.extend(day_results)

    return all_results


# ── CLI entry point ───────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Backtest the AutoTrader scanner signal logic against historical Groww candle data."
    )
    p.add_argument(
        "--symbols", "-s", nargs="+", required=True,
        metavar="SYMBOL",
        help="NSE trading symbols to backtest (e.g. RELIANCE INFY HDFCBANK).",
    )
    p.add_argument(
        "--start", required=True,
        metavar="YYYY-MM-DD",
        help="Backtest start date (inclusive).",
    )
    p.add_argument(
        "--end", required=False,
        metavar="YYYY-MM-DD",
        default=None,
        help="Backtest end date (inclusive). Defaults to yesterday.",
    )
    p.add_argument(
        "--no-volume-filter", action="store_true",
        help="Disable the volume-ratio condition (useful when avg volume data is unavailable).",
    )
    p.add_argument(
        "--avg-vol", nargs="*", default=[],
        metavar="SYMBOL=VALUE",
        help=(
            "20-day average daily volume overrides, e.g. RELIANCE=12500000 INFY=8000000. "
            "When not supplied the runner estimates it from pre-backtest candle data."
        ),
    )
    p.add_argument(
        "--history-days", type=int, default=35,
        metavar="N",
        help=(
            "Extra days of candle history to fetch before the start date for "
            "avg-volume computation and prev-close seeding (default: 35)."
        ),
    )
    p.add_argument(
        "--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: WARNING — suppresses scanner debug noise).",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        start_date = date.fromisoformat(args.start)
    except ValueError:
        print(f"ERROR: Invalid --start date '{args.start}'. Expected YYYY-MM-DD.")
        sys.exit(1)

    if args.end:
        try:
            end_date = date.fromisoformat(args.end)
        except ValueError:
            print(f"ERROR: Invalid --end date '{args.end}'. Expected YYYY-MM-DD.")
            sys.exit(1)
    else:
        end_date = date.today() - timedelta(days=1)

    if end_date < start_date:
        print(f"ERROR: --end ({end_date}) is before --start ({start_date}).")
        sys.exit(1)

    # Parse avg-vol overrides  (SYMBOL=VALUE pairs)
    avg_vol_overrides: Dict[str, float] = {}
    for entry in (args.avg_vol or []):
        try:
            sym, val = entry.split("=", 1)
            avg_vol_overrides[sym.upper()] = float(val)
        except (ValueError, AttributeError):
            print(f"ERROR: Invalid --avg-vol entry '{entry}'. Expected SYMBOL=VALUE.")
            sys.exit(1)

    print(
        f"\nBacktest  {start_date} → {end_date}"
        f"  symbols={args.symbols}"
        f"  volume_filter={'OFF' if args.no_volume_filter else 'ON'}"
    )

    results = asyncio.run(
        run_backtest(
            symbols=args.symbols,
            start_date=start_date,
            end_date=end_date,
            avg_vol_overrides=avg_vol_overrides,
            skip_volume=args.no_volume_filter,
            fetch_history_days=args.history_days,
        )
    )

    print_report(results)


if __name__ == "__main__":
    main()
