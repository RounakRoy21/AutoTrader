"""
Backtest ⇄ live parity and the trade simulator (audit V1, V2, V4, V5, V6).

  * Golden parity: the live Scanner and the backtest reach the same verdict on
    the same indicator snapshot (both call ``agents.signal_rules``).
  * The simulator uses the live exit policy, enters at the next bar's open,
    applies slippage / gap fills / costs, and the live portfolio gates.
  * Metrics count ALL exits.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time as dt_time
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from agents import exit_policy, signal_rules
from agents.risk_manager import _transaction_costs
from backtest import runner
from backtest.simulator import (
    SignalCandidate,
    SimTrade,
    SlippageModel,
    apply_portfolio_rules,
    compute_metrics,
    simulate_trade,
    split_holdout,
    walk_forward_folds,
)

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 11, 12)          # a Thursday


def _settings(**over):
    base = dict(
        groww_client_id="", gap_filter_pct=1.5, nifty_trend_filter_pct=-0.005,
        total_capital=100_000.0, max_loss_per_trade_pct=0.015, atr_sl_multiplier=1.5,
        atr_target_multiplier=3.0, stop_loss_pct=0.01, min_target_pct=0.02,
        max_open_positions=3, max_trades_per_day=6, max_trades_per_symbol_per_day=2,
        partial_booking_enabled=True, partial_booking_trigger_r=1.0,
        partial_booking_fraction=0.5, partial_booking_move_sl_to_breakeven=True,
        roi_decay_enabled=False, trailing_sl_activation_pct=0.008, trailing_sl_trail_pct=0.007,
        daily_drawdown_limit=3000.0, stock_lock_after_sl=True,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ═══════════════════════════════════════════════════════════════════════════
#  V1 — golden parity: Scanner._check_signal vs backtest.check_signal
# ═══════════════════════════════════════════════════════════════════════════


def _store(**over):
    snap = dict(
        ltp=1010.0, vwap=1000.0, rsi=58.0, vol_ratio=2.0, avg_volume_20d=1e6,
        prev_close=1000.0, open_price=1005.0, ema_9=1008.0, ema_21=1004.0,
        macd=0.5, atr=4.0, rsi_5m=60.0, n_ticks=200, n_candles=40,
    )
    snap.update(over)
    return SimpleNamespace(
        symbol="RELIANCE",
        ticks=[0] * snap["n_ticks"],
        _candles_1m=SimpleNamespace(completed_df=[0] * snap["n_candles"]),
        ltp=snap["ltp"],
        avg_volume_20d=snap["avg_volume_20d"],
        _prev_close=snap["prev_close"],
        _open_price=snap["open_price"],
        compute_vwap=lambda: snap["vwap"],
        compute_rsi=lambda: snap["rsi"],
        compute_volume_ratio=lambda: snap["vol_ratio"],
        compute_ema=lambda n: snap["ema_9"] if n == 9 else snap["ema_21"],
        compute_macd_histogram=lambda: snap["macd"],
        compute_atr=lambda: snap["atr"],
        compute_rsi_htf=lambda: snap["rsi_5m"],
    )


GOLDEN = [
    # (case, time, store overrides, nifty (open, ltp), expected pass)
    ("all gates pass", (11, 0), {}, (0, 0), True),
    ("below vwap", (11, 0), {"ltp": 999.0, "ema_21": 990.0}, (0, 0), False),
    ("rsi above band", (11, 0), {"rsi": 66.0}, (0, 0), False),
    ("early-session tight band", (9, 30), {"rsi": 52.0}, (0, 0), False),
    ("early-session inside band", (9, 30), {"rsi": 57.0}, (0, 0), True),
    ("low volume", (11, 0), {"vol_ratio": 1.4}, (0, 0), False),
    ("no volume history skips gate", (11, 0), {"vol_ratio": 0.0, "avg_volume_20d": 0.0}, (0, 0), True),
    ("gap up", (11, 0), {"open_price": 1020.0}, (0, 0), False),
    ("far below ema21", (11, 0), {"ema_21": 1025.0, "ema_9": 1030.0}, (0, 0), False),
    ("ema cross down", (11, 0), {"ema_9": 1003.0}, (0, 0), False),
    ("macd negative", (11, 0), {"macd": -0.1}, (0, 0), False),
    ("trend gates off below 26 candles", (11, 0), {"macd": -0.1, "n_candles": 20}, (0, 0), True),
    ("5m rsi out of band", (11, 0), {"rsi_5m": 75.0}, (0, 0), False),
    ("nifty falling", (11, 0), {}, (20000.0, 19880.0), False),
    ("nifty flat", (11, 0), {}, (20000.0, 19990.0), True),
    ("after cutoff", (15, 16), {}, (0, 0), False),
    ("warm-up ticks", (11, 0), {"n_ticks": 10}, (0, 0), False),
    ("warm-up candles", (11, 0), {"n_candles": 10}, (0, 0), False),
]


@pytest.mark.parametrize("case,hm,over,nifty,expected", GOLDEN, ids=[g[0] for g in GOLDEN])
def test_scanner_and_backtest_reach_the_same_verdict(case, hm, over, nifty, expected):
    from agents.scanner import Scanner

    now = datetime(DAY.year, DAY.month, DAY.day, hm[0], hm[1], tzinfo=IST)
    settings = _settings()

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    scanner = Scanner(asyncio.Queue())
    scanner._settings = settings
    scanner._nifty_open_price, scanner._nifty_ltp = nifty
    with patch("agents.scanner.datetime", _DT):
        live = scanner._check_signal(_store(**over))
    replay = runner.check_signal(
        _store(**over), now, None, skip_volume=False,
        nifty_open=nifty[0], nifty_ltp=nifty[1], settings=settings,
    )
    assert (live is not None) == (replay is not None) == expected
    if expected:
        assert live.ltp == replay.ltp and live.atr == round(replay.atr, 2)


def test_bias_thresholds_match_documented_profiles():
    s = _settings()
    assert signal_rules.bias_thresholds("bullish", s) == signal_rules.BiasThresholds(45.0, 68.0, 1.3, 2.0, -0.008)
    assert signal_rules.bias_thresholds("BEARISH", s) == signal_rules.BiasThresholds(50.0, 63.0, 2.0, 1.0, -0.003)
    neutral = signal_rules.bias_thresholds("unknown", s)
    assert (neutral.gap_max_pct, neutral.nifty_drift_min) == (1.5, -0.005)


def test_backtest_carries_no_copied_thresholds():
    for name in ("RSI_LO", "VOL_RATIO_MIN", "GAP_MAX_PCT", "ATR_SL_MULT", "NIFTY_TREND_THRESH"):
        assert not hasattr(runner, name)


# ═══════════════════════════════════════════════════════════════════════════
#  Exit policy (shared by RiskManager and simulator)
# ═══════════════════════════════════════════════════════════════════════════


def _pos(**kw):
    base = dict(entry_price=100.0, stop_loss_price=98.5, target_price=103.0, quantity=10,
                partial_booked=False, partial_target_price=None, stop_kind=None,
                product_type="MIS", trade_date=DAY, entry_time=dt_time(10, 0))
    base.update(kw)
    return SimpleNamespace(**base)


class TestExitPolicy:
    def test_initial_levels_atr_and_fallback(self):
        s = _settings()
        assert exit_policy.initial_levels(100.0, 1.0, s, 0.05) == (98.5, 103.0)
        assert exit_policy.initial_levels(100.03, None, s, 0.05) == (99.0, 102.05)

    def test_partial_target_only_below_full_target(self):
        s = _settings()
        assert exit_policy.partial_target(100.0, 98.5, 103.0, 10, s) == 101.5
        assert exit_policy.partial_target(100.0, 98.5, 101.0, 10, s) is None
        assert exit_policy.partial_target(100.0, 98.5, 103.0, 1, s) is None

    @pytest.mark.parametrize("minutes,expected", [(10, None), (25, 101.5), (40, 101.0), (55, 100.5)])
    def test_roi_decay_tiers(self, minutes, expected):
        now = datetime(DAY.year, DAY.month, DAY.day, 10, 0, tzinfo=IST) + pd.Timedelta(minutes=minutes)
        assert exit_policy.roi_decay_target(_pos(), now, None, 0.05) == expected

    def test_roi_decay_never_crosses_trailed_stop(self):
        now = datetime(DAY.year, DAY.month, DAY.day, 11, 0, tzinfo=IST)
        assert exit_policy.roi_decay_target(_pos(stop_loss_price=100.6), now, None, 0.05) is None

    def test_trailing_only_ratchets_up(self):
        s = _settings()
        assert exit_policy.trailing_stop(_pos(), 100.5, s, 0.05) is None            # below activation
        assert exit_policy.trailing_stop(_pos(), 101.0, s, 0.05) == 100.25
        assert exit_policy.trailing_stop(_pos(stop_loss_price=100.5), 101.0, s, 0.05) is None

    def test_square_off_at_1500(self):
        assert exit_policy.square_off_due(_pos(), dt_time(15, 0))
        assert not exit_policy.square_off_due(_pos(), dt_time(14, 59))


# ═══════════════════════════════════════════════════════════════════════════
#  Simulator
# ═══════════════════════════════════════════════════════════════════════════


def _candles(start: str, rows):
    h, m = (int(x) for x in start.split(":"))
    base = datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=IST)
    return pd.DataFrame([
        {"timestamp": base + pd.Timedelta(minutes=i), "date": DAY,
         "open": o, "high": hi, "low": lo, "close": c, "volume": 1000}
        for i, (o, hi, lo, c) in enumerate(rows)
    ])


def _cand(rows, start="10:01", atr=1.0):
    return SignalCandidate(
        symbol="RELIANCE", signal_ts=datetime(DAY.year, DAY.month, DAY.day, 10, 0, tzinfo=IST),
        ltp=100.0, vwap=99.5, rsi=58.0, vol_ratio=2.0, rsi_5m=60.0, ema_9=100.0, ema_21=99.8,
        macd_hist=0.1, atr=atr, n_candles=50, future=_candles(start, rows),
    )


NO_SLIP = SlippageModel(bps=0.0)


class TestSimulateTrade:
    def test_enters_at_next_bar_open_with_slippage(self):
        t = simulate_trade(_cand([(100.0, 100.3, 99.9, 100.1)]), _settings(), SlippageModel(bps=10))
        assert t.entry_price == pytest.approx(100.1)
        assert t.entry_ts.time() == dt_time(10, 1)
        assert t.exit_reason == "END_OF_DATA"

    def test_no_entry_at_or_after_square_off(self):
        assert simulate_trade(_cand([(100.0, 100.1, 99.9, 100.0)], start="15:00"), _settings(), NO_SLIP) is None

    def test_scale_out_then_target_books_two_legs(self):
        rows = [(100.0, 100.5, 99.8, 100.2), (100.2, 103.2, 100.1, 103.0)]
        t = simulate_trade(_cand(rows), _settings(), NO_SLIP)
        assert (t.qty, t.initial_sl, t.initial_target) == (333, 98.5, 103.0)
        assert t.legs == [(166, 101.5), (167, 103.0)]
        assert t.exit_reason == "TARGET_HIT"
        assert t.gross_pnl == pytest.approx(166 * 1.5 + 167 * 3.0)
        costs = _transaction_costs(100.0, 101.5, 166, "MIS") + _transaction_costs(100.0, 103.0, 167, "MIS")
        assert t.net_pnl == pytest.approx(t.gross_pnl - costs, abs=0.02)

    def test_runner_stopped_at_breakeven(self):
        rows = [(100.0, 100.5, 99.8, 100.2), (100.2, 101.6, 100.1, 101.0), (101.0, 101.1, 99.9, 100.0)]
        t = simulate_trade(_cand(rows), _settings(trailing_sl_activation_pct=0.5), NO_SLIP)
        assert t.exit_reason == "BREAKEVEN"
        assert t.legs == [(166, 101.5), (167, 100.0)]

    def test_gap_through_stop_fills_at_open_with_stop_slippage(self):
        rows = [(100.0, 100.3, 99.9, 100.1), (98.0, 98.2, 97.5, 98.1)]
        t = simulate_trade(_cand(rows), _settings(), SlippageModel(bps=10, stop_mult=2.0))
        assert t.exit_reason == "STOP_LOSS_HIT"
        assert t.exit_price == pytest.approx(98.0 * (1 - 0.002), abs=0.01)
        assert t.r_multiple < -1                               # worse than 1R: gap + costs

    def test_trailed_stop_exits_as_trailing(self):
        rows = [(100.0, 100.3, 99.9, 100.1), (100.1, 101.0, 100.0, 100.8), (100.9, 100.95, 100.0, 100.1)]
        t = simulate_trade(_cand(rows), _settings(partial_booking_enabled=False), NO_SLIP)
        assert t.exit_reason == "TRAILING_SL"
        assert t.exit_price == 100.25

    def test_square_off_at_1500_open(self):
        flat = [(100.0, 100.2, 99.9, 100.1)] * 5
        t = simulate_trade(_cand(flat + [(100.4, 100.5, 100.3, 100.4)], start="14:55"), _settings(), NO_SLIP)
        assert t.exit_reason == "EOD_CLOSE"
        assert t.exit_ts.time() == dt_time(15, 0) and t.exit_price == 100.4


# ═══════════════════════════════════════════════════════════════════════════
#  Portfolio rules + metrics
# ═══════════════════════════════════════════════════════════════════════════


def _trade(sym, entry_hm, exit_hm, net, d=DAY):
    def _ts(hm):
        return datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=IST)
    return SimTrade(
        symbol=sym, trade_date=d, signal_ts=_ts(entry_hm), entry_ts=_ts(entry_hm), exit_ts=_ts(exit_hm),
        signal_ltp=100.0, entry_price=100.0, qty=10, initial_sl=98.5, initial_target=103.0,
        exit_price=100.0, exit_reason="EOD_CLOSE", legs=[(10, 100.0)], gross_pnl=net, costs=0.0,
        net_pnl=net, risk_rupees=15.0, r_multiple=round(net / 15.0, 3),
    )


class TestPortfolioRules:
    def test_max_open_positions(self):
        trades = [_trade(s, (10, i), (14, 0), 10.0) for i, s in enumerate("ABCD")]
        accepted, rejected, _ = apply_portfolio_rules(trades, _settings())
        assert [t.symbol for t in accepted] == ["A", "B", "C"]
        assert rejected == {"MAX_OPEN": 1}

    def test_daily_halt_blocks_new_entries(self):
        trades = [_trade("A", (10, 0), (10, 30), -3500.0), _trade("B", (11, 0), (12, 0), 50.0)]
        accepted, rejected, halted = apply_portfolio_rules(trades, _settings())
        assert [t.symbol for t in accepted] == ["A"] and halted == {DAY}
        assert rejected == {"DAILY_HALT": 1}

    def test_stock_lock_after_loss_and_one_position_per_symbol(self):
        trades = [
            _trade("A", (10, 0), (10, 30), -100.0),
            _trade("A", (11, 0), (12, 0), 50.0),     # locked after the loss
            _trade("B", (10, 0), (13, 0), 10.0),
            _trade("B", (11, 0), (12, 0), 10.0),     # B still open
        ]
        accepted, rejected, _ = apply_portfolio_rules(trades, _settings())
        assert len(accepted) == 2
        assert rejected == {"STOCK_LOCK": 1, "ALREADY_OPEN": 1}


class TestMetrics:
    def test_profit_factor_counts_every_exit_and_drawdown(self):
        trades = [_trade("A", (10, 0), (10, 30), 100.0), _trade("B", (10, 5), (11, 0), -50.0),
                  _trade("C", (10, 10), (15, 0), -30.0)]
        m = compute_metrics(trades, _settings())
        assert m["profit_factor"] == pytest.approx(1.25)
        assert m["net_pnl"] == 20.0
        assert m["max_drawdown"] == 80.0
        assert m["max_losing_streak"] == 2
        lo, hi = m["expectancy_ci95"]
        assert lo <= m["expectancy_rupees"] <= hi

    def test_halt_days_counted_from_realised_losses(self):
        trades = [_trade("A", (10, 0), (10, 30), -3100.0)]
        assert compute_metrics(trades, _settings())["halt_days"] == 1

    def test_walk_forward_and_holdout_splits(self):
        days = [date(2026, 11, d) for d in (2, 3, 4, 5, 6, 9)]
        assert walk_forward_folds(days, 3) == [
            (date(2026, 11, 2), date(2026, 11, 3)),
            (date(2026, 11, 4), date(2026, 11, 5)),
            (date(2026, 11, 6), date(2026, 11, 9)),
        ]
        a, b = _trade("A", (10, 0), (11, 0), 1.0, d=days[0]), _trade("A", (10, 0), (11, 0), 1.0, d=days[-1])
        assert split_holdout([a, b], days[-1]) == ([a], [b])
