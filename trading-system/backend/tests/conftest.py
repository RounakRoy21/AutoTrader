"""
Shared fixtures for the AutoTrader backend test suite.
"""

from datetime import date, time as dt_time
from unittest.mock import MagicMock

import pytest

from core.config import Settings
from models.trade import Trade
from schemas.decision import Decision, DecisionOutput, ProductType, SignalAudit
from schemas.trade import ScannerSignal


# ── SignalAudit helper ───────────────────────────────────────────────────────────


def default_audit(**overrides) -> SignalAudit:
    """Build a SignalAudit where every condition passes by default.
    Pass keyword args to override individual fields for failure-path tests."""
    defaults = dict(
        rsi_cited=55.0,
        rsi_in_range=True,
        volume_ratio_cited=1.8,
        volume_confirms=True,
        vwap_deviation_pct=0.40,
        price_vwap_valid=True,
        ema_aligned=None,
        macd_confirms=None,
        risk_reward_ratio=2.5,
        confidence_score=75,
        conditions_not_met=[],
    )
    defaults.update(overrides)
    return SignalAudit(**defaults)


# ── Settings ────────────────────────────────────────────────────────────────


@pytest.fixture
def settings() -> Settings:
    """Return a Settings instance with deterministic test defaults."""
    return Settings(
        paper_trading=True,
        total_capital=1_000_000.0,
        stop_loss_pct=0.010,
        min_target_pct=0.020,
        max_loss_per_trade_pct=0.015,
        max_open_positions=3,
        max_trades_per_day=6,
        daily_drawdown_limit_pct=0.03,
        daily_drawdown_soft_alert_pct=0.02,
        trailing_sl_activation_pct=0.008,
        trailing_sl_trail_pct=0.007,
        atr_sl_multiplier=1.5,
        atr_target_multiplier=3.0,
        stock_lock_after_sl=True,
        consecutive_loss_pause_threshold=3,
        consecutive_loss_pause_minutes=30,
        roi_decay_enabled=True,
        anthropic_api_key="placeholder",
        kite_api_key="placeholder",
        kite_api_secret="placeholder",
    )


# ── Scanner Signal ──────────────────────────────────────────────────────────


@pytest.fixture
def signal() -> ScannerSignal:
    """A typical scanner signal for RELIANCE @ ₹2500."""
    return ScannerSignal(
        stock="RELIANCE",
        exchange="NSE",
        signal_time="10:30:00",
        ltp=2500.0,
        vwap=2490.0,
        rsi=55.0,
        volume_ratio=1.8,
        suggested_qty=100,
    )


# ── Decision Output ────────────────────────────────────────────────────────


@pytest.fixture
def valid_decision() -> DecisionOutput:
    """A valid EXECUTE decision that satisfies all hard risk rules."""
    return DecisionOutput(
        decision=Decision.EXECUTE,
        adjusted_qty=50,
        stop_loss_price=2475.0,    # 1.0% below 2500
        target_price=2550.0,      # 2.0% above 2500
        rationale="Good setup",
        product_type=ProductType.MIS,
        signal_audit=default_audit(),
    )


# ── Trade Model ─────────────────────────────────────────────────────────────


@pytest.fixture
def open_trade() -> MagicMock:
    """A mock Trade object representing an open MIS position."""
    trade = MagicMock(spec=Trade)
    trade.id = 1
    trade.stock = "RELIANCE"
    trade.entry_price = 2500.0
    trade.stop_loss_price = 2475.0       # 1.0% below entry
    trade.target_price = 2550.0          # 2.0% above entry
    trade.quantity = 100
    trade.product_type = "MIS"
    trade.status = "OPEN"
    trade.trade_date = date.today()
    trade.entry_time = dt_time(10, 0, 0)
    trade.realized_pnl = None
    trade.exit_price = None
    return trade
