"""
Scanner module — Real-time tick processing and signal generation.

Subscribes to live tick data via Kite Connect WebSocket (KiteTicker).
Maintains a rolling in-memory OHLCV dataframe per stock using pandas.
Recalculates VWAP, RSI(14), and Volume Ratio on every new tick.
Fires a signal when all three entry conditions are met simultaneously.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from collections import deque
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd
import pytz

from core.config import get_settings
from integrations.kite_client import get_kite_client
from integrations.instrument_service import get_instrument_map, get_symbol
from integrations.ltp_store import set_ltp
from integrations.mock_tick_generator import MockTickGenerator
from schemas.trade import ScannerSignal

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


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
    - RSI is computed on ``last_price`` (NOT ``ohlc.close``), because in live
      Kite tick data ``ohlc.close`` is the *previous day's* close — a static
      value that would produce a meaningless RSI.
    - VWAP computes per-tick volume deltas from Kite's *cumulative*
      ``volume_traded`` field, ensuring correct typical-price weighting.
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
        cum_vol = tick.get("volume_traded", 0)
        # Kite's volume_traded is cumulative for the day.
        # First tick: record as baseline only (delta=0).  On a mid-day restart
        # cum_vol could be 500K+; treating it as a delta would inject a false
        # volume spike into the first candle and skew indicators.
        if self._prev_cum_volume == 0:
            vol_delta = 0
        else:
            vol_delta = max(0, cum_vol - self._prev_cum_volume)
        self._prev_cum_volume = cum_vol

        ts = tick.get("exchange_timestamp") or datetime.now(IST)
        price = tick.get("last_price", 0.0)

        # SH2: Capture today's open and previous day's close from the very first
        # tick of the session. Kite's ohlc.open = today's day open; ohlc.close =
        # previous day's close (a static field that never changes intraday).
        ohlc = tick.get("ohlc", {})
        if self._open_price == 0.0:
            open_price = float(ohlc.get("open", 0.0))
            prev_close = float(ohlc.get("close", 0.0))
            if open_price > 0:
                self._open_price = open_price
            if prev_close > 0:
                self._prev_close = prev_close

        self.ticks.append({
            "timestamp": ts,
            "last_price": price,
            "volume_delta": vol_delta,
            "cum_volume": cum_vol,
            "high": ohlc.get("high", 0.0),
            "low": ohlc.get("low", 0.0),
            "open": ohlc.get("open", 0.0),
            "close": ohlc.get("close", 0.0),
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

        Uses last_price directly — NOT ``(high + low + close) / 3`` — because
        Kite's ``ohlc.high`` / ``ohlc.low`` are cumulative *day* extremes that
        grow monotonically.  Using them as a per-tick typical price would
        progressively skew the VWAP upward throughout the session.
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
        """Calculate RSI(14) using Wilder's smoothing on last_price (NOT ohlc.close)."""
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
    SIGNAL_COOLDOWN_SECS = 300  # 5 minutes
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

    def _calculate_suggested_qty(self, ltp: float, atr: float = 0.0, n_candles: int = 0) -> int:
        """
        Calculate position size.

        With ATR (≥45 completed 1m candles): qty = max_loss / (atr × sl_multiplier)
        — volatility-aware sizing.  The 45-candle minimum (≈45 min of session)
        avoids the noisy opening-range ATR, which can run 2–3× the historical
        daily ATR in the first 30 minutes, causing dangerous over- or under-sizing.

        Without sufficient candles (or ATR=0): qty = max_loss / (ltp × stop_loss_pct)
        — fixed-percentage fallback.
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
        return max(1, int(max_loss / sl_distance))

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

        # Require minimum completed 1m candles for candle-based indicators
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

        # ── Tick-level indicators ──────────────────────
        ltp = store.ltp
        vwap = store.compute_vwap()
        rsi = store.compute_rsi()
        vol_ratio = store.compute_volume_ratio()

        # Condition 1: Price > VWAP
        if ltp <= vwap:
            return None
        # Condition 2: RSI between 45 and 65 (scanner pre-filter; see docstring)
        if not (45 <= rsi <= 65):
            return None
        # Condition 3: Volume > 1.5x 20-day average (skip when historical data unavailable)
        if store.avg_volume_20d > 0 and vol_ratio < 1.5:
            return None

        # Condition 7: Gap-at-open filter (SH2)
        # Reject signals when the stock gapped up beyond the configured threshold
        # vs the previous session's close.  Stocks already extended at open are
        # prone to intraday mean-reversion even when momentum indicators look green.
        if store._prev_close > 0 and store._open_price > 0:
            gap_pct = (store._open_price - store._prev_close) / store._prev_close * 100
            if gap_pct > self._settings.gap_filter_pct:
                return None

        # ── Candle-based indicators (1m) ───────────
        ema_9 = store.compute_ema(9)
        ema_21 = store.compute_ema(21)
        macd_hist = store.compute_macd_histogram()
        atr = store.compute_atr()

        # Conditions 4 + 5: EMA trend and MACD momentum — both require ≥35 candles
        # so that MACD (which needs 26-period EMA) is always evaluated alongside
        # the EMA crossover check, rather than being silently skipped.  (SM2)
        if n_candles >= 35:
            if ema_9 > 0 and ema_21 > 0 and ema_9 <= ema_21:
                return None
            if macd_hist <= 0:
                return None

        # ── Higher timeframe filter (5m) ───────────
        rsi_5m = store.compute_rsi_htf()
        # Condition 6: 5-min RSI in neutral-bullish range (45–72, tightened from 35–70)
        if rsi_5m is not None and (rsi_5m < 45 or rsi_5m > 72):
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
        """Callback invoked by KiteTicker or MockTickGenerator on each tick batch."""
        for tick in ticks:
            token = tick.get("instrument_token")
            symbol = get_symbol(token) if token else None
            if symbol is None:
                continue

            if symbol not in self._stores:
                # In live mode, all known symbols are pre-loaded by _load_avg_volumes()
                # before the ticker starts.  An unknown token here is an instrument
                # outside the focus list — create a store without volume data but
                # log a warning so it is visible in production.  (SM4)
                if not self._settings.paper_trading:
                    logger.warning(
                        "[Scanner] Tick for unexpected symbol %s (token=%s) — "
                        "creating store with no historical volume data",
                        symbol, token,
                    )
                self._stores[symbol] = TickDataStore(symbol)
            store = self._stores[symbol]
            store.add_tick(tick)

            # Keep the shared LTP store current (thread-safe, sync-safe)
            set_ltp(symbol, tick.get("last_price", 0.0))

            signal = self._check_signal(store)
            if signal is not None:
                try:
                    # asyncio.Queue is NOT thread-safe.  In live mode _on_ticks
                    # is called from KiteTicker's WebSocket thread, so we must
                    # schedule the put on the event loop.  In paper mode the
                    # callback runs on the event loop already, so both paths
                    # are safe (call_soon_threadsafe is a no-overhead passthrough
                    # when called from the loop's own thread).
                    if self._loop is not None:
                        self._loop.call_soon_threadsafe(
                            self._signal_queue.put_nowait, signal
                        )
                    else:
                        self._signal_queue.put_nowait(signal)
                except asyncio.QueueFull:
                    logger.warning("Signal queue full — dropping signal for %s", symbol)

    async def _load_avg_volumes(self) -> None:
        """Fetch 20-day average daily volume for every focus stock before session start.

        Pre-populates TickDataStore instances so the volume filter (Condition 3) is
        correctly enforced from the very first signal check.  Without this, all stores
        default to avg_volume_20d=0 and the volume check is silently bypassed for the
        entire session.  (SC1)

        Best-effort: symbols whose historical fetch fails are initialised with 0.0
        (volume check bypassed only for that symbol, a warning is logged).
        """
        kite_client = get_kite_client()
        instrument_map = get_instrument_map()
        loaded = 0
        for symbol, token in instrument_map.items():
            try:
                candles = await kite_client.get_historical_data(
                    token, interval="day", days_back=30
                )
                volumes = [c["volume"] for c in candles if c.get("volume", 0) > 0]
                # Take the last 20 trading days; fall back to all available if <20
                recent = volumes[-20:] if len(volumes) >= 20 else volumes
                avg_vol = sum(recent) / len(recent) if recent else 0.0
                self._stores[symbol] = TickDataStore(symbol, avg_volume_20d=avg_vol)
                if avg_vol > 0:
                    loaded += 1
                    logger.debug("[Scanner] %s avg_volume_20d=%.0f", symbol, avg_vol)
                else:
                    logger.warning("[Scanner] %s: no volume data in historical candles", symbol)
            except Exception as exc:
                logger.warning(
                    "[Scanner] Could not fetch avg volume for %s: %s "
                    "— volume filter disabled for this symbol",
                    symbol, exc,
                )
                self._stores[symbol] = TickDataStore(symbol)
        logger.info(
            "[Scanner] Loaded 20-day avg volumes for %d/%d symbols",
            loaded, len(instrument_map),
        )

    def _on_connect(self, ws, response) -> None:
        """Subscribe to instruments on WebSocket connect."""
        tokens = list(get_instrument_map().values())
        if tokens:
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
            logger.info("KiteTicker subscribed to %d instruments", len(tokens))

    def _on_close(self, ws, code, reason) -> None:
        logger.warning("KiteTicker closed: code=%s reason=%s", code, reason)
        self._running = False

    def _on_error(self, ws, code, reason) -> None:
        logger.error("KiteTicker error: code=%s reason=%s", code, reason)

    async def start(self) -> None:
        """Start the scanner: MockTickGenerator in paper mode, KiteTicker in live mode."""
        self._running = True
        self._loop = asyncio.get_running_loop()

        if self._settings.paper_trading:
            logger.info("[Scanner] Paper trading mode — starting MockTickGenerator")
            self._mock_generator = MockTickGenerator(on_ticks_callback=self._on_ticks)
            await self._mock_generator.run()
        else:
            logger.info("[Scanner] Live mode — loading historical volumes before KiteTicker")
            # SC1: Pre-populate TickDataStore instances with 20-day avg volumes so the
            # volume filter is active from the first tick, not silently bypassed.
            await self._load_avg_volumes()

            kite_client = get_kite_client()
            ticker = await kite_client.create_ticker()

            ticker.on_ticks = self._on_ticks
            ticker.on_connect = self._on_connect
            ticker.on_close = self._on_close
            ticker.on_error = self._on_error

            # KiteTicker.connect is blocking; run in a thread
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, ticker.connect, True)

    def stop(self) -> None:
        """Stop the scanner and any running tick source."""
        self._running = False
        if self._mock_generator is not None:
            self._mock_generator.stop()
        logger.info("Scanner stopped")
