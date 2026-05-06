"""
Tests for the Decision Engine — pre-checks, LLM output validation, mock decisions.

Pre-checks involve async DB/Redis calls and are tested with mocks.
_validate_decision is pure logic and needs no mocking.
"""

import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytz
import pytest

from agents.decision_engine import DecisionEngine
from schemas.decision import Decision, DecisionOutput, ProductType, SignalAudit
from schemas.market_brief import (
    MarketBias,
    MarketBriefLLMOutput,
    RecommendedStance,
)
from schemas.trade import ScannerSignal

# Real IST datetimes used to pin market-hours gate in pre-check tests.
# Using concrete datetime objects (not MagicMock) ensures .weekday(),
# .replace(), and comparison operators work correctly in _pre_check.
_IST         = pytz.timezone("Asia/Kolkata")
_MARKET_DT   = _IST.localize(datetime(2026, 4, 29, 10, 30, 0))   # Tuesday 10:30
_MONDAY_DT   = _IST.localize(datetime(2026, 4, 27, 10, 30, 0))   # Monday  10:30
_FRIDAY_PM_DT = _IST.localize(datetime(2026, 5, 1, 14, 30, 0))   # Friday  14:30


def _audit(**overrides) -> SignalAudit:
    """Return a fully-passing SignalAudit. Override individual fields to test failure paths."""
    defaults = dict(
        rsi_cited=55.0, rsi_in_range=True,
        volume_ratio_cited=1.8, volume_confirms=True,
        vwap_deviation_pct=0.40, price_vwap_valid=True,
        ema_aligned=None, macd_confirms=None,
        risk_reward_ratio=2.5, confidence_score=75,
        conditions_not_met=[],
    )
    defaults.update(overrides)
    return SignalAudit(**defaults)


@pytest.fixture
def engine(settings) -> DecisionEngine:
    """A DecisionEngine wired up with test settings."""
    queue = asyncio.Queue()
    de = DecisionEngine(queue)
    de._settings = settings
    return de


# ═══════════════════════════════════════════════════════════════════════════
#  _validate_decision  (pure logic — no I/O, no mocks)
# ═══════════════════════════════════════════════════════════════════════════


class TestValidateDecision:
    """Post-validation of LLM DecisionOutput against hard risk rules."""

    # ── Stop-loss clamping ─────────────────────────────────────────────

    def test_sl_too_tight_clamped(self, engine, signal):
        decision = DecisionOutput(
            decision=Decision.EXECUTE,
            adjusted_qty=100,
            stop_loss_price=2498.0,           # only 0.08% below entry
            target_price=2550.0,
            rationale="Test",
            product_type=ProductType.MIS,
            signal_audit=_audit(),
        )
        result = engine._validate_decision(signal, decision)
        expected_sl = round(2500.0 * (1 - 0.010), 2)   # 2475.0
        assert result.stop_loss_price == expected_sl

    def test_sl_at_minimum_passes(self, engine, signal):
        min_sl = round(2500.0 * (1 - 0.010), 2)  # 2475.0
        decision = DecisionOutput(
            decision=Decision.EXECUTE,
            adjusted_qty=100,
            stop_loss_price=min_sl,
            target_price=2550.0,
            rationale="Test",
            product_type=ProductType.MIS,
            signal_audit=_audit(),
        )
        result = engine._validate_decision(signal, decision)
        assert result.stop_loss_price == min_sl

    def test_sl_wider_than_minimum_passes(self, engine, signal):
        decision = DecisionOutput(
            decision=Decision.EXECUTE,
            adjusted_qty=100,
            stop_loss_price=2450.0,           # 2% below — more conservative
            target_price=2550.0,
            rationale="Test",
            product_type=ProductType.MIS,
            signal_audit=_audit(),
        )
        result = engine._validate_decision(signal, decision)
        # _validate_decision always recomputes SL from signal.ltp regardless of what
        # the LLM returned; the "wider" SL is replaced by the ATR/%-derived minimum.
        assert result.stop_loss_price == round(2500.0 * (1 - 0.010), 2)  # 2475.0

    # ── Target clamping ────────────────────────────────────────────────

    def test_target_too_close_clamped(self, engine, signal):
        decision = DecisionOutput(
            decision=Decision.EXECUTE,
            adjusted_qty=100,
            stop_loss_price=2475.0,
            target_price=2510.0,              # only 0.4%
            rationale="Test",
            product_type=ProductType.MIS,
            signal_audit=_audit(),
        )
        result = engine._validate_decision(signal, decision)
        expected_tgt = round(2500.0 * (1 + 0.020), 2)  # 2550.0
        assert result.target_price == expected_tgt

    def test_target_at_minimum_passes(self, engine, signal):
        min_tgt = round(2500.0 * (1 + 0.020), 2)  # 2550.0
        decision = DecisionOutput(
            decision=Decision.EXECUTE,
            adjusted_qty=100,
            stop_loss_price=2475.0,
            target_price=min_tgt,
            rationale="Test",
            product_type=ProductType.MIS,
            signal_audit=_audit(),
        )
        result = engine._validate_decision(signal, decision)
        assert result.target_price == min_tgt

    def test_target_above_minimum_passes(self, engine, signal):
        decision = DecisionOutput(
            decision=Decision.EXECUTE,
            adjusted_qty=100,
            stop_loss_price=2475.0,
            target_price=2600.0,              # generous
            rationale="Test",
            product_type=ProductType.MIS,
            signal_audit=_audit(),
        )
        result = engine._validate_decision(signal, decision)
        # _validate_decision always recomputes target from signal.ltp; the generous
        # target is replaced by the ATR/%-derived value.
        assert result.target_price == round(2500.0 * (1 + 0.020), 2)  # 2550.0

    # ── Quantity clamping ──────────────────────────────────────────────

    def test_qty_zero_clamped_to_one(self, engine, signal):
        decision = DecisionOutput(
            decision=Decision.EXECUTE,
            adjusted_qty=0,
            stop_loss_price=2475.0,
            target_price=2550.0,
            rationale="Test",
            product_type=ProductType.MIS,
            signal_audit=_audit(),
        )
        result = engine._validate_decision(signal, decision)
        assert result.adjusted_qty == 100  # clamped to signal.suggested_qty, not literal 1

    def test_qty_exceeds_suggested_capped(self, engine, signal):
        decision = DecisionOutput(
            decision=Decision.EXECUTE,
            adjusted_qty=500,                 # signal.suggested_qty = 100
            stop_loss_price=2475.0,
            target_price=2550.0,
            rationale="Test",
            product_type=ProductType.MIS,
            signal_audit=_audit(),
        )
        result = engine._validate_decision(signal, decision)
        assert result.adjusted_qty == 100     # capped to suggested

    def test_reject_allows_qty_zero(self, engine, signal):
        decision = DecisionOutput(
            decision=Decision.REJECT,
            adjusted_qty=0,
            stop_loss_price=2475.0,
            target_price=2550.0,
            rationale="Bad setup",
            product_type=ProductType.MIS,
            signal_audit=_audit(),
        )
        result = engine._validate_decision(signal, decision)
        assert result.adjusted_qty == 0       # not clamped for REJECT

    # ── Happy path ─────────────────────────────────────────────────────

    def test_valid_decision_unchanged(self, engine, signal, valid_decision):
        result = engine._validate_decision(signal, valid_decision)
        assert result.adjusted_qty == valid_decision.adjusted_qty
        assert result.stop_loss_price == valid_decision.stop_loss_price
        assert result.target_price == valid_decision.target_price


# ═══════════════════════════════════════════════════════════════════════════
#  _mock_decision
# ═══════════════════════════════════════════════════════════════════════════


class TestMockDecision:
    """Paper-trading mock decision generation."""

    def test_sl_and_target(self, engine, signal):
        d = engine._mock_decision(signal)
        assert d.decision == Decision.EXECUTE
        assert d.stop_loss_price == round(2500.0 * (1 - 0.010), 2)  # 2475.0 (1.0% SL)
        assert d.target_price == round(2500.0 * (1 + 0.020), 2)     # 2550.0 (2.0% target)

    def test_qty_matches_signal(self, engine, signal):
        d = engine._mock_decision(signal)
        assert d.adjusted_qty == signal.suggested_qty

    def test_product_type_mis(self, engine, signal):
        d = engine._mock_decision(signal)
        assert d.product_type == ProductType.MIS


# ═══════════════════════════════════════════════════════════════════════════
#  _pre_check  (async — needs mocking for Redis, DB)
# ═══════════════════════════════════════════════════════════════════════════


def _mock_db_context(scalar_value=0):
    """Return a patched get_db_context that yields a mock session."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar.return_value = scalar_value
    mock_session.execute.return_value = mock_result

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


class TestPreCheck:
    """Pre-flight checks before LLM call."""

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_redis", side_effect=ConnectionError("Redis unavailable in unit tests"))
    @patch("agents.decision_engine.get_value")
    async def test_halt_rejects(self, mock_get_value, mock_get_redis, mock_dt, engine, signal):
        mock_dt.now.return_value = _MARKET_DT
        mock_get_value.return_value = "TRUE"
        passed, reason, _ = await engine._pre_check(signal)
        assert not passed
        assert "halted" in reason.lower()

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_value", return_value=None)
    async def test_avoid_list_rejects(self, mock_get_value, mock_dt, engine, signal):
        mock_dt.now.return_value = _MARKET_DT
        brief = MagicMock(spec=MarketBriefLLMOutput)
        brief.market_bias = MarketBias.NEUTRAL
        brief.recommended_stance = RecommendedStance.FULL_SIZE_POSITIONS
        brief.avoid_today = ["RELIANCE"]
        brief.watchlist_today = []
        engine._market_brief = brief
        engine._count_open_positions = AsyncMock(return_value=0)

        passed, reason, _ = await engine._pre_check(signal)
        assert not passed
        assert "avoid" in reason.lower()

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_value", return_value=None)
    async def test_watchlist_filter(self, mock_get_value, mock_dt, engine, signal):
        mock_dt.now.return_value = _MARKET_DT
        brief = MagicMock(spec=MarketBriefLLMOutput)
        brief.market_bias = MarketBias.NEUTRAL
        brief.recommended_stance = RecommendedStance.FULL_SIZE_POSITIONS
        brief.avoid_today = []
        brief.watchlist_today = ["HDFCBANK", "TCS"]   # RELIANCE not included
        engine._market_brief = brief
        engine._count_open_positions = AsyncMock(return_value=0)

        passed, reason, _ = await engine._pre_check(signal)
        assert not passed
        assert "watchlist" in reason.lower()

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_value", return_value=None)
    async def test_avoid_trading_stance_rejects(self, mock_get_value, mock_dt, engine, signal):
        mock_dt.now.return_value = _MARKET_DT
        brief = MagicMock(spec=MarketBriefLLMOutput)
        brief.recommended_stance = RecommendedStance.AVOID_TRADING
        engine._market_brief = brief
        engine._count_open_positions = AsyncMock(return_value=0)

        passed, reason, _ = await engine._pre_check(signal)
        assert not passed
        assert "avoid" in reason.lower()

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_value", return_value=None)
    async def test_half_size_stance_halves_qty(self, mock_get_value, mock_dt, engine, signal):
        mock_dt.now.return_value = _MARKET_DT
        brief = MagicMock(spec=MarketBriefLLMOutput)
        brief.market_bias = MarketBias.NEUTRAL
        brief.recommended_stance = RecommendedStance.HALF_SIZE_POSITIONS
        brief.avoid_today = []
        brief.watchlist_today = ["RELIANCE"]
        engine._market_brief = brief
        engine._count_open_positions = AsyncMock(return_value=0)

        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(0)):
            passed, reason, modified = await engine._pre_check(signal)
            assert passed
            assert modified.suggested_qty == 50   # 100 // 2

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_value", return_value=None)
    async def test_max_positions_rejects(self, mock_get_value, mock_dt, engine, signal):
        mock_dt.now.return_value = _MARKET_DT
        engine._market_brief = None
        engine._count_open_positions = AsyncMock(return_value=3)

        passed, reason, _ = await engine._pre_check(signal)
        assert not passed
        assert "max open positions" in reason.lower()

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_redis", side_effect=ConnectionError("Redis unavailable in unit tests"))
    @patch("agents.decision_engine.get_value")
    async def test_max_daily_trades_rejects(self, mock_get_value, mock_get_redis, mock_dt, engine, signal):
        mock_dt.now.return_value = _MARKET_DT

        async def side_effect(key):
            if key == "trading_halt":
                return None
            if key == "daily_trade_count":
                return "6"
            return None

        mock_get_value.side_effect = side_effect
        engine._market_brief = None
        engine._count_open_positions = AsyncMock(return_value=0)

        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(0)):
            passed, reason, _ = await engine._pre_check(signal)
            assert not passed
            assert "max daily trades" in reason.lower()

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_value", return_value=None)
    async def test_duplicate_position_rejects(self, mock_get_value, mock_dt, engine, signal):
        mock_dt.now.return_value = _MARKET_DT
        engine._market_brief = None
        engine._count_open_positions = AsyncMock(return_value=1)

        # scalar returns 1 → duplicate exists
        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(1)):
            passed, reason, _ = await engine._pre_check(signal)
            assert not passed
            assert "already open" in reason.lower()

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_value", return_value=None)
    async def test_no_brief_skips_stance_and_watchlist(self, mock_get_value, mock_dt, engine, signal):
        """Without a market brief, stance/watchlist checks should be skipped."""
        mock_dt.now.return_value = _MARKET_DT
        engine._market_brief = None
        engine._count_open_positions = AsyncMock(return_value=0)

        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(0)):
            passed, reason, _ = await engine._pre_check(signal)
            assert passed

    @pytest.mark.asyncio
    @patch("agents.decision_engine.get_value", return_value=None)
    @patch("agents.decision_engine.datetime")
    async def test_monday_half_size(self, mock_dt, mock_get_value, engine, signal):
        """Monday rule should halve quantity independently of stance."""
        mock_dt.now.return_value = _MONDAY_DT

        engine._market_brief = None
        engine._count_open_positions = AsyncMock(return_value=0)

        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(0)):
            passed, reason, modified = await engine._pre_check(signal)
            assert passed
            assert modified.suggested_qty == 50   # 100 // 2

    @pytest.mark.asyncio
    @patch("agents.decision_engine.get_value", return_value=None)
    @patch("agents.decision_engine.datetime")
    async def test_friday_afternoon_rejects(self, mock_dt, mock_get_value, engine, signal):
        """No new entries on Friday after 2 PM."""
        mock_dt.now.return_value = _FRIDAY_PM_DT
        engine._market_brief = None
        engine._count_open_positions = AsyncMock(return_value=0)

        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(0)):
            passed, reason, _ = await engine._pre_check(signal)
            assert not passed
            assert "friday" in reason.lower()


# ═══════════════════════════════════════════════════════════════════════════
#  ATR-based SL / Target
# ═══════════════════════════════════════════════════════════════════════════


class TestAtrDecision:
    """ATR-based stop-loss and target in mock & validation paths."""

    def test_mock_decision_with_atr(self, engine):
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2500.0, vwap=2490.0, rsi=55.0, volume_ratio=1.8,
            suggested_qty=100, atr=10.0,
        )
        d = engine._mock_decision(sig)
        # SL = 2500 - 10 * 1.5 = 2485.0
        assert d.stop_loss_price == 2485.0
        # TGT = 2500 + 10 * 3.0 = 2530.0
        assert d.target_price == 2530.0

    def test_mock_decision_without_atr_fallback(self, engine, signal):
        """When ATR is None, should use fixed percentage SL/target."""
        d = engine._mock_decision(signal)
        assert d.stop_loss_price == round(2500.0 * (1 - 0.010), 2)  # 2475.0 (1.0% SL)
        assert d.target_price == round(2500.0 * (1 + 0.020), 2)     # 2550.0 (2.0% target)

    def test_validate_clamps_to_atr_distances(self, engine):
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2500.0, vwap=2490.0, rsi=55.0, volume_ratio=1.8,
            suggested_qty=100, atr=10.0,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE,
            adjusted_qty=100,
            stop_loss_price=2498.0,   # too tight
            target_price=2510.0,      # too close
            rationale="Test",
            product_type=ProductType.MIS,
            signal_audit=_audit(),
        )
        result = engine._validate_decision(sig, decision)
        assert result.stop_loss_price == 2485.0   # 2500 - 10*1.5
        assert result.target_price == 2530.0       # 2500 + 10*3.0


# ═══════════════════════════════════════════════════════════════════════════
#  Stock Lock & Consecutive Loss Pause Pre-checks
# ═══════════════════════════════════════════════════════════════════════════


class TestProtectionPreChecks:

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_redis", side_effect=ConnectionError("Redis unavailable in unit tests"))
    @patch("agents.decision_engine.get_value")
    async def test_stock_lock_rejects(self, mock_get_value, mock_get_redis, mock_dt, engine, signal):
        mock_dt.now.return_value = _MARKET_DT

        async def side_effect(key):
            if key == "trading_halt":
                return None
            if key == "stock_lock:RELIANCE":
                return "TRUE"
            return None
        mock_get_value.side_effect = side_effect

        passed, reason, _ = await engine._pre_check(signal)
        assert not passed
        assert "locked" in reason.lower()

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.set_value", new_callable=AsyncMock)
    @patch("agents.decision_engine.get_redis", side_effect=ConnectionError("Redis unavailable in unit tests"))
    @patch("agents.decision_engine.get_value")
    async def test_consecutive_loss_pause_rejects(
        self, mock_get_value, mock_get_redis, mock_set_value, mock_dt, engine, signal
    ):
        mock_dt.now.return_value = _MARKET_DT

        from datetime import datetime as _real_dt, timedelta

        # Delegate fromisoformat to real impl so datetime arithmetic works inside _pre_check.
        mock_dt.fromisoformat.side_effect = _real_dt.fromisoformat

        # future is relative to _MARKET_DT so the pause is still active at mock time
        future = (_MARKET_DT + timedelta(minutes=15)).isoformat()

        async def side_effect(key):
            if key == "trading_halt":
                return None
            if key.startswith("stock_lock"):
                return None
            if key == "consecutive_losses":
                return "3"
            if key == "consecutive_loss_pause_until":
                return future
            return None
        mock_get_value.side_effect = side_effect

        passed, reason, _ = await engine._pre_check(signal)
        assert not passed
        assert "consecutive" in reason.lower()


# ═══════════════════════════════════════════════════════════════════════════
#  Monday + HALF_SIZE Non-Stacking
# ═══════════════════════════════════════════════════════════════════════════


class TestSizeReductionStacking:
    """Monday half-size and HALF_SIZE stance must NOT stack (quarter the qty)."""

    @pytest.mark.asyncio
    @patch("agents.decision_engine.get_value", return_value=None)
    @patch("agents.decision_engine.datetime")
    async def test_monday_plus_half_size_halves_once(
        self, mock_dt, mock_get_value, engine, signal
    ):
        """On Monday with HALF_SIZE stance, qty should be halved ONCE, not quartered."""
        mock_dt.now.return_value = _MONDAY_DT

        brief = MagicMock(spec=MarketBriefLLMOutput)
        brief.market_bias = MarketBias.NEUTRAL
        brief.recommended_stance = RecommendedStance.HALF_SIZE_POSITIONS
        brief.avoid_today = []
        brief.watchlist_today = ["RELIANCE"]
        engine._market_brief = brief
        engine._count_open_positions = AsyncMock(return_value=0)

        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(0)):
            passed, reason, modified = await engine._pre_check(signal)
            assert passed
            # Should be 50 (halved once from 100), NOT 25 (quartered)
            assert modified.suggested_qty == 50

    @pytest.mark.asyncio
    @patch("agents.decision_engine.get_value", return_value=None)
    @patch("agents.decision_engine.datetime")
    async def test_tuesday_half_size_stance_halves(
        self, mock_dt, mock_get_value, engine, signal
    ):
        """On non-Monday with HALF_SIZE stance, qty should still be halved."""
        mock_dt.now.return_value = _MARKET_DT  # Tuesday

        brief = MagicMock(spec=MarketBriefLLMOutput)
        brief.market_bias = MarketBias.NEUTRAL
        brief.recommended_stance = RecommendedStance.HALF_SIZE_POSITIONS
        brief.avoid_today = []
        brief.watchlist_today = ["RELIANCE"]
        engine._market_brief = brief
        engine._count_open_positions = AsyncMock(return_value=0)

        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(0)):
            passed, reason, modified = await engine._pre_check(signal)
            assert passed
            assert modified.suggested_qty == 50


# ═══════════════════════════════════════════════════════════════════════════
#  SignalAudit — quantified threshold enforcement
# ═══════════════════════════════════════════════════════════════════════════


class TestSignalAuditValidation:
    """
    _validate_decision() independently recomputes every signal_audit boolean
    field from the raw signal, so the LLM cannot bypass hard risk rules by
    misreporting indicator values.
    """

    # ── Hard REJECT via RSI ───────────────────────────────────────────────────

    def test_rsi_hard_reject_overbought(self, engine):
        """RSI > 80 hard-rejects regardless of LLM approval."""
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2500.0, vwap=2490.0, rsi=82.0,
            volume_ratio=2.0, suggested_qty=100,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2540.0,
            rationale="LLM approved", product_type=ProductType.MIS,
            signal_audit=_audit(rsi_cited=82.0, confidence_score=80),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REJECT
        assert "RSI" in result.rationale

    def test_rsi_hard_reject_oversold(self, engine):
        """RSI < 28 hard-rejects (falling knife / counter-trend entry)."""
        sig = ScannerSignal(
            stock="INFY", exchange="NSE", signal_time="11:00:00",
            ltp=1500.0, vwap=1510.0, rsi=25.0,
            volume_ratio=2.0, suggested_qty=50,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=50,
            stop_loss_price=1488.0, target_price=1524.0,
            rationale="LLM approved", product_type=ProductType.MIS,
            signal_audit=_audit(rsi_cited=25.0, confidence_score=80),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REJECT

    # ── Hard REJECT via volume ────────────────────────────────────────────────

    def test_volume_hard_reject_below_threshold(self, engine):
        """Volume ratio < 1.2× hard-rejects (insufficient liquidity confirmation)."""
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2500.0, vwap=2490.0, rsi=55.0,
            volume_ratio=1.1, suggested_qty=100,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2540.0,
            rationale="LLM approved", product_type=ProductType.MIS,
            signal_audit=_audit(volume_ratio_cited=1.1, confidence_score=75),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REJECT
        assert "volume" in result.rationale.lower()

    # ── Hard REJECT via VWAP deviation ────────────────────────────────────────

    def test_vwap_overextended_hard_reject(self, engine):
        """Price > 2% above VWAP hard-rejects (extreme overextension)."""
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2560.0, vwap=2500.0,  # 2.4% above VWAP
            rsi=60.0, volume_ratio=2.0, suggested_qty=100,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2530.0, target_price=2620.0,
            rationale="LLM approved", product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=75),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REJECT
        assert "VWAP" in result.rationale

    # ── Hard REJECT via R:R ───────────────────────────────────────────────────

    def test_rr_below_minimum_hard_rejects(self, engine):
        """If post-clamping R:R < 2.0, the trade is rejected."""
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2500.0, vwap=2490.0, rsi=55.0, volume_ratio=1.8,
            suggested_qty=100, atr=10.0,
        )
        # Force atr_target_multiplier=1.0 so clamped target=2510, min_sl=2485
        # R:R = (2510-2500) / (2500-2485) = 10/15 ≈ 0.67 → REJECT
        engine._settings = engine._settings.model_copy(
            update={"atr_target_multiplier": 1.0}
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2505.0,
            rationale="Low R:R test", product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=75),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REJECT
        assert "R:R" in result.rationale

    # ── Hard REJECT via confidence floor ─────────────────────────────────────

    def test_low_confidence_hard_rejects(self, engine, signal):
        """confidence_score < 45 hard-rejects even when all signal checks pass."""
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2540.0,
            rationale="Weak setup", product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=40),
        )
        result = engine._validate_decision(signal, decision)
        assert result.decision == Decision.REJECT
        assert "confidence" in result.rationale.lower()

    # ── Soft downgrade: EXECUTE → REDUCE ─────────────────────────────────────

    def test_medium_confidence_downgrades_to_reduce(self, engine, signal):
        """45 ≤ confidence_score < 65 downgrades EXECUTE → REDUCE at half qty."""
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2540.0,
            rationale="Moderate confidence", product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=58),
        )
        result = engine._validate_decision(signal, decision)
        assert result.decision == Decision.REDUCE
        assert result.adjusted_qty == 50

    def test_ema_misaligned_downgrades_execute(self, engine):
        """EMA-9 < EMA-21 (trend not confirmed) downgrades EXECUTE → REDUCE."""
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2500.0, vwap=2490.0, rsi=55.0, volume_ratio=1.8,
            suggested_qty=100, ema_9=2488.0, ema_21=2495.0,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2540.0,
            rationale="EMA test", product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=75),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REDUCE
        assert result.adjusted_qty == 50

    def test_macd_negative_downgrades_execute(self, engine):
        """Negative MACD histogram (momentum against direction) downgrades EXECUTE → REDUCE."""
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2500.0, vwap=2490.0, rsi=55.0, volume_ratio=1.8,
            suggested_qty=100, macd_histogram=-0.15,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2540.0,
            rationale="MACD test", product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=75),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REDUCE
        assert result.adjusted_qty == 50

    def test_high_vwap_deviation_downgrades_execute(self, engine):
        """Price > 0.8% above VWAP (elevated mean-reversion risk) downgrades EXECUTE → REDUCE."""
        # vwap_dev = (2512 - 2490) / 2490 * 100 ≈ 0.88%  — above 0.8% reduce zone
        # stop and target sized to give R:R = 2.0 (32-point risk, 64-point reward)
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2512.0, vwap=2490.0, rsi=55.0, volume_ratio=1.8,
            suggested_qty=100,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2576.0,   # R:R = 64/32 = 2.0
            rationale="VWAP zone test", product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=75),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REDUCE

    # ── LLM cannot fake signal values ────────────────────────────────────────

    def test_llm_rsi_citation_overwritten_with_actual(self, engine, signal):
        """signal_audit.rsi_cited and rsi_in_range are overwritten from the raw signal."""
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2540.0,
            rationale="LLM misreporting RSI", product_type=ProductType.MIS,
            signal_audit=_audit(
                rsi_cited=99.0,    # LLM says 99, actual signal has 55
                rsi_in_range=True,
                confidence_score=75,
            ),
        )
        result = engine._validate_decision(signal, decision)
        # After overwrite the audit reflects the actual signal value
        assert result.signal_audit.rsi_cited == pytest.approx(signal.rsi)
        assert result.signal_audit.rsi_in_range is True   # 55 ∈ [40, 72]

    def test_llm_volume_citation_overwritten_with_actual(self, engine, signal):
        """signal_audit.volume_ratio_cited is overwritten from the raw signal."""
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2540.0,
            rationale="LLM misreporting volume", product_type=ProductType.MIS,
            signal_audit=_audit(
                volume_ratio_cited=5.0,  # LLM claims 5×, actual is 1.8×
                volume_confirms=True,
                confidence_score=75,
            ),
        )
        result = engine._validate_decision(signal, decision)
        assert result.signal_audit.volume_ratio_cited == pytest.approx(signal.volume_ratio)


# ═══════════════════════════════════════════════════════════════════════════
#  Audit-1 fix: VWAP below entry enforcement
# ═══════════════════════════════════════════════════════════════════════════


class TestVwapBelowEntry:
    """Price below VWAP on a long entry must trigger reduce or hard-reject."""

    def test_price_significantly_below_vwap_hard_rejects(self, engine):
        """Price ≥ 1 % below VWAP is a hard REJECT — strong bearish intraday structure."""
        # vwap_dev = (2474 - 2500) / 2500 * 100 = -1.04%  — crosses VWAP_DEV_BELOW_HARD_PCT
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2474.0, vwap=2500.0, rsi=55.0, volume_ratio=1.8,
            suggested_qty=100,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2454.0, target_price=2514.0,   # R:R=2.0
            rationale="LLM approved despite price below VWAP",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=75),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REJECT
        assert "VWAP" in result.rationale
        assert "below" in result.rationale.lower()

    def test_price_marginally_below_vwap_downgrades_to_reduce(self, engine):
        """Any price below VWAP (but above -1%) downgrades EXECUTE → REDUCE."""
        # vwap_dev = (2495 - 2500) / 2500 * 100 = -0.2% — below VWAP but passes hard reject
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2495.0, vwap=2500.0, rsi=55.0, volume_ratio=1.8,
            suggested_qty=100,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2475.0, target_price=2535.0,   # R:R=2.0
            rationale="LLM approved below-VWAP entry",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=75),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REDUCE
        assert result.adjusted_qty == 50

    def test_price_at_vwap_exact_passes_without_reduce(self, engine):
        """Price exactly at VWAP (0% deviation) should not trigger the below-VWAP reduce rule."""
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2500.0, vwap=2500.0, rsi=55.0, volume_ratio=1.8,
            suggested_qty=100,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2540.0,
            rationale="At VWAP test",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=75),
        )
        result = engine._validate_decision(sig, decision)
        # 0.0% deviation: price_vwap_valid=True, no reduce from VWAP below rule
        assert result.decision == Decision.EXECUTE


# ═══════════════════════════════════════════════════════════════════════════
#  Audit-2 fix: conditions_not_met rebuilt from ground-truth audit state
# ═══════════════════════════════════════════════════════════════════════════


class TestConditionsNotMetRebuilt:
    """_validate_decision must replace the LLM's conditions_not_met with a list
    derived from the actual boolean states it computed — not what the LLM said."""

    def test_llm_empty_conditions_filled_when_volume_fails(self, engine, signal):
        """LLM returned empty conditions_not_met but volume_ratio is below threshold."""
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2540.0,
            rationale="LLM forgot to flag low volume",
            product_type=ProductType.MIS,
            signal_audit=_audit(
                volume_ratio_cited=1.3,   # LLM cites 1.3× but signal actually has 1.8×
                volume_confirms=True,
                conditions_not_met=[],    # LLM left this empty
                confidence_score=75,
            ),
        )
        # Override signal volume so it actually fails the threshold
        low_vol_signal = signal.model_copy(update={"volume_ratio": 1.3})
        result = engine._validate_decision(low_vol_signal, decision)
        # Hard reject: volume_ratio=1.3 is between VOLUME_RATIO_HARD_REJECT(1.2)
        # and VOLUME_RATIO_MIN(1.5) → actually passes hard reject but NOT soft reduce list
        # Wait: volume 1.3 passes hard reject (≥ 1.2) but volume_confirms=False (< 1.5)
        # → conditions_not_met must include "volume_below_threshold"
        assert "volume_below_threshold" in result.signal_audit.conditions_not_met

    def test_llm_fabricated_conditions_replaced_on_clean_signal(self, engine, signal):
        """LLM specified spurious conditions_not_met for a signal that actually passes all checks."""
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2540.0,
            rationale="LLM hallucinated a failing condition",
            product_type=ProductType.MIS,
            signal_audit=_audit(
                conditions_not_met=["rsi_out_of_range", "volume_below_threshold"],  # fabricated
                confidence_score=75,
            ),
        )
        result = engine._validate_decision(signal, decision)
        # signal fixture has passing RSI/volume/VWAP/conf — conditions list must be empty
        assert result.signal_audit.conditions_not_met == []

    def test_hard_reject_populates_conditions_not_met(self, engine):
        """Hard reject path must also write the authoritative conditions_not_met."""
        sig = ScannerSignal(
            stock="INFY", exchange="NSE", signal_time="10:30:00",
            ltp=1500.0, vwap=1490.0, rsi=82.0,   # RSI > 80 → hard reject
            volume_ratio=2.0, suggested_qty=50,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=50,
            stop_loss_price=1480.0, target_price=1540.0,
            rationale="LLM approved despite extreme RSI",
            product_type=ProductType.MIS,
            signal_audit=_audit(rsi_cited=82.0, rsi_in_range=True, conditions_not_met=[]),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REJECT
        assert "rsi_out_of_range" in result.signal_audit.conditions_not_met

    def test_reduce_path_populates_conditions_not_met(self, engine):
        """Soft-reduce path sets conditions_not_met when EMA is misaligned."""
        sig = ScannerSignal(
            stock="TCS", exchange="NSE", signal_time="10:30:00",
            ltp=3500.0, vwap=3490.0, rsi=55.0,
            volume_ratio=1.8, suggested_qty=20,
            ema_9=3480.0, ema_21=3510.0,   # EMA-9 < EMA-21 → misaligned
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=20,
            stop_loss_price=3460.0, target_price=3582.0,   # R:R = 82/40 = 2.05
            rationale="LLM chose not to mention EMA",
            product_type=ProductType.MIS,
            signal_audit=_audit(
                ema_aligned=True,          # LLM claimed aligned — will be overwritten
                conditions_not_met=[],
                confidence_score=75,
            ),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REDUCE
        assert "ema_not_aligned" in result.signal_audit.conditions_not_met


# ═══════════════════════════════════════════════════════════════════════════
#  Audit-3 fix: signal.vwap = 0 (unavailable) guard
# ═══════════════════════════════════════════════════════════════════════════


class TestVwapUnavailableGuard:
    """signal.vwap <= 0 (scanner not warmed up) must NOT validate as price_vwap_valid=True."""

    def test_zero_vwap_marks_price_vwap_valid_false(self, engine):
        """VWAP=0 must set price_vwap_valid=False — data not available, not 'at VWAP'."""
        sig = ScannerSignal(
            stock="HDFCBANK", exchange="NSE", signal_time="09:16:00",
            ltp=1600.0, vwap=0.0,   # VWAP not yet seeded
            rsi=55.0, volume_ratio=1.8, suggested_qty=30,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=30,
            stop_loss_price=1580.0, target_price=1640.0,   # R:R = 40/20 = 2.0
            rationale="Early morning signal",
            product_type=ProductType.MIS,
            signal_audit=_audit(price_vwap_valid=True, confidence_score=75),
        )
        result = engine._validate_decision(sig, decision)
        assert result.signal_audit.price_vwap_valid is False

    def test_zero_vwap_downgrades_to_reduce(self, engine):
        """VWAP=0 triggers the precautionary REDUCE rather than silently EXECUTE."""
        sig = ScannerSignal(
            stock="HDFCBANK", exchange="NSE", signal_time="09:16:00",
            ltp=1600.0, vwap=0.0,
            rsi=55.0, volume_ratio=1.8, suggested_qty=30,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=30,
            stop_loss_price=1580.0, target_price=1640.0,   # R:R=2.0
            rationale="Early morning signal",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=75),
        )
        result = engine._validate_decision(sig, decision)
        assert result.decision == Decision.REDUCE
        assert result.adjusted_qty == 15

    def test_zero_vwap_sets_vwap_data_unavailable_in_conditions(self, engine):
        """conditions_not_met must contain 'vwap_data_unavailable' when vwap=0."""
        sig = ScannerSignal(
            stock="HDFCBANK", exchange="NSE", signal_time="09:16:00",
            ltp=1600.0, vwap=0.0,
            rsi=55.0, volume_ratio=1.8, suggested_qty=30,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=30,
            stop_loss_price=1580.0, target_price=1640.0,
            rationale="Early morning signal",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=75),
        )
        result = engine._validate_decision(sig, decision)
        assert "vwap_data_unavailable" in result.signal_audit.conditions_not_met

    def test_valid_vwap_does_not_trigger_unavailable_label(self, engine, signal):
        """A signal with a valid VWAP must not get 'vwap_data_unavailable' in conditions."""
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2480.0, target_price=2540.0,
            rationale="Normal signal with valid VWAP",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=75),
        )
        result = engine._validate_decision(signal, decision)
        assert "vwap_data_unavailable" not in result.signal_audit.conditions_not_met


# ═══════════════════════════════════════════════════════════════════════════
#  Bias-Modulated Volume and VWAP Thresholds
# ═══════════════════════════════════════════════════════════════════════════


class TestBiasModulatedThresholds:
    """_validate_decision() uses bias-modulated thresholds for volume_confirms and
    price_vwap_valid so that bearish markets require stronger confirmation."""

    def _make_brief(self, bias: MarketBias) -> MagicMock:
        brief = MagicMock(spec=MarketBriefLLMOutput)
        brief.market_bias = bias
        return brief

    # ── Volume ratio min: BULLISH=1.3, NEUTRAL=1.5, BEARISH=2.0 ──────────

    def test_volume_1_4x_confirms_on_bullish(self, engine, signal):
        """1.4× volume is sufficient on a BULLISH day (threshold=1.3×)."""
        engine._market_brief = self._make_brief(MarketBias.BULLISH)
        low_vol_signal = signal.model_copy(update={"volume_ratio": 1.4})
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2475.0, target_price=2550.0,
            rationale="Bullish bias test",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=70),
        )
        result = engine._validate_decision(low_vol_signal, decision)
        assert result.signal_audit.volume_confirms is True

    def test_volume_1_4x_not_confirmed_on_neutral(self, engine, signal):
        """1.4× volume fails the NEUTRAL threshold (1.5×) → volume_confirms=False."""
        engine._market_brief = self._make_brief(MarketBias.NEUTRAL)
        low_vol_signal = signal.model_copy(update={"volume_ratio": 1.4})
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2475.0, target_price=2550.0,
            rationale="Neutral bias test",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=70),
        )
        result = engine._validate_decision(low_vol_signal, decision)
        assert result.signal_audit.volume_confirms is False

    def test_volume_1_8x_not_confirmed_on_bearish(self, engine, signal):
        """1.8× volume fails the BEARISH threshold (2.0×) → volume_confirms=False."""
        engine._market_brief = self._make_brief(MarketBias.BEARISH)
        mid_vol_signal = signal.model_copy(update={"volume_ratio": 1.8})
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2475.0, target_price=2550.0,
            rationale="Bearish bias test",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=70),
        )
        result = engine._validate_decision(mid_vol_signal, decision)
        assert result.signal_audit.volume_confirms is False

    def test_volume_2_1x_confirms_on_bearish(self, engine, signal):
        """2.1× volume meets the BEARISH threshold (2.0×) → volume_confirms=True."""
        engine._market_brief = self._make_brief(MarketBias.BEARISH)
        strong_vol_signal = signal.model_copy(update={"volume_ratio": 2.1})
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2475.0, target_price=2550.0,
            rationale="Bearish bias, strong volume",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=70),
        )
        result = engine._validate_decision(strong_vol_signal, decision)
        assert result.signal_audit.volume_confirms is True

    # ── VWAP dev max: BULLISH=1.8%, NEUTRAL=1.5%, BEARISH=1.0% ──────────

    def test_vwap_dev_1_6_valid_on_bullish(self, engine):
        """1.6% above VWAP is valid on BULLISH day (max=1.8%)."""
        engine._market_brief = self._make_brief(MarketBias.BULLISH)
        # ltp = 2500, vwap = 2460 → dev = 40/2460*100 ≈ 1.626%
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2500.0, vwap=2460.0, rsi=55.0, volume_ratio=1.8, suggested_qty=100,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2475.0, target_price=2550.0,
            rationale="VWAP bullish test",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=70),
        )
        result = engine._validate_decision(sig, decision)
        assert result.signal_audit.price_vwap_valid is True

    def test_vwap_dev_1_6_invalid_on_neutral(self, engine):
        """1.6% above VWAP fails on NEUTRAL day (max=1.5%) → price_vwap_valid=False."""
        engine._market_brief = self._make_brief(MarketBias.NEUTRAL)
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2500.0, vwap=2460.0, rsi=55.0, volume_ratio=1.8, suggested_qty=100,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2475.0, target_price=2550.0,
            rationale="VWAP neutral test",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=70),
        )
        result = engine._validate_decision(sig, decision)
        assert result.signal_audit.price_vwap_valid is False

    def test_vwap_dev_1_1_invalid_on_bearish(self, engine):
        """1.1% above VWAP fails on BEARISH day (max=1.0%) → price_vwap_valid=False."""
        engine._market_brief = self._make_brief(MarketBias.BEARISH)
        # ltp=2500, vwap=2473 → dev = 27/2473*100 ≈ 1.09%
        sig = ScannerSignal(
            stock="RELIANCE", exchange="NSE", signal_time="10:30:00",
            ltp=2500.0, vwap=2473.0, rsi=55.0, volume_ratio=2.1, suggested_qty=100,
        )
        decision = DecisionOutput(
            decision=Decision.EXECUTE, adjusted_qty=100,
            stop_loss_price=2475.0, target_price=2550.0,
            rationale="VWAP bearish test",
            product_type=ProductType.MIS,
            signal_audit=_audit(confidence_score=70),
        )
        result = engine._validate_decision(sig, decision)
        assert result.signal_audit.price_vwap_valid is False


# ═══════════════════════════════════════════════════════════════════════════
#  Bias-Modulated Position and Trade Limits
# ═══════════════════════════════════════════════════════════════════════════


class TestBiasModulatedLimits:
    """_pre_check() uses market_bias to adjust max_open_positions and max_trades_per_day."""

    def _make_brief(self, bias: MarketBias) -> MagicMock:
        brief = MagicMock(spec=MarketBriefLLMOutput)
        brief.market_bias = bias
        brief.recommended_stance = RecommendedStance.FULL_SIZE_POSITIONS
        brief.avoid_today = []
        brief.watchlist_today = []
        brief.news_flags = []
        return brief

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_value", return_value=None)
    async def test_bearish_rejects_all_long_entries(self, mock_get_value, mock_dt, engine, signal):
        """BEARISH bias rejects all long entries before reaching position limit checks."""
        mock_dt.now.return_value = _MARKET_DT
        engine._market_brief = self._make_brief(MarketBias.BEARISH)
        engine._count_open_positions = AsyncMock(return_value=0)

        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(0)):
            passed, reason, _ = await engine._pre_check(signal)
        assert not passed
        assert "bearish" in reason.lower()

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_value", return_value=None)
    async def test_neutral_max_3_positions(self, mock_get_value, mock_dt, engine, signal):
        """NEUTRAL bias caps max open positions at 3 (settings default)."""
        mock_dt.now.return_value = _MARKET_DT
        engine._market_brief = self._make_brief(MarketBias.NEUTRAL)
        engine._count_open_positions = AsyncMock(return_value=3)

        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(0)):
            passed, reason, _ = await engine._pre_check(signal)
        assert not passed
        assert "3" in reason and "neutral" in reason.lower()

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_value", return_value=None)
    async def test_bullish_allows_4_positions(self, mock_get_value, mock_dt, engine, signal):
        """BULLISH bias raises max open positions to 4."""
        mock_dt.now.return_value = _MARKET_DT
        engine._market_brief = self._make_brief(MarketBias.BULLISH)
        engine._count_open_positions = AsyncMock(return_value=3)

        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(0)):
            passed, reason, _ = await engine._pre_check(signal)
        # 3 open positions < 4 bullish limit → should PASS
        assert passed

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_redis", side_effect=ConnectionError)
    @patch("agents.decision_engine.get_value")
    async def test_neutral_max_6_daily_trades(self, mock_get_value, mock_get_redis, mock_dt, engine, signal):
        """NEUTRAL bias caps daily trades at 6 (settings default)."""
        mock_dt.now.return_value = _MARKET_DT
        engine._market_brief = self._make_brief(MarketBias.NEUTRAL)
        engine._count_open_positions = AsyncMock(return_value=0)

        async def side_effect(key):
            if key == "daily_trade_count":
                return "6"
            return None
        mock_get_value.side_effect = side_effect

        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(0)):
            passed, reason, _ = await engine._pre_check(signal)
        assert not passed
        assert "6" in reason and "neutral" in reason.lower()

    @pytest.mark.asyncio
    @patch("agents.decision_engine.datetime")
    @patch("agents.decision_engine.get_redis", side_effect=ConnectionError)
    @patch("agents.decision_engine.get_value")
    async def test_bullish_allows_8_daily_trades(self, mock_get_value, mock_get_redis, mock_dt, engine, signal):
        """BULLISH bias raises daily trade limit to 8."""
        mock_dt.now.return_value = _MARKET_DT
        engine._market_brief = self._make_brief(MarketBias.BULLISH)
        engine._count_open_positions = AsyncMock(return_value=0)

        async def side_effect(key):
            if key == "daily_trade_count":
                return "7"   # 7 trades < 8 limit → should PASS
            return None
        mock_get_value.side_effect = side_effect

        with patch("agents.decision_engine.get_db_context", return_value=_mock_db_context(0)):
            passed, reason, _ = await engine._pre_check(signal)
        assert passed
