"""
Scanner module — Real-time tick processing and signal generation.

Subscribes to live tick data via GrowwFeed WebSocket.
Maintains a rolling in-memory OHLCV dataframe per stock using pandas.
Recalculates VWAP, RSI(14), and Volume Ratio on every new tick.
Fires a signal when all three entry conditions are met simultaneously.

GrowwFeed delivers only LTP + timestamp per tick (no volume_traded or OHLC fields).
Volume and OHLC data are supplemented via a periodic REST polling thread that calls
get_ohlc() every ~60 seconds and updates _ohlc_cache[symbol].
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from collections import deque
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd
import pytz

# ── Performance logging ──────────────────────────────────────────────────────
# Step 0: timing checkpoints log at DEBUG level with a [PERF] prefix so they
# are invisible at INFO but trivially filterable when debugging latency.
_PERF = logging.DEBUG

from core.config import get_settings
from integrations.groww_client import get_groww_client
from integrations.instrument_service import get_instrument_map, get_symbol, NIFTY50_TOKEN
from integrations.ltp_store import set_ltp
from integrations.mock_tick_generator import MockTickGenerator
from schemas.trade import ScannerSignal

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# ── GrowwFeed OHLCV supplement cache ─────────────────────────────────────────
# GrowwFeed delivers only {ltp, tsInMillis} per tick.
# _start_ohlcv_poll_loop() (async task, started in Scanner.start() for live mode)
# populates this dict every OHLCV_POLL_INTERVAL_SECS via get_ohlcv_snapshot().
# Keys = trading symbol (e.g. "RELIANCE"); values = {"open", "high", "low",
# "close", "volume"} from the latest REST snapshot.
_ohlc_cache: Dict[str, Dict[str, Any]] = {}
# Monotonic timestamp of last successful OHLCV poll per symbol (for freshness gate)
_ohlc_last_ts: Dict[str, float] = {}

OHLCV_POLL_INTERVAL_SECS: int = 60   # REST poll cadence (business logic — do not change)
MAX_OHLCV_STALENESS_SECS: int = 90   # 1.5× poll interval; wider than poll to absorb one retry

# Last reported market-data API health (None=unknown, True=forbidden, False=OK).
# Used to fire the Telegram alert only on state transitions, never every poll.
_data_api_forbidden: Optional[bool] = None


async def _update_data_api_health(fetched: int, total: int, results: List[Any]) -> None:
    """Publish Groww market-data API health to Redis and alert on transitions.

    Sets DATA_API_STATUS_KEY to FORBIDDEN when every symbol failed with a
    permanent 403 (subscription/entitlement gone), DEGRADED on partial failure,
    or OK when data is flowing.  A Telegram alert is sent only when the
    forbidden↔OK state changes, so a persistent outage is not spammed.
    """
    global _data_api_forbidden
    from datetime import datetime, timezone
    from core.redis_client import set_value
    from core.redis_keys import (
        DATA_API_STATUS_KEY, DATA_API_DETAIL_KEY, DATA_API_LAST_OK_KEY,
    )
    from integrations.groww_client import _is_permanent_groww_error
    from integrations.telegram_client import send_data_api_alert

    exceptions = [r for r in results if isinstance(r, Exception)]
    all_forbidden = (
        fetched == 0 and total > 0 and bool(exceptions)
        and all(_is_permanent_groww_error(e) for e in exceptions)
    )

    if all_forbidden:
        detail = str(exceptions[0]) if exceptions else "Access forbidden"
        await set_value(DATA_API_STATUS_KEY, "FORBIDDEN")
        await set_value(DATA_API_DETAIL_KEY, detail)
        if _data_api_forbidden is not True:
            logger.error("[Scanner] Market-data API FORBIDDEN (403) — trading effectively paused")
            try:
                await send_data_api_alert(forbidden=True, detail=detail)
            except Exception:
                pass
        _data_api_forbidden = True
        return

    # Some (or all) symbols succeeded → data group is reachable.
    status = "OK" if fetched == total else "DEGRADED"
    await set_value(DATA_API_STATUS_KEY, status)
    if fetched > 0:
        await set_value(DATA_API_LAST_OK_KEY, datetime.now(timezone.utc).isoformat())
    if _data_api_forbidden is True and fetched > 0:
        logger.info("[Scanner] Market-data API recovered — data flowing again")
        try:
            await send_data_api_alert(forbidden=False)
        except Exception:
            pass
    if fetched > 0:
        _data_api_forbidden = False


class CandleBuilder:
    """Aggregates raw ticks into OHLCV candles at a configurable interval.

    Used to build 1-minute and 5-minute candles from live tick data
    for computing candle-based indicators (EMA, MACD, ATR).
    """

    def __init__(self, interval_minutes: int = 1, max_candles: int = 200) -> None:
        self.interval_minutes = interval_minutes
        self.max_candles = max_candles
        self.candles: deque = deque(maxlen=max_candles)
        self._current: Optional[Dict[str, Any]] = None
        self._current_bucket: int = -1
        self._df_cache: Optional[pd.DataFrame] = None  # invalidated on each completed candle

    def _bucket(self, ts: datetime) -> int:
        """Map timestamp to its candle bucket (minute-of-day ÷ interval)."""
        return (ts.hour * 60 + ts.minute) // self.interval_minutes

    def add_tick(self, ts: datetime, price: float, volume_delta: float) -> None:
        """Feed a tick; automatically closes completed candles."""
        bucket = self._bucket(ts)
        if bucket != self._current_bucket:
            # New interval started — close previous candle (if any)
            if self._current is not None:
                self.candles.append(self._current)
                self._df_cache = None  # invalidate cache: a new completed candle exists
            self._current_bucket = bucket
            self._current = {
                "timestamp": ts,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume_delta,
            }
        else:
            # Update current candle
            self._current["high"] = max(self._current["high"], price)
            self._current["low"] = min(self._current["low"], price)
            self._current["close"] = price
            self._current["volume"] += volume_delta

    @property
    def completed_df(self) -> Optional[pd.DataFrame]:
        """Return completed candles as a DataFrame (excludes current partial candle).

        Result is cached until the next candle completes, so multiple indicator
        methods called during the same tick never rebuild the DataFrame twice.
        """
        if not self.candles:
            return None
        if self._df_cache is None:
            self._df_cache = pd.DataFrame(self.candles)
        return self._df_cache


class TickDataStore:
    """In-memory rolling OHLCV store for a single stock.

    Key design decisions (production hardening):
    - Tick list is capped at MAX_TICKS to prevent memory leaks over a full
      trading session (~450K raw ticks/day for 20 symbols).
    - RSI is computed on ``last_price``.
    - VWAP uses volume from the REST OHLCV poll cache (_ohlc_cache) since
      GrowwFeed does not deliver volume_traded per tick.
    - ``_open_price`` / ``_prev_close`` are seeded from the REST OHLC poll at
      session start (GrowwFeed ticks have no ohlc field).
    """

    MAX_TICKS = 1_000  # keep last N ticks (≈ 16 min @ 1 tick/sec)

    def __init__(self, symbol: str, avg_volume_20d: float = 0.0) -> None:
        self.symbol = symbol
        self.avg_volume_20d = avg_volume_20d
        self.ticks: deque = deque(maxlen=self.MAX_TICKS)
        self._df_backing: Optional[pd.DataFrame] = None  # SH1: backing store for lazy _df
        self._df_dirty: bool = False                      # SH1: True after each add_tick
        self._prev_cum_volume: float = 0.0  # track previous cumulative volume
        self._open_price: float = 0.0        # SH2: today's session open (from first tick)
        self._prev_close: float = 0.0        # SH2: previous day's close (from first tick)
        self._candles_1m = CandleBuilder(interval_minutes=1, max_candles=200)
        self._candles_5m = CandleBuilder(interval_minutes=5, max_candles=100)

    def add_tick(self, tick: Dict[str, Any]) -> None:
        """Append a tick (capped at MAX_TICKS) and mark the dataframe stale."""
        # GrowwFeed delivers {ltp, tsInMillis}. Volume comes from the REST poll cache.
        symbol = tick.get("tradingsymbol", self.symbol)
        # _ohlc_cache is a module-level dict in this same file; no import needed
        cached_ohlc = _ohlc_cache.get(symbol, {})
        # GrowwFeed: volume comes from REST poll cache. Fallback to tick["volume_traded"]
        # for mock ticks and tests that don't populate the cache.
        cum_vol = cached_ohlc.get("volume") or tick.get("volume_traded", 0)
        # Volume delta: treat as 0 on first tick to avoid false spikes on mid-day restart.
        if self._prev_cum_volume == 0:
            vol_delta = 0
        else:
            vol_delta = max(0, cum_vol - self._prev_cum_volume)
        self._prev_cum_volume = cum_vol

        # GrowwFeed uses tsInMillis; fall back to exchange_timestamp (mock/test)
        # or now() if neither is present.
        ts_ms = tick.get("tsInMillis")
        if ts_ms:
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=IST)
        else:
            ts_raw = tick.get("exchange_timestamp")
            if ts_raw is not None:
                ts = ts_raw if ts_raw.tzinfo else IST.localize(ts_raw)
            else:
                ts = datetime.now(IST)
        price = tick.get("ltp") or tick.get("last_price", 0.0)

        # SH2: Seed open/prev_close from the REST OHLC poll cache on first tick,
        # or from tick["ohlc"] (mock/test format).
        tick_ohlc = tick.get("ohlc", {})
        if self._open_price == 0.0:
            open_price = float(cached_ohlc.get("open") or tick_ohlc.get("open", 0.0))
            prev_close = float(cached_ohlc.get("close") or tick_ohlc.get("close", 0.0))
            if open_price > 0:
                self._open_price = open_price
            if prev_close > 0:
                self._prev_close = prev_close

        self.ticks.append({
            "timestamp": ts,
            "last_price": price,
            "volume_delta": vol_delta,
            "cum_volume": cum_vol,
            "high": cached_ohlc.get("high") or tick_ohlc.get("high", 0.0),
            "low": cached_ohlc.get("low") or tick_ohlc.get("low", 0.0),
            "open": cached_ohlc.get("open") or tick_ohlc.get("open", 0.0),
            "close": cached_ohlc.get("close") or tick_ohlc.get("close", 0.0),
        })
        # SH1: Mark df stale instead of rebuilding on every tick.
        # pd.DataFrame(deque) is O(N); rebuilding eagerly 20–40×/sec on the
        # WebSocket thread causes backpressure.  The _df property rebuilds
        # lazily on the first read per tick (first indicator call, or direct
        # _df access in tests).
        self._df_dirty = True

        # Feed candle builders for higher-level indicators
        self._candles_1m.add_tick(ts, price, vol_delta)
        self._candles_5m.add_tick(ts, price, vol_delta)

    @property
    def _df(self) -> Optional[pd.DataFrame]:
        """Tick DataFrame, rebuilt lazily when the backing store is stale."""
        if self._df_dirty or self._df_backing is None:
            self._df_backing = pd.DataFrame(self.ticks) if self.ticks else pd.DataFrame()
            self._df_dirty = False
        return self._df_backing

    def compute_vwap(self) -> float:
        """Calculate intraday VWAP using per-tick volume deltas.

        Uses last_price directly. Volume comes from the REST OHLCV poll cache
        (GrowwFeed does not deliver volume_traded). Volume deltas are computed
        from the cached cumulative volume between REST poll intervals.
        """
        if self._df is None or self._df.empty:
            return 0.0
        df = self._df
        cumulative_tp_vol = (df["last_price"] * df["volume_delta"]).cumsum()
        cumulative_vol = df["volume_delta"].cumsum()
        vwap_series = cumulative_tp_vol / cumulative_vol.replace(0, np.nan)
        last_val = vwap_series.iloc[-1] if not vwap_series.empty else 0.0
        # NaN arises when all volume deltas are zero (e.g. single baseline tick)
        return float(last_val) if not np.isnan(last_val) else 0.0

    def compute_rsi(self, period: int = 14) -> float:
        """Calculate RSI(14) using Wilder's smoothing on last_price."""
        if self._df is None or len(self._df) < period + 1:
            return 50.0  # neutral default until we have enough ticks
        price = self._df["last_price"]
        delta = price.diff()
        gains = delta.clip(lower=0)
        losses = (-delta).clip(lower=0)
        # Wilder's smoothing = EWM with alpha = 1/period, no bias correction
        avg_gain = gains.ewm(alpha=1.0 / period, adjust=False).mean()
        avg_loss = losses.ewm(alpha=1.0 / period, adjust=False).mean()
        last_loss = avg_loss.iloc[-1]
        if last_loss == 0:
            return 100.0
        rs = avg_gain.iloc[-1] / last_loss
        return float(100.0 - (100.0 / (1.0 + rs)))

    def compute_volume_ratio(self) -> float:
        """Calculate current cumulative volume relative to *prorated* 20-day average.

        The average is prorated by the fraction of the trading session elapsed.
        At 10:00 AM (~45 min into the 375-min session), a stock trading at
        'normal' pace reads ~1.0 — not ~0.12 as the raw ratio would show.

        Returns 0.0 when avg_volume_20d is not yet initialised (unknown).
        """
        if self._df is None or self._df.empty or self.avg_volume_20d <= 0:
            return 0.0
        current_volume = float(self._df["cum_volume"].iloc[-1])
        # Prorate: 9:15-15:30 = 375 minutes
        now = datetime.now(IST)
        minutes_since_open = max(1, (now.hour * 60 + now.minute) - (9 * 60 + 15))
        total_session_minutes = 375
        session_fraction = min(1.0, minutes_since_open / total_session_minutes)
        prorated_avg = self.avg_volume_20d * session_fraction
        return current_volume / prorated_avg if prorated_avg > 0 else 0.0

    # ── Candle-based indicators ────────────────────────────────────────

    def compute_ema(self, period: int = 9) -> float:
        """EMA of 1-minute candle close prices."""
        df = self._candles_1m.completed_df
        if df is None or len(df) < period:
            return 0.0
        return float(df["close"].ewm(span=period, adjust=False).mean().iloc[-1])

    def compute_macd_histogram(self) -> float:
        """MACD histogram (MACD line - signal line) from 1-minute candle closes.

        MACD(12, 26, 9): fast EMA(12) - slow EMA(26), signal EMA(9) of MACD.
        Positive histogram = bullish momentum.
        """
        df = self._candles_1m.completed_df
        if df is None or len(df) < 26:
            return 0.0
        close = df["close"]
        ema_12 = close.ewm(span=12, adjust=False).mean()
        ema_26 = close.ewm(span=26, adjust=False).mean()
        macd_line = ema_12 - ema_26
        signal_line = macd_line.ewm(span=9, adjust=False).mean()
        histogram = macd_line - signal_line
        return float(histogram.iloc[-1])

    def compute_atr(self, period: int = 14) -> float:
        """ATR from 1-minute candle OHLC data using Wilder's smoothing.

        True Range = max(High-Low, |High-prevClose|, |Low-prevClose|).
        ATR = EWM of TR with alpha=1/period (Wilder's smoothing).
        """
        df = self._candles_1m.completed_df
        if df is None or len(df) < period + 1:
            return 0.0
        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
        return float(atr.iloc[-1])

    def compute_rsi_htf(self, period: int = 14) -> Optional[float]:
        """RSI from 5-minute candle close prices (higher timeframe filter).

        Returns None if not enough data — caller should skip the HTF check.
        """
        df = self._candles_5m.completed_df
        if df is None or len(df) < period + 1:
            return None
        price = df["close"]
        delta = price.diff()
        gains = delta.clip(lower=0)
        losses = (-delta).clip(lower=0)
        avg_gain = gains.ewm(alpha=1.0 / period, adjust=False).mean()
        avg_loss = losses.ewm(alpha=1.0 / period, adjust=False).mean()
        last_loss = avg_loss.iloc[-1]
        if last_loss == 0:
            return 100.0
        rs = avg_gain.iloc[-1] / last_loss
        return float(100.0 - (100.0 / (1.0 + rs)))

    @property
    def ltp(self) -> float:
        """Last traded price."""
        if self._df is None or self._df.empty:
            return 0.0
        return float(self._df["last_price"].iloc[-1])


class Scanner:
    """
    Scans live tick data and emits signals when all three entry conditions
    are met: Price > VWAP, RSI between 45-65, Volume > 1.5x average.
    """

    # How long to suppress repeat signals for the same stock (seconds)
    SIGNAL_COOLDOWN_SECS = 1800  # 30 minutes
    # Hard cutoff — no new signals after this time (HH, MM)
    SIGNAL_CUTOFF = (15, 15)    # 3:15 PM IST
    # Minimum ticks before indicators are statistically meaningful
    MIN_TICKS_FOR_SIGNAL = 50
    # Minimum completed 1-minute candles for candle-based indicators
    MIN_CANDLES_FOR_INDICATORS = 15

    def __init__(self, signal_queue: asyncio.Queue) -> None:
        self._settings = get_settings()
        self._signal_queue = signal_queue
        self._stores: Dict[str, TickDataStore] = {}
        self._signal_cooldown: Dict[str, datetime] = {}  # symbol → last signal time
        self._running = False
        self._mock_generator: Optional[MockTickGenerator] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None  # for thread-safe queue access
        # A2: Intraday NIFTY 50 trend state — updated from index ticks on every tick batch.
        # Suppresses long signals when NIFTY drifts below nifty_trend_filter_pct from open.
        self._nifty_open_price: float = 0.0
        self._nifty_ltp: float = 0.0
        # Market bias (BEARISH / NEUTRAL / BULLISH) — updated by set_market_bias() when
        # the research agent publishes a new brief.  Used to modulate signal thresholds.
        self._market_bias: str = "NEUTRAL"

    def set_market_bias(self, bias: str) -> None:
        """Update the cached market bias (called by TradingAgent when a new brief arrives)."""
        self._market_bias = bias.upper()
        logger.info("[Scanner] Market bias updated to %s", self._market_bias)

    def _calculate_suggested_qty(self, ltp: float, atr: float = 0.0, n_candles: int = 0) -> int:
        """
        Calculate position size.

        With ATR (≥45 completed 1m candles): qty = max_loss / (atr × sl_multiplier)
        — volatility-aware sizing.  The 45-candle minimum (≈45 min of session)
        avoids the noisy opening-range ATR, which can run 2–3× the historical
        daily ATR in the first 30 minutes, causing dangerous over- or under-sizing.

        Without sufficient candles (or ATR=0): qty = max_loss / (ltp × stop_loss_pct)
        — fixed-percentage fallback.

        Hard capital cap (A1 fix): the risk formula alone can produce notional values
        of 3–9× total capital when ATR is tight relative to the max-loss budget (e.g.
        RELIANCE at ₹2,800 with 1m ATR ₹8 → 625 shares → ₹17.5L on ₹5L capital).
        The cap clamps qty so that qty × ltp ≤ total_capital / max_open_positions,
        keeping every position within its designated capital envelope.
        """
        settings = self._settings
        max_loss = settings.total_capital * settings.max_loss_per_trade_pct
        # SH3: require ≥45 completed 1m candles before trusting the intraday ATR
        if atr > 0 and n_candles >= 45:
            sl_distance = atr * settings.atr_sl_multiplier
        else:
            sl_distance = ltp * settings.stop_loss_pct
        if sl_distance <= 0:
            return 0

        qty_from_risk = max(1, int(max_loss / sl_distance))

        # A1: Hard capital cap — each position must fit within its capital envelope.
        # Without this, the risk formula produces notional values far exceeding
        # available capital, resulting in Groww margin rejections and silent order loss.
        if ltp > 0 and settings.max_open_positions > 0:
            max_notional_per_position = settings.total_capital / settings.max_open_positions
            qty_from_capital = max(1, int(max_notional_per_position / ltp))
            return min(qty_from_risk, qty_from_capital)

        return qty_from_risk

    def _check_signal(self, store: TickDataStore) -> Optional[ScannerSignal]:
        """Check if all entry conditions are met for a given stock.

        Conditions (all must pass):
         1. Price > VWAP
         2. RSI(14) tick-level between 45–65
            Note: DecisionEngine accepts RSI 40–72.  The Scanner uses a tighter
            band (45–65) as a conservative first-pass filter.  Signals in the
            40–44 and 66–72 RSI zones are intentionally not generated here —
            they would pass the DecisionEngine but the scanner pre-filters them
            out.  This two-layer design is deliberate. (SM1)
         3. Volume > 1.5× 20-day average (skipped in paper mode where avg=0)
         4. EMA(9) > EMA(21) on 1m candles (trend filter) — requires ≥35 candles
         5. MACD histogram > 0 on 1m candles (momentum confirmation) — ≥35 candles
            Both candle checks share the same ≥35 minimum so MACD is never
            silently skipped while EMA is already evaluated. (SM2)
         6. 5-min RSI between 45–72 (higher timeframe filter, when available)
            Tightened from (35–70): a 5m RSI of 35–44 indicates a clearly bearish
            higher-timeframe context that the original band would have passed. (SH4)
         7. Gap-at-open < gap_filter_pct: reject when stock gapped up too far vs
            yesterday's close, since VWAP above-price check alone cannot catch
            this risk (VWAP resets each session). (SH2)
        """
        now_ist = datetime.now(IST)

        # Hard market-hours cutoff: no new signals after 3:15 PM IST
        cutoff_h, cutoff_m = self.SIGNAL_CUTOFF
        if (now_ist.hour, now_ist.minute) >= (cutoff_h, cutoff_m):
            return None

        # SM5: On Fridays, stop generating signals after 14:00 IST so the
        # DecisionEngine never even calls the LLM on soon-to-expire positions.
        # (The DecisionEngine also rejects Friday-14:00 signals, but filtering
        # here prevents wasteful queue entries and LLM call avoidance work.)
        if now_ist.weekday() == 4 and now_ist.hour >= 14:
            return None

        # Require minimum data points for statistically meaningful indicators
        if len(store.ticks) < self.MIN_TICKS_FOR_SIGNAL:
            return None

        # Require minimum completed 1m candles for candle-based indicators.
        candles_1m_df = store._candles_1m.completed_df
        n_candles = len(candles_1m_df) if candles_1m_df is not None else 0
        if n_candles < self.MIN_CANDLES_FOR_INDICATORS:
            return None

        # Per-symbol cooldown: suppress repeats within SIGNAL_COOLDOWN_SECS
        last_fired = self._signal_cooldown.get(store.symbol)
        if last_fired is not None:
            elapsed = (now_ist - last_fired).total_seconds()
            if elapsed < self.SIGNAL_COOLDOWN_SECS:
                return None

        # ── BN1: OHLCV freshness gate ────────────────────────────────────────────
        # When GrowwFeed is active (credentials present), skip signal generation
        # when OHLCV data is stale.  A stale VWAP against a current LTP produces
        # false readings which can trigger signals on outdated volumes.
        # When MockTickGenerator is active (no credentials), OHLC is injected
        # per tick via tick["ohlc"] so the cache is always fresh — skip gate.
        if self._settings.groww_client_id:
            ts = _ohlc_last_ts.get(store.symbol, 0.0)
            if ts == 0.0 or (time.monotonic() - ts) > MAX_OHLCV_STALENESS_SECS:
                logger.debug(
                    "[Scanner] %s OHLCV stale (last=%.0fs ago) — skipping signal",
                    store.symbol,
                    time.monotonic() - ts if ts > 0.0 else -1,
                )
                return None
        _t4_start = time.perf_counter_ns()
        ltp = store.ltp
        vwap = store.compute_vwap()
        rsi = store.compute_rsi()
        vol_ratio = store.compute_volume_ratio()


        # A2: Intraday NIFTY 50 macro trend filter.
        # Suppress all long signals when NIFTY has drifted below the configured
        # threshold from its session open.  An individual stock can satisfy all
        # technical conditions while NIFTY is in a broad intraday downtrend — the
        # stock-level indicators cannot see this because VWAP and RSI both reset
        # at the session open and carry no information about index direction.
        # In paper mode or when NIFTY data hasn't arrived yet, skip the check
        # (open=0 means no tick received) to avoid blocking all signals at startup.
        #
        # Bias modulation: on bullish days widen the threshold so the filter only
        # triggers on genuinely severe index drops; on bearish days tighten it so
        # even a mild drift suppresses long entries.
        #   BULLISH: -0.8%   NEUTRAL: -0.5%   BEARISH: -0.3%
        _nifty_thresh = {
            "BULLISH": -0.008,
            "NEUTRAL": self._settings.nifty_trend_filter_pct,   # -0.005 from config
            "BEARISH": -0.003,
        }.get(self._market_bias, self._settings.nifty_trend_filter_pct)
        if self._nifty_open_price > 0 and self._nifty_ltp > 0:
            nifty_drift = (self._nifty_ltp - self._nifty_open_price) / self._nifty_open_price
            if nifty_drift < _nifty_thresh:
                logger.debug(
                    "[Scanner] %s REJECT NIFTY-TREND drift=%.3f%% < threshold=%.3f%% (%s bias) — suppressing long signals",
                    store.symbol, nifty_drift * 100, _nifty_thresh * 100, self._market_bias,
                )
                return None

        # Condition 1: Price > VWAP
        if ltp <= vwap:
            logger.debug("[Scanner] %s REJECT C1 ltp=%.2f <= vwap=%.2f", store.symbol, ltp, vwap)
            return None
        # Condition 2: RSI between bias-modulated band (scanner pre-filter; see docstring)
        #   BULLISH: 42–68   NEUTRAL: 45–65   BEARISH: 48–63
        _rsi_lo, _rsi_hi = {
            "BULLISH": (45.0, 68.0),
            "NEUTRAL": (50.0, 65.0),
            "BEARISH": (50.0, 63.0),
        }.get(self._market_bias, (50.0, 65.0))
        if not (_rsi_lo <= rsi <= _rsi_hi):
            logger.debug(
                "[Scanner] %s REJECT C2 rsi=%.1f not in [%.0f,%.0f] bias=%s",
                store.symbol, rsi, _rsi_lo, _rsi_hi, self._market_bias,
            )
            return None
        # Early-session tighter RSI (9:15–9:44 IST).
        # Indicators warm up over the first 15–30 candles; momentum is noisiest
        # right after open.  A ±5-point tighter band rejects borderline entries
        # before the market establishes its intraday direction.
        if now_ist.hour == 9 and now_ist.minute < 45:
            _early_lo, _early_hi = _rsi_lo + 5, _rsi_hi - 5
            if not (_early_lo <= rsi <= _early_hi):
                logger.debug(
                    "[Scanner] %s REJECT EARLY-RSI rsi=%.1f not in tight band [%.0f,%.0f] (pre-9:45)",
                    store.symbol, rsi, _early_lo, _early_hi,
                )
                return None
        # Condition 3: Volume > bias-modulated minimum (skip when historical data unavailable)
        #   BULLISH: 1.3×   NEUTRAL: 1.5×   BEARISH: 2.0×
        _vol_min = {
            "BULLISH": 1.3,
            "NEUTRAL": 1.5,
            "BEARISH": 2.0,
        }.get(self._market_bias, 1.5)
        if store.avg_volume_20d > 0 and vol_ratio < _vol_min:
            logger.debug(
                "[Scanner] %s REJECT C3 vol_ratio=%.2f < %.1fx bias=%s",
                store.symbol, vol_ratio, _vol_min, self._market_bias,
            )
            return None

        # Condition 7: Gap-at-open filter (SH2)
        # Reject signals when the stock gapped up beyond the configured threshold
        # vs the previous session's close.  Stocks already extended at open are
        # prone to intraday mean-reversion even when momentum indicators look green.
        # Bias modulation: on bearish days lower the gap tolerance (less room for
        # extended stocks); on bullish days relax it slightly.
        #   BULLISH: 2.0%   NEUTRAL: 1.5%   BEARISH: 1.0%
        _gap_max = {
            "BULLISH": 2.0,
            "NEUTRAL": self._settings.gap_filter_pct,   # 1.5 from config
            "BEARISH": 1.0,
        }.get(self._market_bias, self._settings.gap_filter_pct)
        if store._prev_close > 0 and store._open_price > 0:
            gap_pct = (store._open_price - store._prev_close) / store._prev_close * 100
            if gap_pct > _gap_max:
                logger.debug(
                    "[Scanner] %s REJECT C7 gap_pct=%.2f%% > max %.1f%% bias=%s",
                    store.symbol, gap_pct, _gap_max, self._market_bias,
                )
                return None

        # ── Candle-based indicators (1m) ───────────
        ema_9 = store.compute_ema(9)
        ema_21 = store.compute_ema(21)
        macd_hist = store.compute_macd_histogram()
        atr = store.compute_atr()
        _t4_end = time.perf_counter_ns()
        logger.log(_PERF, "[PERF T3→T4] all indicators %s: %.0f μs",
                   store.symbol, (_t4_end - _t4_start) / 1_000)
        # Stock-level intraday trend bias: suppress long signals when price is
        # >1% below EMA(21).  Complements the NIFTY macro filter (A2 above) by
        # catching single-stock downtrends on mixed or bullish market days
        # (e.g. TATASTEEL falling while NIFTY is flat).  Safe below 21 candles
        # because compute_ema() returns 0.0 and the ema_21 > 0 guard skips it.
        if ema_21 > 0 and ltp < ema_21 * 0.99:
            logger.debug(
                "[Scanner] %s REJECT EMA21-TREND ltp=%.2f < ema21*0.99=%.2f",
                store.symbol, ltp, ema_21 * 0.99,
            )
            return None
        # Conditions 4 + 5: EMA trend and MACD momentum — enforced from ≥26 candles
        # (MACD minimum) so the filter is active from ~9:41 AM rather than ~9:50 AM.
        # Both checks share the same threshold so MACD is never silently skipped
        # while EMA is already being evaluated.  (SM2)
        if n_candles >= 26:
            if ema_9 > 0 and ema_21 > 0 and ema_9 <= ema_21:
                logger.debug(
                    "[Scanner] %s REJECT C4 ema9=%.2f <= ema21=%.2f",
                    store.symbol, ema_9, ema_21,
                )
                return None
            if macd_hist <= 0:
                logger.debug(
                    "[Scanner] %s REJECT C5 macd_hist=%.4f", store.symbol, macd_hist
                )
                return None

        # ── Higher timeframe filter (5m) ───────────
        rsi_5m = store.compute_rsi_htf()
        # Condition 6: 5-min RSI in neutral-bullish range (45–72, tightened from 35–70)
        if rsi_5m is not None and (rsi_5m < 45 or rsi_5m > 72):
            logger.debug(
                "[Scanner] %s REJECT C6 rsi_5m=%.1f not in [45,72]", store.symbol, rsi_5m
            )
            return None

        # Record cooldown timestamp before returning
        self._signal_cooldown[store.symbol] = now_ist

        signal = ScannerSignal(
            stock=store.symbol,
            exchange="NSE",
            signal_time=now_ist.strftime("%H:%M:%S"),
            ltp=ltp,
            vwap=round(vwap, 2),
            rsi=round(rsi, 2),
            volume_ratio=round(vol_ratio, 2),
            suggested_qty=self._calculate_suggested_qty(ltp, atr, n_candles),
            ema_9=round(ema_9, 2) if ema_9 else None,
            ema_21=round(ema_21, 2) if ema_21 else None,
            macd_histogram=round(macd_hist, 4) if macd_hist else None,
            atr=round(atr, 2) if atr else None,
            rsi_5m=round(rsi_5m, 2) if rsi_5m is not None else None,
        )
        logger.info(
            "🔔 Signal: %s LTP=%.2f VWAP=%.2f RSI=%.2f VolR=%.2f "
            "EMA9=%.2f EMA21=%.2f MACD=%.4f ATR=%.2f",
            store.symbol, ltp, vwap, rsi, vol_ratio,
            ema_9, ema_21, macd_hist, atr,
        )
        return signal

    def _on_ticks(self, ws, ticks: List[Dict[str, Any]]) -> None:
        """Callback invoked by GrowwFeed or MockTickGenerator on each tick batch."""
        logger.log(
            _PERF, "[Scanner] _on_ticks: %d tick(s) received", len(ticks)
        )
        for tick in ticks:
            # ── P1: tick entry ────────────────────────────────────────────────
            _p1 = time.perf_counter_ns()

            token = tick.get("instrument_token") or tick.get("exchange_token")

            # A2: Intercept NIFTY 50 index ticks to maintain the macro trend state.
            # The index is subscribed in _on_connect() but has no TickDataStore.
            if token == NIFTY50_TOKEN:
                price = tick.get("ltp") or tick.get("last_price", 0.0)
                if price > 0:
                    self._nifty_ltp = price
                    cached = _ohlc_cache.get("NIFTY 50", {})
                    if self._nifty_open_price == 0.0 and cached.get("open", 0.0) > 0:
                        self._nifty_open_price = float(cached["open"])
                        logger.info(
                            "[Scanner] NIFTY 50 open price captured: ₹%.2f", self._nifty_open_price
                        )
                continue

            symbol = get_symbol(token) if token else None
            if symbol is None:
                continue

            # ── P2: LTP cache write — MUST be first, before any other processing.
            # The Risk Manager's 5-second poll reads from this cache for stop-loss
            # monitoring.  Any overhead before this write delays SL detection.
            ltp = tick.get("ltp") or tick.get("last_price", 0.0)
            set_ltp(symbol, ltp)
            _p2 = time.perf_counter_ns()
            logger.log(_PERF, "[PERF P1→P2] LTP cache write %s: %.0f ns", symbol, _p2 - _p1)

            if symbol not in self._stores:
                if not self._settings.paper_trading:
                    logger.warning(
                        "[Scanner] Tick for unexpected symbol %s (token=%s) — "
                        "creating store with no historical volume data",
                        symbol, token,
                    )
                self._stores[symbol] = TickDataStore(symbol)
            store = self._stores[symbol]

            _t3_start = time.perf_counter_ns()
            store.add_tick(tick)
            _t3_end = time.perf_counter_ns()
            logger.log(_PERF, "[PERF T3] candle construction %s: %.0f ns", symbol, _t3_end - _t3_start)

            _t5_start = time.perf_counter_ns()
            signal = self._check_signal(store)
            _t5_end = time.perf_counter_ns()
            logger.log(_PERF, "[PERF T3→T5] signal check %s: %.0f μs", symbol, (_t5_end - _t5_start) / 1_000)

            if signal is not None:
                _t6_start = time.perf_counter_ns()
                try:
                    # asyncio.Queue is NOT thread-safe.  In live mode _on_ticks
                    # is called from GrowwFeed's WebSocket thread, so we must
                    # schedule the put on the event loop.  In paper mode the
                    # callback runs on the event loop already, so both paths
                    # are safe.
                    if self._loop is not None:
                        self._loop.call_soon_threadsafe(
                            self._signal_queue.put_nowait, signal
                        )
                    else:
                        self._signal_queue.put_nowait(signal)
                    logger.log(_PERF, "[PERF T6] signal queued %s: %.0f ns",
                               symbol, time.perf_counter_ns() - _t6_start)
                except asyncio.QueueFull:
                    logger.warning("Signal queue full — dropping signal for %s", symbol)

    async def _start_ohlcv_poll_loop(self) -> None:
        """Periodic OHLCV supplementation task for live mode.

        GrowwFeed delivers only {ltp, tsInMillis} per tick.  Volume, open, high,
        low, close are only available via REST.  This loop fetches all tracked
        symbols in parallel every OHLCV_POLL_INTERVAL_SECS and writes results
        into module-level _ohlc_cache and _ohlc_last_ts.

        _check_signal() gates on _ohlc_last_ts to avoid stale VWAP / volume
        readings: if the last successful poll is older than MAX_OHLCV_STALENESS_SECS
        the signal check is skipped entirely for that symbol.

        Uses asyncio.gather() so all symbols are fetched in one concurrent batch
        (O(1 RTT) instead of O(N RTTs) for N symbols).
        """
        groww = get_groww_client()
        symbols = list(get_instrument_map().keys())
        while self._running:
            _t1 = time.perf_counter_ns()
            try:
                results = await asyncio.gather(
                    *[groww.get_ohlcv_snapshot(sym) for sym in symbols],
                    return_exceptions=True,
                )
                _t2 = time.perf_counter_ns()
                fetched = 0
                for sym, result in zip(symbols, results):
                    if isinstance(result, Exception):
                        logger.warning(
                            "[Scanner] OHLCV poll failed for %s: %s (%s)",
                            sym, result, type(result).__name__,
                        )
                        continue
                    if result:  # empty dict in paper mode — nothing to cache
                        _ohlc_cache[sym] = result
                        _ohlc_last_ts[sym] = time.monotonic()
                        fetched += 1
                        logger.log(
                            _PERF,
                            "[Scanner] OHLCV %s: O=%.2f H=%.2f L=%.2f C=%.2f V=%d",
                            sym,
                            result.get("open", 0), result.get("high", 0),
                            result.get("low", 0), result.get("close", 0),
                            result.get("volume", 0),
                        )
                logger.log(
                    _PERF,
                    "[PERF T1→T2] OHLCV REST poll: %d/%d symbols %.1f ms",
                    fetched, len(symbols), (_t2 - _t1) / 1_000_000,
                )
                if fetched < len(symbols):
                    logger.warning(
                        "[Scanner] OHLCV poll: only %d/%d symbols updated "
                        "(check per-symbol warnings above)",
                        fetched, len(symbols),
                    )
                else:
                    logger.debug("[Scanner] OHLCV poll: all %d symbols updated", fetched)
                # Publish market-data API health (alerts on forbidden↔OK transitions).
                await _update_data_api_health(fetched, len(symbols), results)
            except Exception as exc:
                logger.error("[Scanner] OHLCV poll loop exception: %s", exc, exc_info=True)
            await asyncio.sleep(OHLCV_POLL_INTERVAL_SECS)

    async def _load_avg_volumes(self) -> None:
        """Fetch 20-day average daily volume for every focus stock before session start.

        Pre-populates TickDataStore instances so the volume filter (Condition 3) is
        correctly enforced from the very first signal check.  Without this, all stores
        default to avg_volume_20d=0 and the volume check is silently bypassed for the
        entire session.  (SC1)

        Best-effort: symbols whose historical fetch fails are initialised with 0.0
        (volume check bypassed only for that symbol, a warning is logged).

        All fetches run concurrently (asyncio.gather) with a per-symbol timeout so a
        rate-limit or API-forbidden response on any symbol does not block the entire
        pre-load phase.  Without concurrency, 49 sequential retries × ~14 s each
        = ~11 minutes before GrowwFeed ever connects.
        """
        groww_client = get_groww_client()
        instrument_map = get_instrument_map()

        # Per-symbol timeout: cap at one full retry cycle (2+4+8 = 14 s + margin).
        # asyncio.wait_for cancels the to_thread Future; the background thread may
        # finish later but the scanner proceeds immediately with avg_vol=0.
        _FETCH_TIMEOUT = 20.0

        async def _fetch_one(symbol: str, token: int):
            try:
                candles = await asyncio.wait_for(
                    groww_client.get_historical_data(token, interval="day", days_back=30),
                    timeout=_FETCH_TIMEOUT,
                )
                volumes = [c["volume"] for c in candles if c.get("volume", 0) > 0]
                recent = volumes[-20:] if len(volumes) >= 20 else volumes
                avg_vol = sum(recent) / len(recent) if recent else 0.0
                if avg_vol > 0:
                    logger.debug("[Scanner] %s avg_volume_20d=%.0f", symbol, avg_vol)
                else:
                    logger.warning("[Scanner] %s: no volume data in historical candles", symbol)
                return symbol, avg_vol
            except asyncio.TimeoutError:
                logger.warning(
                    "[Scanner] Could not fetch avg volume for %s: timed out after %.0fs "
                    "— volume filter disabled for this symbol",
                    symbol, _FETCH_TIMEOUT,
                )
                return symbol, 0.0
            except Exception as exc:
                logger.warning(
                    "[Scanner] Could not fetch avg volume for %s: %s "
                    "— volume filter disabled for this symbol",
                    symbol, exc,
                )
                return symbol, 0.0

        results = await asyncio.gather(
            *[_fetch_one(sym, tok) for sym, tok in instrument_map.items()]
        )
        loaded = 0
        for symbol, avg_vol in results:
            self._stores[symbol] = TickDataStore(symbol, avg_volume_20d=avg_vol)
            if avg_vol > 0:
                loaded += 1
        logger.info(
            "[Scanner] Loaded 20-day avg volumes for %d/%d symbols",
            loaded, len(instrument_map),
        )

    def _on_connect(self, ws, response) -> None:
        """Subscribe to instruments on WebSocket connect."""
        tokens = list(get_instrument_map().values())
        # A2: Always subscribe to the NIFTY 50 index for the intraday trend filter.
        if NIFTY50_TOKEN not in tokens:
            tokens.append(NIFTY50_TOKEN)
        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
            from integrations.instrument_service import get_symbol as _gs
            token_labels = [
                f"{_gs(t) or 'NIFTY50'}={t}" for t in tokens
            ]
            logger.info(
                "[Scanner] GrowwFeed connected — subscribed %d instruments: %s",
                len(tokens), ", ".join(token_labels),
            )

        # Record the feed-connected timestamp in Redis so the dashboard can
        # compute candle warmup progress (15 candles ≈ 15 min from this point).
        try:
            from datetime import datetime, timezone
            from core.redis_client import set_value
            from core.redis_keys import SCANNER_FEED_CONNECTED_AT_KEY
            ts = datetime.now(timezone.utc).isoformat()
            if self._loop is not None:
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    set_value(SCANNER_FEED_CONNECTED_AT_KEY, ts, ttl=86400),
                    self._loop,
                )
        except Exception:
            pass  # non-critical — warmup indicator is informational only

    def _on_close(self, ws, code, reason) -> None:
        logger.warning(
            "[Scanner] GrowwFeed connection closed: code=%s reason=%s",
            code, reason,
        )
        self._running = False

    def _on_error(self, ws, code, reason) -> None:
        logger.error(
            "[Scanner] GrowwFeed error: code=%s reason=%s (type=%s)",
            code, reason, type(reason).__name__ if reason is not None else "None",
        )

    async def start(self) -> None:
        """Start the scanner.

        Data feed selection is based on whether real Groww credentials are
        configured, NOT on the paper_trading flag:

          • Credentials absent  → MockTickGenerator (offline dev / CI with no
                                   market access).  paper_trading may be True or
                                   False; the mock is the only data source
                                   available in this environment.
          • Credentials present → GrowwFeed WebSocket (real market ticks).
                                   This applies to BOTH paper trading and live
                                   trading.  paper_trading only gates order
                                   placement and GTT — never data collection.
        """
        self._running = True
        self._loop = asyncio.get_running_loop()

        # credentials_present = Groww client_id is a non-empty string in .env
        credentials_present = bool(self._settings.groww_client_id)

        if not credentials_present:
            logger.info(
                "[Scanner] No Groww credentials — starting MockTickGenerator "
                "(offline dev mode; paper_trading=%s)",
                self._settings.paper_trading,
            )
            self._mock_generator = MockTickGenerator(on_ticks_callback=self._on_ticks)
            # Seed avg_volume_20d so the volume_ratio filter works in dev mode.
            # MockTickGenerator drives _volume_base from uniform(80k–500k);
            # we pre-populate TickDataStores with the same values so
            # compute_volume_ratio() returns a real ratio rather than 0.0.
            self._mock_generator._seed_prices()   # idempotent
            for symbol, base_vol in self._mock_generator._volume_base.items():
                if symbol not in self._stores:
                    self._stores[symbol] = TickDataStore(symbol, avg_volume_20d=base_vol)
            logger.info(
                "[Scanner] Dev mode volume bases seeded for %d symbols",
                len(self._mock_generator._volume_base),
            )
            await self._mock_generator.run()
        else:
            logger.info(
                "[Scanner] Groww credentials present — loading historical volumes "
                "before GrowwFeed (paper_trading=%s)",
                self._settings.paper_trading,
            )
            # SC1: Pre-populate TickDataStore instances with 20-day avg volumes so the
            # volume filter is active from the first tick, not silently bypassed.
            await self._load_avg_volumes()

            # BN1: Start the OHLCV REST poll loop BEFORE connecting GrowwFeed so the
            # first tick batch already has valid OHLCV data in _ohlc_cache.
            # Runs in both paper and live mode whenever GrowwFeed is active —
            # paper_trading only skips order placement, not data collection.
            asyncio.create_task(
                self._start_ohlcv_poll_loop(),
                name="scanner_ohlcv_poll",
            )
            logger.info("[Scanner] OHLCV poll task started (interval=%ds)", OHLCV_POLL_INTERVAL_SECS)

            groww_client = get_groww_client()

            # Reconnect loop: transparently reauthenticate and reconnect if
            # GrowwFeed raises any connection or auth exception.
            _reconnect_attempt = 0
            while self._running:
                _reconnect_attempt += 1
                if _reconnect_attempt > 1:
                    logger.info(
                        "[Scanner] GrowwFeed reconnect attempt #%d", _reconnect_attempt
                    )

                loop = asyncio.get_event_loop()
                _stage = "create_ticker"
                try:
                    # create_ticker() is inside the try so that a constructor
                    # failure (e.g. bad token on GrowwFeed init) is also caught
                    # and handled by the reauth logic below.
                    ticker = await groww_client.create_ticker()
                    _stage = "configure_callbacks"
                    ticker.on_ticks = self._on_ticks
                    ticker.on_connect = self._on_connect
                    ticker.on_close = self._on_close
                    ticker.on_error = self._on_error

                    logger.info(
                        "[Scanner] Connecting GrowwFeed (attempt #%d)…", _reconnect_attempt
                    )
                    _stage = "connect_executor"
                    await loop.run_in_executor(None, ticker.connect, True)
                    # connect() returned normally — GrowwFeed closed gracefully.
                    break

                except Exception as exc:
                    if not self._running:
                        break  # scanner.stop() was called — exit cleanly

                    exc_type = type(exc).__name__
                    exc_msg = str(exc)
                    logger.exception(
                        "[Scanner] GrowwFeed exception on attempt #%d at stage=%s — %s: %s",
                        _reconnect_attempt,
                        _stage,
                        exc_type,
                        exc_msg,
                    )

                    # Detect authentication / connection failure.
                    # GrowwAPIAuthenticationException  → expired REST token
                    # GrowwFeedConnectionException     → any feed connection failure
                    #   (expired token causes the WebSocket handshake to fail, which
                    #    the SDK surfaces as GrowwFeedConnectionException regardless
                    #    of the specific HTTP status code — so we always try reauth
                    #    for this exception type before giving up)
                    _is_auth = False
                    try:
                        from growwapi.groww.exceptions import (
                            GrowwAPIAuthenticationException,
                            GrowwFeedConnectionException,
                        )
                        if isinstance(exc, GrowwAPIAuthenticationException):
                            _is_auth = True
                            logger.warning(
                                "[Scanner] GrowwAPIAuthenticationException — "
                                "token expired; reauthenticating"
                            )
                        elif isinstance(exc, GrowwFeedConnectionException):
                            # Any feed connection failure is treated as potentially
                            # auth-related because an expired/invalid token is the
                            # most common cause of GrowwFeed refusing to connect.
                            _is_auth = True
                            logger.warning(
                                "[Scanner] GrowwFeedConnectionException — "
                                "connection refused; attempting reauth before reconnect"
                            )
                    except ImportError:
                        pass

                    # Message-based fallback (e.g. SDK wraps auth errors in a
                    # plain Exception with a descriptive message).
                    if not _is_auth:
                        _lmsg = exc_msg.lower()
                        if (
                            "authentication" in _lmsg or "unauthori" in _lmsg
                            or "token" in _lmsg or "expire" in _lmsg
                            or "invalid" in _lmsg or "401" in _lmsg
                        ):
                            _is_auth = True
                            logger.warning(
                                "[Scanner] Auth-related message detected in %s — "
                                "attempting reauth before reconnect",
                                exc_type,
                            )

                    if _is_auth:
                        try:
                            await groww_client.reauthenticate()
                            logger.info(
                                "[Scanner] Re-authentication succeeded — "
                                "reconnecting GrowwFeed in 2s"
                            )
                            await asyncio.sleep(2)
                            continue  # reconnect with fresh token
                        except Exception as reauth_exc:
                            logger.error(
                                "[Scanner] Reauthentication failed: %s — "
                                "scanner stopping; call POST /api/auth/groww/login",
                                reauth_exc,
                            )
                            raise  # propagate to let the manager report it
                    else:
                        logger.error(
                            "[Scanner] Non-auth GrowwFeed failure (%s) — "
                            "scanner stopping",
                            exc_type,
                        )
                        raise  # non-auth error: propagate crash as before

    def stop(self) -> None:
        """Stop the scanner and any running tick source."""
        self._running = False
        if self._mock_generator is not None:
            self._mock_generator.stop()
        logger.info("Scanner stopped")
