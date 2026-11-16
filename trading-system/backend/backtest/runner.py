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

    # Costs/slippage, walk-forward folds, an untouched holdout and a trade file:
    python -m backtest.runner --symbols RELIANCE INFY --start 2025-01-01 --end 2026-06-30 \\
        --slippage-bps 5 --walk-forward 4 --holdout-from 2026-04-01 --out trades.csv

How it works
------------
For each symbol and each trading day in the range:
1. 1-minute historical candles are fetched from the Groww API.
2. Each candle is replayed as 4 synthetic ticks (O→{L,H}→{H,L}→C) through the
   production TickDataStore.
3. After each candle the LIVE entry gates (``agents.signal_rules`` — the same
   functions the Scanner calls) are evaluated on the candle timestamp, with
   thresholds from Settings and the chosen market bias.
4. Each signal is simulated with the LIVE exit policy (``agents.exit_policy``):
   entry at the next bar's open, production sizing, stop / scale-out +
   breakeven / ROI decay / target / trailing / 15:00 square-off, adverse
   slippage on every fill (more on stops), stops filled at min(stop, price),
   and round-trip transaction costs per leg (``backtest.simulator``).
5. The live portfolio gates are applied on one clock (max open positions,
   daily / per-symbol trade caps, stock lock after a loss, daily drawdown halt).
6. ₹ metrics are reported: net expectancy (₹ and R) with a bootstrap CI,
   profit factor over all exits, max drawdown, worst day, halt days, Sharpe /
   Sortino; plus a slippage sensitivity table (0/5/10/20 bps), optional
   walk-forward folds and an untouched holdout period.

Limitations
-----------
- LLM decisions and the decision engine's deterministic pre-checks are NOT
  replayed (they need the market brief / Redis state).  Results describe the
  scanner + exit policy, not the full pipeline.
- Indicators are computed on 4 synthetic ticks per candle; live they are
  computed on real ticks, so tick-level RSI / VWAP windows differ (audit V3).
- The NIFTY filter uses index candles; the daily EMA21 filter uses prior
  closes from the fetched history.
- The 30-calendar-day Groww API limit is handled by auto-chunking.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

# ── path setup so we can import from the backend package ──────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from agents import signal_rules
from agents.scanner import TickDataStore          # reuse production indicator code
from backtest.simulator import (
    SENSITIVITY_BPS,
    SignalCandidate,
    SimTrade,
    SlippageModel,
    apply_portfolio_rules,
    compute_metrics,
    simulate_trade,
    split_holdout,
    trades_frame,
    walk_forward_folds,
)
from core.config import get_settings
from integrations.groww_client import get_groww_client, _retry_sync

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")

# Signal gates, thresholds, sizing and exits come from the live code
# (agents.signal_rules / agents.exit_policy) and Settings — not copied here.
SESSION_OPEN_MIN          = 9 * 60 + 15    # 9:15 AM in minutes-from-midnight
SESSION_TOTAL_MIN         = 375             # 9:15 → 15:30 = 375 min
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


# ── Signal condition check (shared with the live Scanner) ─────────────────────

def check_signal(
    store: BacktestTickDataStore,
    candle_ts: datetime,
    last_signal_ts: Optional[datetime],
    skip_volume: bool,
    nifty_open: float = 0.0,
    nifty_ltp: float = 0.0,
    bias: str = "NEUTRAL",
    settings=None,
) -> Optional[signal_rules.EntryEvaluation]:
    """Evaluate the live entry gates (``agents.signal_rules``) at the candle
    timestamp.  Returns the passing evaluation, or None when any gate rejects.

    Only the live-only OHLCV freshness gate is omitted (replayed data is
    always fresh).
    """
    settings = settings or get_settings()
    if signal_rules.pre_gate(store, candle_ts, last_signal_ts):
        return None
    # Inject backtest clock into the store for volume-ratio computation
    store._bt_now = candle_ts
    ev = signal_rules.evaluate_indicators(
        store, candle_ts,
        signal_rules.bias_thresholds(bias, settings),
        nifty_open=nifty_open,
        nifty_ltp=nifty_ltp,
        check_volume=not skip_volume,
    )
    return ev if ev.passed else None


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
    bias: str = "NEUTRAL",
    settings=None,
) -> List[SignalCandidate]:
    """Replay one trading day of 1-minute candles and return every signal fired
    (with the rest of the day's candles attached for simulation)."""
    if day_df.empty:
        return []
    # Multi-day trend filter: skip this entire day when the stock is below its
    # daily EMA21 (mirrors decision_engine._is_stock_uptrending).
    if not daily_uptrend:
        return []

    store = BacktestTickDataStore(symbol=symbol, avg_volume_20d=avg_volume_20d)
    results: List[SignalCandidate] = []
    last_signal_ts: Optional[datetime] = None
    cum_vol: int = 0
    day_open = float(day_df.iloc[0]["open"])

    # NIFTY macro-trend: capture the session open from the first NIFTY candle
    nifty_open: float = 0.0
    if nifty_day_df is not None and not nifty_day_df.empty:
        nifty_open = float(nifty_day_df.iloc[0]["open"])

    for _, row in day_df.iterrows():
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

        ev = check_signal(
            store=store,
            candle_ts=candle_ts,
            last_signal_ts=last_signal_ts,
            skip_volume=skip_volume,
            nifty_open=nifty_open,
            nifty_ltp=nifty_ltp,
            bias=bias,
            settings=settings,
        )
        if ev is not None:
            results.append(SignalCandidate(
                symbol=symbol, signal_ts=candle_ts, ltp=round(ev.ltp, 2),
                vwap=round(ev.vwap, 2), rsi=round(ev.rsi, 2), vol_ratio=round(ev.vol_ratio, 2),
                rsi_5m=round(ev.rsi_5m, 2) if ev.rsi_5m is not None else None,
                ema_9=round(ev.ema_9, 2), ema_21=round(ev.ema_21, 2),
                macd_hist=round(ev.macd_hist, 4), atr=round(ev.atr, 2), n_candles=ev.n_candles,
                future=day_df[day_df["timestamp"] > candle_ts].reset_index(drop=True),
            ))
            last_signal_ts = candle_ts

    return results


# ── Evaluation + reporting ────────────────────────────────────────────────────

def evaluate_candidates(
    candidates: List[SignalCandidate], settings, slip: SlippageModel,
) -> Tuple[List[SimTrade], Dict[str, int], set]:
    """Simulate every signal, then apply the live portfolio gates."""
    trades = [t for t in (simulate_trade(c, settings, slip) for c in candidates) if t is not None]
    return apply_portfolio_rules(trades, settings)


def _fmt_metrics(label: str, m: Dict) -> str:
    if not m.get("trades"):
        return f"  {label:<22} no trades"
    pf = m["profit_factor"]
    pf_s = "∞" if pf == float("inf") else f"{pf:.2f}"
    return (
        f"  {label:<22} n={m['trades']:<4} win={m['win_rate_pct']:>5.1f}%  "
        f"net=₹{m['net_pnl']:>10,.0f}  E=₹{m['expectancy_rupees']:>7,.0f} ({m['expectancy_r']:+.2f}R)  "
        f"PF={pf_s:>5}  maxDD={m['max_drawdown_pct']:.2f}%"
    )


def print_report(
    trades: List[SimTrade],
    metrics: Dict,
    rejected: Dict[str, int],
    settings,
    slip: SlippageModel,
) -> None:
    """Per-trade table, ₹ metrics and portfolio-rule rejections."""
    if not trades:
        print("\nNo trades for the specified parameters.")
        return

    header = (
        f"{'Symbol':<11} {'Date':<11} {'Sig':<6} {'Entry':<6} {'Fill':>9} {'Qty':>5} "
        f"{'SL':>9} {'Tgt':>9} {'Exit':>9} {'Reason':<13} {'Net ₹':>10} {'R':>6}"
    )
    sep = "─" * len(header)
    print(f"\n{sep}\n{header}\n{sep}")
    for t in sorted(trades, key=lambda x: x.entry_ts):
        print(
            f"{t.symbol:<11} {str(t.trade_date):<11} {t.signal_ts:%H:%M} {t.entry_ts:%H:%M}  "
            f"{t.entry_price:>9.2f} {t.qty:>5} {t.initial_sl:>9.2f} {t.initial_target:>9.2f} "
            f"{t.exit_price:>9.2f} {t.exit_reason:<13} {t.net_pnl:>+10,.0f} {t.r_multiple:>+6.2f}"
        )
    print(sep)

    print(
        f"\n{'Summary':─<60}\n"
        f"  Capital ₹{settings.total_capital:,.0f} | slippage {slip.bps:g} bps "
        f"(stops ×{slip.stop_mult:g}) | costs: NSE/broker round-trip per leg"
    )
    m = metrics
    lo, hi = m["expectancy_ci95"]
    print(f"  Trades             : {m['trades']}  (win {m['win_rate_pct']}%)")
    print(f"  Net P&L            : ₹{m['net_pnl']:,.2f}  (costs ₹{m['gross_costs']:,.2f})")
    print(f"  Expectancy         : ₹{m['expectancy_rupees']:,.2f} / trade  "
          f"[95% CI ₹{lo:,.0f} … ₹{hi:,.0f}]  = {m['expectancy_r']:+.3f} R")
    print(f"  Profit factor      : {m['profit_factor']}  (all exits)")
    print(f"  Max drawdown       : ₹{m['max_drawdown']:,.2f}  ({m['max_drawdown_pct']}% of capital)")
    print(f"  Worst / best day   : ₹{m['worst_day']:,.2f} / ₹{m['best_day']:,.2f}  over {m['days']} day(s)")
    print(f"  Days at 3% halt    : {m['halt_days']}")
    print(f"  Sharpe / Sortino   : {m['sharpe_daily_ann']} / {m['sortino_daily_ann']}  (daily, annualised)")
    print(f"  Max losing streak  : {m['max_losing_streak']}")
    print(f"  Exit reasons       : {m['exit_reasons']}")
    if rejected:
        print(f"  Portfolio rejects  : {rejected}")


def print_sensitivity(candidates: List[SignalCandidate], settings, stop_mult: float) -> None:
    """Re-simulate the same signals at 0/5/10/20 bps (audit V2)."""
    print(f"\n{'Slippage sensitivity':─<60}")
    for bps in SENSITIVITY_BPS:
        slip = SlippageModel(bps=bps, stop_mult=stop_mult)
        accepted, _, halted = evaluate_candidates(candidates, settings, slip)
        print(_fmt_metrics(f"{bps:g} bps", compute_metrics(accepted, settings, halted)))


def print_out_of_sample(
    trades: List[SimTrade], halted: set, settings, walk_forward: int, holdout_from: Optional[date],
) -> None:
    """Walk-forward folds and an untouched holdout (audit V6)."""
    if walk_forward > 1:
        print(f"\n{'Walk-forward folds':─<60}")
        for start, end in walk_forward_folds([t.trade_date for t in trades], walk_forward):
            fold = [t for t in trades if start <= t.trade_date <= end]
            print(_fmt_metrics(f"{start}→{end}", compute_metrics(fold, settings, halted)))
    if holdout_from is not None:
        ins, hold = split_holdout(trades, holdout_from)
        print(f"\n{'Holdout (do NOT tune on it)':─<60}")
        print(_fmt_metrics(f"in-sample <{holdout_from}", compute_metrics(ins, settings, halted)))
        print(_fmt_metrics(f"holdout  ≥{holdout_from}", compute_metrics(hold, settings, halted)))


def write_trades(trades: List[SimTrade], path: str) -> None:
    df = trades_frame(trades)
    if path.endswith(".parquet"):
        try:
            df.to_parquet(path, index=False)
            print(f"\n[+] Trades written to {path}")
            return
        except ImportError:
            path = path[: -len(".parquet")] + ".csv"
            print("    ⚠ pyarrow not installed — writing CSV instead")
    df.to_csv(path, index=False)
    print(f"\n[+] Trades written to {path}")


def _phase(ts: datetime) -> str:
    minutes = ts.hour * 60 + ts.minute
    if minutes < 11 * 60 + 30:
        return "EARLY (09:15-11:30)"
    if minutes < 13 * 60:
        return "DEAD ZONE (11:30-13:00)"
    if minutes < 14 * 60 + 30:
        return "RECOVERY (13:00-14:30)"
    return "LATE (14:30-15:30)"


def print_breakdowns(trades: List[SimTrade], settings, halted: set) -> None:
    """Per-symbol and per-session-phase ₹ metrics (phase = signal time)."""
    symbols = sorted({t.symbol for t in trades})
    if len(symbols) > 1:
        print(f"\n{'Per-symbol':─<60}")
        for sym in symbols:
            print(_fmt_metrics(sym, compute_metrics([t for t in trades if t.symbol == sym], settings)))
    print(f"\n{'Per-session-phase':─<60}")
    for phase in ("EARLY (09:15-11:30)", "DEAD ZONE (11:30-13:00)",
                  "RECOVERY (13:00-14:30)", "LATE (14:30-15:30)"):
        ph = [t for t in trades if _phase(t.signal_ts) == phase]
        if ph:
            print(_fmt_metrics(phase, compute_metrics(ph, settings)))


# ── Main orchestration ────────────────────────────────────────────────────────

async def run_backtest(
    symbols: List[str],
    start_date: date,
    end_date: date,
    avg_vol_overrides: Dict[str, float],
    skip_volume: bool,
    fetch_history_days: int,
    bias: str = "NEUTRAL",
    settings=None,
) -> List[SignalCandidate]:
    """Fetch candles and replay them through the live entry gates."""
    client = get_groww_client()
    groww  = await client.get_groww()

    all_results: List[SignalCandidate] = []

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
                bias=bias,
                settings=settings,
            )

            print(f"    {bd}  signals={len(day_results)}")
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
    p.add_argument(
        "--bias", default="NEUTRAL", choices=["BULLISH", "NEUTRAL", "BEARISH"],
        help="Market bias for the bias-modulated entry thresholds (default: NEUTRAL).",
    )
    p.add_argument(
        "--slippage-bps", type=float, default=5.0, metavar="BPS",
        help="Adverse slippage per market fill in basis points (default: 5).",
    )
    p.add_argument(
        "--stop-slippage-mult", type=float, default=2.0, metavar="X",
        help="Stop / gap fills slip this many times more (default: 2).",
    )
    p.add_argument(
        "--capital", type=float, default=None, metavar="RUPEES",
        help="Override TOTAL_CAPITAL for sizing and metrics (default: Settings).",
    )
    p.add_argument(
        "--walk-forward", type=int, default=0, metavar="K",
        help="Report metrics for K contiguous date folds (stability check).",
    )
    p.add_argument(
        "--holdout-from", default=None, metavar="YYYY-MM-DD",
        help="Report trades on/after this date separately as an untouched holdout.",
    )
    p.add_argument(
        "--out", default=None, metavar="PATH",
        help="Write accepted trades to CSV (or .parquet when pyarrow is installed).",
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
        f"  bias={args.bias}"
    )

    holdout_from = date.fromisoformat(args.holdout_from) if args.holdout_from else None
    settings = get_settings()
    if args.capital:
        settings = settings.model_copy(update={"total_capital": args.capital})
    slip = SlippageModel(bps=args.slippage_bps, stop_mult=args.stop_slippage_mult)

    candidates = asyncio.run(
        run_backtest(
            symbols=args.symbols,
            start_date=start_date,
            end_date=end_date,
            avg_vol_overrides=avg_vol_overrides,
            skip_volume=args.no_volume_filter,
            fetch_history_days=args.history_days,
            bias=args.bias,
            settings=settings,
        )
    )

    trades, rejected, halted = evaluate_candidates(candidates, settings, slip)
    print(f"\n[+] {len(candidates)} signal(s) → {len(trades)} trade(s) after portfolio rules")
    print_report(trades, compute_metrics(trades, settings, halted), rejected, settings, slip)
    if trades:
        print_breakdowns(trades, settings, halted)
        print_sensitivity(candidates, settings, args.stop_slippage_mult)
        print_out_of_sample(trades, halted, settings, args.walk_forward, holdout_from)
        if args.out:
            write_trades(trades, args.out)


if __name__ == "__main__":
    main()
