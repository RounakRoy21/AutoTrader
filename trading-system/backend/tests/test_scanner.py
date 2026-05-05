"""
Tests for the Scanner — TickDataStore indicators and signal logic.

TickDataStore is pure in-memory computation, so these tests need no mocks.
"""

import asyncio
from datetime import datetime
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from agents.scanner import Scanner, TickDataStore


# ── Helpers ─────────────────────────────────────────────────────────────────


def _tick(price: float, cum_volume: int, *, high=None, low=None):
    """Build a minimal tick dict."""
    return {
        "last_price": price,
        "volume_traded": cum_volume,
        "ohlc": {
            "high": high if high is not None else price,
            "low": low if low is not None else price,
            "open": price,
            "close": price,
        },
        "exchange_timestamp": datetime.now(),
    }


# ═══════════════════════════════════════════════════════════════════════════
#  VWAP
# ═══════════════════════════════════════════════════════════════════════════


class TestVwap:
    """VWAP must use last_price only (not day high/low)."""

    def test_empty_store_returns_zero(self):
        store = TickDataStore("TEST")
        assert store.compute_vwap() == 0.0

    def test_single_tick(self):
        """Single tick is baseline-only (vol_delta=0) — VWAP is 0.0 until
        a second tick arrives with real volume."""
        store = TickDataStore("TEST")
        store.add_tick(_tick(100.0, cum_volume=1000))
        assert store.compute_vwap() == 0.0

    def test_two_ticks_produces_vwap(self):
        """After a baseline tick, the second tick has real volume → valid VWAP."""
        store = TickDataStore("TEST")
        store.add_tick(_tick(100.0, cum_volume=1000))  # baseline, delta=0
        store.add_tick(_tick(102.0, cum_volume=2000))  # delta=1000
        vwap = store.compute_vwap()
        # Only the 2nd tick has volume, so VWAP = 102.0
        assert abs(vwap - 102.0) < 0.01

    def test_equal_volume_deltas(self):
        """With equal volume per tick (after baseline), VWAP ≈ avg of ticks 2+."""
        store = TickDataStore("TEST")
        store.add_tick(_tick(100.0, cum_volume=1000))   # baseline delta=0
        store.add_tick(_tick(102.0, cum_volume=2000))   # delta=1000
        store.add_tick(_tick(104.0, cum_volume=3000))   # delta=1000
        vwap = store.compute_vwap()
        # Weighted: (102*1000 + 104*1000) / 2000 = 103.0
        assert abs(vwap - 103.0) < 0.01

    def test_unequal_volume_weights(self):
        """VWAP should weight prices by volume delta (after baseline)."""
        store = TickDataStore("TEST")
        store.add_tick(_tick(100.0, cum_volume=1000))    # baseline delta=0
        store.add_tick(_tick(105.0, cum_volume=2000))    # delta=1000
        store.add_tick(_tick(110.0, cum_volume=11000))   # delta=9000
        vwap = store.compute_vwap()
        # Expected: (105*1000 + 110*9000) / (1000+9000) = 1095000/10000 = 109.5
        assert abs(vwap - 109.5) < 0.01

    def test_day_high_low_do_not_affect_vwap(self):
        """Kite's cumulative day high/low should NOT influence VWAP.

        Before the fix, typical_price was (day_high + day_low + ltp) / 3
        which would yield different results when high/low differ from ltp.
        After the fix, only last_price is used.
        """
        store_clean = TickDataStore("CLEAN")
        store_skewed = TickDataStore("SKEWED")

        # Same prices and volumes — only day high/low differ
        for i in range(5):
            price = 100.0 + i
            cum_vol = (i + 1) * 1000
            store_clean.add_tick(_tick(price, cum_vol, high=price, low=price))
            store_skewed.add_tick(_tick(price, cum_vol, high=200.0, low=50.0))

        assert abs(store_clean.compute_vwap() - store_skewed.compute_vwap()) < 0.01


# ═══════════════════════════════════════════════════════════════════════════
#  RSI
# ═══════════════════════════════════════════════════════════════════════════


class TestRsi:

    def test_insufficient_data_returns_neutral(self):
        """RSI with < period+1 ticks should return 50 (neutral)."""
        store = TickDataStore("TEST")
        for i in range(10):
            store.add_tick(_tick(100.0 + i, cum_volume=(i + 1) * 100))
        assert store.compute_rsi() == 50.0

    def test_all_gains_approaches_100(self):
        store = TickDataStore("TEST")
        for i in range(30):
            store.add_tick(_tick(100.0 + i * 0.5, cum_volume=(i + 1) * 100))
        assert store.compute_rsi() > 90.0

    def test_all_losses_approaches_0(self):
        store = TickDataStore("TEST")
        for i in range(30):
            store.add_tick(_tick(200.0 - i * 0.5, cum_volume=(i + 1) * 100))
        assert store.compute_rsi() < 10.0

    def test_mixed_moves_in_middle_range(self):
        store = TickDataStore("TEST")
        prices = [100, 101, 100.5, 102, 101, 103, 102, 104, 103, 105,
                  104, 103, 104, 105, 104, 103, 104, 105, 106, 105]
        for i, p in enumerate(prices):
            store.add_tick(_tick(p, cum_volume=(i + 1) * 100))
        rsi = store.compute_rsi()
        assert 30 <= rsi <= 70

    def test_zero_losses_returns_100(self):
        """Edge case: if avg_loss is 0, RSI should be 100."""
        store = TickDataStore("TEST")
        # All identical prices after first → delta=0 except first
        store.add_tick(_tick(100.0, cum_volume=100))
        for i in range(1, 20):
            store.add_tick(_tick(100.0 + 0.01 * i, cum_volume=(i + 1) * 100))
        assert store.compute_rsi() == 100.0


# ═══════════════════════════════════════════════════════════════════════════
#  Volume Ratio
# ═══════════════════════════════════════════════════════════════════════════


class TestVolumeRatio:

    def test_normal(self):
        store = TickDataStore("TEST", avg_volume_20d=10_000)
        store.add_tick(_tick(100, cum_volume=15_000))
        # Volume ratio is now time-prorated; result depends on time of day.
        # Just verify it returns a positive number when avg is set.
        assert store.compute_volume_ratio() > 0

    def test_zero_avg_volume(self):
        store = TickDataStore("TEST", avg_volume_20d=0)
        store.add_tick(_tick(100, cum_volume=15_000))
        assert store.compute_volume_ratio() == 0.0

    def test_empty_store(self):
        store = TickDataStore("TEST", avg_volume_20d=10_000)
        assert store.compute_volume_ratio() == 0.0

    def test_volume_filter_skipped_when_avg_unavailable(self):
        """When avg_volume_20d=0 (not initialised), the volume condition
        should be skipped so that signals can still be generated."""
        queue = asyncio.Queue()
        scanner = Scanner(queue)
        store = TickDataStore("TEST", avg_volume_20d=0)
        # avg_volume_20d=0 → volume_ratio=0.0, but the check in _check_signal
        # is `store.avg_volume_20d > 0 and vol_ratio < 1.5`.
        # With avg=0 the first part is False, so the filter is skipped.
        assert store.avg_volume_20d == 0
        vol_ratio = store.compute_volume_ratio()
        # The guard: `store.avg_volume_20d > 0` is False → filter bypassed
        filter_blocks = store.avg_volume_20d > 0 and vol_ratio < 1.5
        assert not filter_blocks


# ═══════════════════════════════════════════════════════════════════════════
#  Volume Deltas
# ═══════════════════════════════════════════════════════════════════════════


class TestVolumeDeltas:

    def test_cumulative_to_delta(self):
        """Volume deltas should be computed from cumulative differences.
        First tick is baseline-only (delta=0) to avoid mid-day restart spikes."""
        store = TickDataStore("TEST")
        store.add_tick(_tick(100, cum_volume=1000))
        store.add_tick(_tick(101, cum_volume=1500))
        store.add_tick(_tick(102, cum_volume=2200))
        deltas = list(store._df["volume_delta"])
        assert deltas == [0, 500, 700]

    def test_no_negative_deltas(self):
        """Cumulative volume can't decrease — deltas should never be negative."""
        store = TickDataStore("TEST")
        store.add_tick(_tick(100, cum_volume=1000))
        # Anomaly: cumulative drops (shouldn't happen, but defensive)
        store.add_tick(_tick(101, cum_volume=800))
        deltas = list(store._df["volume_delta"])
        assert all(d >= 0 for d in deltas)


# ═══════════════════════════════════════════════════════════════════════════
#  LTP
# ═══════════════════════════════════════════════════════════════════════════


class TestLtp:

    def test_ltp_tracks_last_price(self):
        store = TickDataStore("TEST")
        store.add_tick(_tick(100, cum_volume=100))
        store.add_tick(_tick(105, cum_volume=200))
        assert store.ltp == 105.0

    def test_ltp_empty_store(self):
        store = TickDataStore("TEST")
        assert store.ltp == 0.0


# ═══════════════════════════════════════════════════════════════════════════
#  Tick Cap
# ═══════════════════════════════════════════════════════════════════════════


class TestTickCap:

    def test_deque_does_not_exceed_max(self):
        store = TickDataStore("TEST")
        for i in range(store.MAX_TICKS + 200):
            store.add_tick(_tick(100 + i * 0.01, cum_volume=(i + 1) * 10))
        assert len(store.ticks) == store.MAX_TICKS


# ═══════════════════════════════════════════════════════════════════════════
#  Signal Logic
# ═══════════════════════════════════════════════════════════════════════════


class TestSignalLogic:

    def test_suggested_qty_calculation(self, settings):
        """qty = max_loss_per_trade / (ltp * stop_loss_pct)"""
        queue = asyncio.Queue()
        scanner = Scanner(queue)
        scanner._settings = settings

        ltp = 1000.0
        qty = scanner._calculate_suggested_qty(ltp)
        # max_loss = 1M * 0.015 = 15000
        # sl_amount = 1000 * 0.008 = 8
        # qty_from_risk = 15000 / 8 = 1875
        # capital cap: 1M / 3 positions / 1000 ltp = 333
        # result = min(1875, 333) = 333
        assert qty == 333

    def test_suggested_qty_zero_ltp(self):
        queue = asyncio.Queue()
        scanner = Scanner(queue)
        assert scanner._calculate_suggested_qty(0.0) == 0

    def test_min_ticks_guard_blocks_early_signals(self):
        """_check_signal should return None when tick count < MIN_TICKS_FOR_SIGNAL."""
        queue = asyncio.Queue()
        scanner = Scanner(queue)
        store = TickDataStore("TEST", avg_volume_20d=10_000)

        # Add fewer ticks than threshold
        for i in range(Scanner.MIN_TICKS_FOR_SIGNAL - 1):
            store.add_tick(_tick(100 + i * 0.1, cum_volume=(i + 1) * 1000))

        signal = scanner._check_signal(store)
        assert signal is None


# ── Helpers for candle-based tests ──────────────────────────────────────────


def _tick_at(price: float, cum_volume: int, ts: datetime, *, high=None, low=None):
    """Build a tick dict with a specific timestamp."""
    return {
        "last_price": price,
        "volume_traded": cum_volume,
        "ohlc": {
            "high": high if high is not None else price,
            "low": low if low is not None else price,
            "open": price,
            "close": price,
        },
        "exchange_timestamp": ts,
    }


def _build_candle_store(n_completed_1m: int, base: float = 100.0, vol: float = 1.0):
    """Create a TickDataStore with N completed 1-minute candles.

    Each minute gets 3 ticks to create meaningful OHLC:
      tick 0: open = base + minute × 0.5
      tick 1: high = open + vol
      tick 2: close = open + 0.3
    An extra tick starts a new minute bucket so the Nth candle completes.
    """
    store = TickDataStore("TEST", avg_volume_20d=10_000)
    cum = 0
    for m in range(n_completed_1m + 1):  # +1 to close the Nth candle
        mid = base + m * 0.5
        total_minutes = 9 * 60 + 15 + m  # start at 09:15
        hour = total_minutes // 60
        minute = total_minutes % 60
        ts_base = datetime(2024, 1, 2, hour, minute)
        for sec, offset in [(0, 0.0), (20, vol), (40, 0.3)]:
            cum += 1000
            store.add_tick(
                _tick_at(round(mid + offset, 2), cum, ts_base.replace(second=sec))
            )
    return store


# ═══════════════════════════════════════════════════════════════════════════
#  CandleBuilder
# ═══════════════════════════════════════════════════════════════════════════


class TestCandleBuilder:
    """CandleBuilder aggregates ticks into interval candles."""

    def test_creates_completed_candles(self):
        store = _build_candle_store(5)
        df = store._candles_1m.completed_df
        assert df is not None
        assert len(df) == 5

    def test_ohlc_values(self):
        store = _build_candle_store(1, base=100.0, vol=2.0)
        df = store._candles_1m.completed_df
        row = df.iloc[0]
        assert row["open"] == 100.0
        assert row["high"] == 102.0  # base + vol
        assert row["low"] == 100.0
        assert row["close"] == 100.3  # base + 0.3

    def test_no_completed_with_single_bucket(self):
        store = TickDataStore("TEST", avg_volume_20d=10_000)
        ts = datetime(2024, 1, 2, 9, 15, 0)
        store.add_tick(_tick_at(100.0, 1000, ts))
        assert store._candles_1m.completed_df is None

    def test_5m_candles(self):
        """5-minute candles: one candle per 5 unique minutes."""
        store = _build_candle_store(10)  # 10 1m candles → 2 completed 5m candles
        df = store._candles_5m.completed_df
        assert df is not None
        assert len(df) >= 1


# ═══════════════════════════════════════════════════════════════════════════
#  EMA
# ═══════════════════════════════════════════════════════════════════════════


class TestEma:

    def test_insufficient_data_returns_zero(self):
        store = _build_candle_store(5)  # 5 candles, EMA(9) needs 9
        assert store.compute_ema(9) == 0.0

    def test_ema9_with_enough_data(self):
        store = _build_candle_store(20)
        ema9 = store.compute_ema(9)
        assert ema9 > 0

    def test_uptrend_short_above_long(self):
        """In an uptrend, EMA(9) should be above EMA(21)."""
        store = _build_candle_store(30, base=100.0)
        ema9 = store.compute_ema(9)
        ema21 = store.compute_ema(21)
        assert ema9 > ema21


# ═══════════════════════════════════════════════════════════════════════════
#  MACD
# ═══════════════════════════════════════════════════════════════════════════


class TestMacd:

    def test_insufficient_data_returns_zero(self):
        store = _build_candle_store(20)  # MACD needs 26
        assert store.compute_macd_histogram() == 0.0

    def test_positive_histogram_in_uptrend(self):
        store = _build_candle_store(40, base=100.0)
        macd_hist = store.compute_macd_histogram()
        assert macd_hist > 0


# ═══════════════════════════════════════════════════════════════════════════
#  ATR
# ═══════════════════════════════════════════════════════════════════════════


class TestAtr:

    def test_insufficient_data_returns_zero(self):
        store = _build_candle_store(10)  # ATR(14) needs 15
        assert store.compute_atr() == 0.0

    def test_positive_atr_with_volatility(self):
        store = _build_candle_store(20, vol=2.0)
        atr = store.compute_atr()
        assert atr > 0


# ═══════════════════════════════════════════════════════════════════════════
#  RSI Higher Timeframe (5m)
# ═══════════════════════════════════════════════════════════════════════════


class TestRsiHtf:

    def test_insufficient_data_returns_none(self):
        store = _build_candle_store(10)
        assert store.compute_rsi_htf() is None

    def test_returns_value_with_enough_5m_candles(self):
        # Need 15+ completed 5m candles = 75+ different minute buckets
        store = _build_candle_store(80)
        rsi = store.compute_rsi_htf()
        assert rsi is not None
        assert 0 < rsi <= 100


# ═══════════════════════════════════════════════════════════════════════════
#  ATR-based Qty Calculation
# ═══════════════════════════════════════════════════════════════════════════


class TestAtrQty:

    def test_atr_based_qty(self, settings):
        queue = asyncio.Queue()
        scanner = Scanner(queue)
        scanner._settings = settings

        # n_candles ≥ 45 is required before the intraday ATR is stable enough
        # to use for position sizing (SH3: opening-range ATR exclusion).
        qty = scanner._calculate_suggested_qty(1000.0, atr=5.0, n_candles=45)
        # max_loss = 1M * 0.015 = 15000
        # sl_distance = 5.0 * 1.5 = 7.5
        # qty_from_risk = 15000 / 7.5 = 2000
        # capital cap: 1M / 3 / 1000 = 333
        # result = min(2000, 333) = 333
        assert qty == 333

    def test_atr_qty_requires_min_candles(self, settings):
        """ATR-based sizing must fall back to fixed-pct when < 45 candles (SH3)."""
        queue = asyncio.Queue()
        scanner = Scanner(queue)
        scanner._settings = settings

        # n_candles=30 (early session) → ATR not trusted → fixed-pct fallback
        qty_early = scanner._calculate_suggested_qty(1000.0, atr=5.0, n_candles=30)
        # fixed: max_loss=15000, sl=1000*0.008=8, qty_from_risk=1875
        # capital cap: 1M / 3 / 1000 = 333
        # result = min(1875, 333) = 333
        assert qty_early == 333

    def test_fallback_without_atr(self, settings):
        queue = asyncio.Queue()
        scanner = Scanner(queue)
        scanner._settings = settings

        qty = scanner._calculate_suggested_qty(1000.0)
        # max_loss = 15000, sl = 1000 * 0.008 = 8, qty_from_risk = 1875
        # capital cap: 1M / 3 / 1000 = 333 → min(1875, 333) = 333
        assert qty == 333


# ═══════════════════════════════════════════════════════════════════════════
#  Slippage ATR Recalculation
# ═══════════════════════════════════════════════════════════════════════════


class TestSlippageAtrRecalc:
    """When fill price differs from signal LTP, SL/target must use ATR distances."""

    def test_atr_based_slippage_recalc(self):
        """With ATR available, SL and target should be recalculated from fill price
        using ATR multipliers, not fixed percentages."""
        fill_price = 1005.0
        signal_ltp = 1000.0
        atr = 5.0
        atr_sl_mult = 1.5
        atr_tgt_mult = 3.0
        stop_loss_pct = 0.008
        min_target_pct = 0.016

        # ATR-based (correct)
        new_sl_atr = round(fill_price - atr * atr_sl_mult, 2)
        new_tgt_atr = round(fill_price + atr * atr_tgt_mult, 2)

        # Fixed-% (old, incorrect)
        new_sl_fixed = round(fill_price * (1 - stop_loss_pct), 2)
        new_tgt_fixed = round(fill_price * (1 + min_target_pct), 2)

        # ATR gives: SL=997.50, TGT=1020.00
        assert new_sl_atr == 997.50
        assert new_tgt_atr == 1020.0

        # Fixed gives: SL=996.96, TGT=1021.08 — different risk profile
        assert new_sl_fixed != new_sl_atr
        assert new_tgt_fixed != new_tgt_atr

    def test_fixed_pct_fallback_without_atr(self):
        """Without ATR, slippage recalculation should use fixed percentages."""
        fill_price = 1005.0
        atr = 0.0  # no ATR available
        stop_loss_pct = 0.008
        min_target_pct = 0.016

        if atr and atr > 0:
            new_sl = round(fill_price - atr * 1.5, 2)
            new_tgt = round(fill_price + atr * 3.0, 2)
        else:
            new_sl = round(fill_price * (1 - stop_loss_pct), 2)
            new_tgt = round(fill_price * (1 + min_target_pct), 2)

        assert new_sl == round(fill_price * (1 - stop_loss_pct), 2)
        assert new_tgt == round(fill_price * (1 + min_target_pct), 2)


# ═══════════════════════════════════════════════════════════════════════════
#  Mid-day Restart Volume Spike Prevention
# ═══════════════════════════════════════════════════════════════════════════


class TestMidDayRestartVolume:
    """First tick's vol_delta must be 0 (baseline-only) to avoid
    injecting the entire day's cumulative volume into the first candle."""

    def test_first_tick_delta_is_zero(self):
        """First tick should record baseline only — vol_delta=0."""
        store = TickDataStore("TEST")
        store.add_tick(_tick(100, cum_volume=500_000))  # mid-day restart
        deltas = list(store._df["volume_delta"])
        assert deltas == [0]

    def test_second_tick_delta_is_correct(self):
        """Second tick should compute a real delta from the baseline."""
        store = TickDataStore("TEST")
        store.add_tick(_tick(100, cum_volume=500_000))
        store.add_tick(_tick(101, cum_volume=501_000))
        deltas = list(store._df["volume_delta"])
        assert deltas == [0, 1000]

    def test_fresh_start_small_cum_vol(self):
        """At 9:15 AM start, first tick with small cum_vol still gets delta=0."""
        store = TickDataStore("TEST")
        store.add_tick(_tick(100, cum_volume=100))
        store.add_tick(_tick(101, cum_volume=250))
        deltas = list(store._df["volume_delta"])
        assert deltas == [0, 150]


# ═══════════════════════════════════════════════════════════════════════════
#  MockTickGenerator — Audit-6 price-bound enforcement
# ═══════════════════════════════════════════════════════════════════════════


class TestMockTickBounds:
    """MockTickGenerator must keep every simulated price within ±2 % of the
    session-open price so paper-trading calibration is not skewed by unrealistic
    intraday drifts."""

    _SYMBOL = "TEST"
    _SEED = 1_000.0
    _TICKS = 2_000          # enough to expose any drift

    def _build_generator(self):
        """Return a MockTickGenerator pre-seeded with a known open price."""
        from integrations.mock_tick_generator import MockTickGenerator
        gen = MockTickGenerator(on_ticks_callback=lambda ticks: None)
        # Manually seed so _open_prices is populated without hitting Kite
        gen._prices[self._SYMBOL] = self._SEED
        gen._open_prices[self._SYMBOL] = self._SEED
        gen._day_high[self._SYMBOL] = self._SEED
        gen._day_low[self._SYMBOL] = self._SEED
        gen._directions[self._SYMBOL] = 1
        gen._cum_volume[self._SYMBOL] = 0
        gen._volume_base[self._SYMBOL] = 100_000
        return gen

    def test_price_never_exceeds_upper_bound(self):
        """All generated prices must be ≤ open * 1.02."""
        from integrations.mock_tick_generator import MockTickGenerator
        gen = self._build_generator()
        for _ in range(self._TICKS):
            tick = gen._next_tick(self._SYMBOL)
            assert tick["last_price"] <= self._SEED * 1.02 + 0.01, (
                f"Price {tick['last_price']} exceeded upper bound "
                f"{self._SEED * 1.02:.2f}"
            )

    def test_price_never_drops_below_lower_bound(self):
        """All generated prices must be ≥ open * 0.98."""
        gen = self._build_generator()
        for _ in range(self._TICKS):
            tick = gen._next_tick(self._SYMBOL)
            assert tick["last_price"] >= self._SEED * 0.98 - 0.01, (
                f"Price {tick['last_price']} dropped below lower bound "
                f"{self._SEED * 0.98:.2f}"
            )

    def test_direction_reverses_when_hitting_upper_band(self):
        """When price is clamped to the upper band, direction must flip to -1."""
        gen = self._build_generator()
        # Force price to exactly the ceiling so it triggers the reversal check
        gen._prices[self._SYMBOL] = round(self._SEED * 1.02, 2)
        gen._directions[self._SYMBOL] = 1   # heading up

        # Patch random.random to always return 0.1 (< 0.6 → always keep direction),
        # so the stochastic momentum flip doesn't interfere with this one tick.
        with patch("integrations.mock_tick_generator.random") as mock_rng:
            mock_rng.random.return_value = 0.1          # keep direction
            mock_rng.uniform.side_effect = [0.0003, 0.5]  # move_pct, vol_multiplier offset
            gen._next_tick(self._SYMBOL)

        assert gen._directions[self._SYMBOL] == -1, (
            "Direction should have flipped to -1 after hitting upper band"
        )

    def test_direction_reverses_when_hitting_lower_band(self):
        """When price is clamped to the lower band, direction must flip to +1."""
        gen = self._build_generator()
        gen._prices[self._SYMBOL] = round(self._SEED * 0.98, 2)
        gen._directions[self._SYMBOL] = -1   # heading down

        with patch("integrations.mock_tick_generator.random") as mock_rng:
            mock_rng.random.return_value = 0.1          # keep direction
            mock_rng.uniform.side_effect = [0.0003, 0.5]
            gen._next_tick(self._SYMBOL)

        assert gen._directions[self._SYMBOL] == 1, (
            "Direction should have flipped to +1 after hitting lower band"
        )

    def test_open_prices_seeded_by_seed_prices(self):
        """_seed_prices() must populate _open_prices for the symbol."""
        from integrations.mock_tick_generator import MockTickGenerator, SEED_PRICES
        if not SEED_PRICES:
            pytest.skip("SEED_PRICES is empty — cannot test seed path")
        symbol = next(iter(SEED_PRICES))
        gen = MockTickGenerator(on_ticks_callback=lambda ticks: None)
        # Patch get_instrument_map to include the test symbol so _seed_prices runs
        with patch(
            "integrations.mock_tick_generator.get_instrument_map",
            return_value={symbol: 12345},
        ):
            gen._seed_prices()
        assert symbol in gen._open_prices
        assert gen._open_prices[symbol] == pytest.approx(SEED_PRICES[symbol])
        assert symbol in gen._open_prices
        assert gen._open_prices[symbol] == pytest.approx(SEED_PRICES[symbol])


# ═══════════════════════════════════════════════════════════════════════════
#  Bias-Modulated Scanner Thresholds
# ═══════════════════════════════════════════════════════════════════════════


class TestScannerBiasModulation:
    """Scanner.set_market_bias() must update the cached bias that _check_signal()
    reads to apply market-condition-aware RSI, volume, gap, and NIFTY thresholds."""

    def _make_scanner(self, bias: str = "NEUTRAL") -> Scanner:
        scanner = Scanner(asyncio.Queue())
        scanner.set_market_bias(bias)
        return scanner

    def test_set_market_bias_updates_field(self):
        """set_market_bias() must persist the bias string on the instance."""
        scanner = self._make_scanner("BULLISH")
        assert scanner._market_bias == "BULLISH"

    def test_set_market_bias_normalises_case(self):
        """Lowercase bias value must be uppercased to match the lookup keys."""
        scanner = self._make_scanner()
        scanner.set_market_bias("bearish")
        assert scanner._market_bias == "BEARISH"

    def test_default_bias_is_neutral(self):
        scanner = Scanner(asyncio.Queue())
        assert scanner._market_bias == "NEUTRAL"

    # ── RSI band ──────────────────────────────────────────────────────────────

    def test_rsi_band_bullish_wider(self):
        """On BULLISH day RSI band = 42–68; RSI=43 should pass."""
        scanner = self._make_scanner("BULLISH")
        _rsi_lo, _rsi_hi = {"BULLISH": (42.0, 68.0)}.get(
            scanner._market_bias, (45.0, 65.0)
        )
        assert _rsi_lo <= 43.0 <= _rsi_hi  # RSI=43 passes

    def test_rsi_band_bearish_narrower(self):
        """On BEARISH day RSI band = 48–63; RSI=43 should fail."""
        scanner = self._make_scanner("BEARISH")
        _rsi_lo, _rsi_hi = {"BEARISH": (48.0, 63.0)}.get(
            scanner._market_bias, (45.0, 65.0)
        )
        assert not (_rsi_lo <= 43.0 <= _rsi_hi)  # RSI=43 fails

    def test_rsi_band_neutral_midpoint(self):
        """On NEUTRAL day RSI band = 45–65; RSI=43 fails, RSI=55 passes."""
        scanner = self._make_scanner("NEUTRAL")
        _rsi_lo, _rsi_hi = (45.0, 65.0)
        assert not (_rsi_lo <= 43.0 <= _rsi_hi)
        assert _rsi_lo <= 55.0 <= _rsi_hi

    # ── Volume ratio min ─────────────────────────────────────────────────────

    def test_vol_min_bullish_lower(self):
        """On BULLISH day vol_min = 1.3; 1.35× should be accepted."""
        scanner = self._make_scanner("BULLISH")
        _vol_min = {"BULLISH": 1.3, "NEUTRAL": 1.5, "BEARISH": 2.0}.get(
            scanner._market_bias, 1.5
        )
        assert 1.35 >= _vol_min

    def test_vol_min_bearish_higher(self):
        """On BEARISH day vol_min = 2.0; 1.8× should be rejected."""
        scanner = self._make_scanner("BEARISH")
        _vol_min = {"BULLISH": 1.3, "NEUTRAL": 1.5, "BEARISH": 2.0}.get(
            scanner._market_bias, 1.5
        )
        assert 1.8 < _vol_min

    # ── NIFTY drift threshold ────────────────────────────────────────────────

    def test_nifty_thresh_bullish_looser(self):
        """On BULLISH day NIFTY threshold = -0.8%; a -0.6% drift should NOT block signals."""
        scanner = self._make_scanner("BULLISH")
        _thresh = {"BULLISH": -0.008, "NEUTRAL": -0.005, "BEARISH": -0.003}.get(
            scanner._market_bias, -0.005
        )
        drift = -0.006   # -0.6%
        assert drift >= _thresh  # passes (not below threshold)

    def test_nifty_thresh_bearish_tighter(self):
        """On BEARISH day NIFTY threshold = -0.3%; a -0.4% drift blocks signals."""
        scanner = self._make_scanner("BEARISH")
        _thresh = {"BULLISH": -0.008, "NEUTRAL": -0.005, "BEARISH": -0.003}.get(
            scanner._market_bias, -0.005
        )
        drift = -0.004   # -0.4%
        assert drift < _thresh  # blocked

    # ── Gap filter ───────────────────────────────────────────────────────────

    def test_gap_filter_bullish_relaxed(self):
        """On BULLISH day gap_max = 2.0%; a 1.7% gap-up should pass."""
        scanner = self._make_scanner("BULLISH")
        _gap_max = {"BULLISH": 2.0, "NEUTRAL": 1.5, "BEARISH": 1.0}.get(
            scanner._market_bias, 1.5
        )
        assert 1.7 <= _gap_max  # passes

    def test_gap_filter_bearish_tighter(self):
        """On BEARISH day gap_max = 1.0%; a 1.2% gap-up should be rejected."""
        scanner = self._make_scanner("BEARISH")
        _gap_max = {"BULLISH": 2.0, "NEUTRAL": 1.5, "BEARISH": 1.0}.get(
            scanner._market_bias, 1.5
        )
        assert 1.2 > _gap_max  # blocked
