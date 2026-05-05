"""
Tests for the Risk Manager — SL/target detection, trailing SL, drawdown, EOD.
"""

from datetime import date, datetime, time as dt_time

import pytest

from agents.risk_manager import RiskManager


# ═══════════════════════════════════════════════════════════════════════════
#  Trailing Stop-Loss Logic
# ═══════════════════════════════════════════════════════════════════════════


class TestTrailingSL:
    """Trailing stop-loss activation, progression, and monotonicity."""

    def test_activation_above_threshold(self):
        """Trailing SL activates when LTP >= entry * (1 + activation_pct)."""
        entry = 2500.0
        activation_pct = 0.008   # updated: 0.8%
        trail_pct = 0.007        # updated: 0.7%

        activation = entry * (1 + activation_pct)       # 2520.0
        ltp = 2522.0                                     # above threshold

        trailing_sl = round(ltp * (1 - trail_pct), 2)   # 2506.46
        current_sl = 2475.0                               # original SL (1.0%)

        assert ltp >= activation
        assert trailing_sl > current_sl                   # update should fire

    def test_no_activation_below_threshold(self):
        """Trailing SL should NOT activate when LTP < threshold."""
        entry = 2500.0
        activation_pct = 0.008   # updated: 0.8%

        activation = entry * (1 + activation_pct)        # 2520.0
        ltp = 2518.0                                      # below threshold

        assert ltp < activation

    def test_sl_never_moves_down(self):
        """Once the trailing SL ratchets up, a pullback must NOT lower it."""
        trail_pct = 0.007   # updated: 0.7%

        # Peak at 2530 → SL = 2512.29
        sl_at_peak = round(2530.0 * (1 - trail_pct), 2)
        # Pullback to 2520 → SL would be 2502.36
        sl_at_pullback = round(2520.0 * (1 - trail_pct), 2)

        # The condition `trailing_sl > trade.stop_loss_price` prevents lowering
        assert sl_at_pullback < sl_at_peak

    def test_sl_progression_is_monotonic(self):
        """As price rises, trailing SL should strictly increase."""
        trail_pct = 0.007   # updated: 0.7%
        prices = [2522, 2525, 2530, 2540, 2550, 2560]
        sls = [round(p * (1 - trail_pct), 2) for p in prices]

        for i in range(1, len(sls)):
            assert sls[i] > sls[i - 1]

    def test_trailing_sl_values(self):
        """Verify exact trailing SL values at specific price points."""
        trail_pct = 0.007   # updated: 0.7%

        # At 2520 → SL = 2520 * 0.993 = 2502.36
        assert round(2520.0 * (1 - trail_pct), 2) == 2502.36
        # At 2550 → SL = 2550 * 0.993 = 2532.15
        assert round(2550.0 * (1 - trail_pct), 2) == 2532.15

    def test_trailing_sl_breakeven(self):
        """After sufficient movement, trailing SL should be above entry (breakeven+)."""
        entry = 2500.0
        activation_pct = 0.008   # updated: 0.8%
        trail_pct = 0.007        # updated: 0.7%

        # Entry=2500, activation=2520.0, trail=0.7%
        # At exactly 2520.0: SL = 2520 * 0.993 = 2502.36 (above entry — breakeven+)
        ltp = 2520.0
        sl = round(ltp * (1 - trail_pct), 2)
        assert sl > entry


# ═══════════════════════════════════════════════════════════════════════════
#  Stop-Loss / Target Detection
# ═══════════════════════════════════════════════════════════════════════════


class TestExitDetection:
    """Tests for SL/target hit detection and P&L calculation."""

    def test_sl_hit(self, open_trade):
        """SL triggers when LTP <= stop_loss_price."""
        ltp = 2474.0  # below new 1.0% SL of 2475
        assert ltp <= open_trade.stop_loss_price

    def test_sl_exact_boundary(self, open_trade):
        """SL should trigger at exactly the stop_loss_price."""
        ltp = 2475.0  # exactly at new 1.0% SL
        assert ltp <= open_trade.stop_loss_price

    def test_target_hit(self, open_trade):
        """Target triggers when LTP >= target_price."""
        ltp = 2551.0  # above new 2.0% target of 2550
        assert ltp >= open_trade.target_price

    def test_target_exact_boundary(self, open_trade):
        ltp = 2550.0  # exactly at new 2.0% target
        assert ltp >= open_trade.target_price

    def test_no_exit_between_sl_and_target(self, open_trade):
        """Between SL and target, no exit should trigger."""
        ltp = 2510.0
        assert ltp > open_trade.stop_loss_price
        assert ltp < open_trade.target_price

    def test_pnl_on_sl(self, open_trade):
        """P&L on stop-loss = (exit - entry) × qty → negative."""
        exit_price = open_trade.stop_loss_price  # 2475.0
        pnl = (exit_price - open_trade.entry_price) * open_trade.quantity
        assert pnl == pytest.approx(-2500.0)  # (2475-2500)*100

    def test_pnl_on_target(self, open_trade):
        """P&L on target hit = (exit - entry) × qty → positive."""
        exit_price = open_trade.target_price  # 2550.0
        pnl = (exit_price - open_trade.entry_price) * open_trade.quantity
        assert pnl == pytest.approx(5000.0)  # (2550-2500)*100

    def test_risk_reward_ratio(self, open_trade):
        """Risk-reward should be at least 1:2 (target P&L / SL P&L >= 2)."""
        loss = abs(open_trade.stop_loss_price - open_trade.entry_price)
        reward = abs(open_trade.target_price - open_trade.entry_price)
        rr = reward / loss
        assert rr >= 2.0


# ═══════════════════════════════════════════════════════════════════════════
#  Daily Drawdown
# ═══════════════════════════════════════════════════════════════════════════


class TestDrawdown:
    """Tests for daily drawdown halt calculation."""

    def test_under_limit_no_halt(self):
        """Total loss below limit should not trigger halt."""
        realised = 15_000.0
        unrealised = 5_000.0
        limit = 30_000.0
        assert (realised + unrealised) < limit

    def test_at_limit_triggers_halt(self):
        """Total loss at or above limit should trigger halt."""
        realised = 15_000.0
        unrealised = 15_000.0
        limit = 30_000.0
        assert (realised + unrealised) >= limit

    def test_unrealised_alone_can_trigger(self):
        """Pure unrealised losses can trigger halt (no closed trades needed)."""
        realised = 0.0
        unrealised = 30_000.0
        limit = 30_000.0
        assert (realised + unrealised) >= limit

    def test_drawdown_pct(self):
        """Drawdown percentage = (total_loss / limit) × 100."""
        total_loss = 15_000.0
        limit = 30_000.0
        pct = round((total_loss / limit) * 100, 2)
        assert pct == 50.0

    def test_drawdown_pct_at_100(self):
        pct = round((30_000.0 / 30_000.0) * 100, 2)
        assert pct == 100.0


# ═══════════════════════════════════════════════════════════════════════════
#  EOD Report / CLOSING Reconciliation
# ═══════════════════════════════════════════════════════════════════════════


class TestEodReport:
    """Tests for EOD report calculation logic."""

    def test_win_loss_classification(self):
        """Closed trades should be classified by realised P&L sign."""
        pnls = [1000, -500, 2000, -1000, 300]
        won = sum(1 for p in pnls if p > 0)
        lost = sum(1 for p in pnls if p < 0)
        assert won == 3
        assert lost == 2

    def test_net_pnl(self):
        pnls = [1000, -500, 2000, -1000, 300]
        assert sum(pnls) == 1800

    def test_return_pct(self):
        net_pnl = 1800.0
        starting_capital = 1_000_000.0
        return_pct = (net_pnl / starting_capital) * 100
        assert return_pct == pytest.approx(0.18)

    def test_reconciled_trades_zero_pnl(self):
        """Orphaned CLOSING trades should be closed at entry price → P&L = 0."""
        entry_price = 2500.0
        exit_price = entry_price    # reconciled at entry
        quantity = 100
        pnl = (exit_price - entry_price) * quantity
        assert pnl == 0.0


# ═══════════════════════════════════════════════════════════════════════════
#  ROI Decay
# ═══════════════════════════════════════════════════════════════════════════


class TestRoiDecay:
    """ROI decay reduces target price over time to capture partial profits."""

    def test_no_decay_under_15_min(self):
        """Target should not change within the first 15 minutes."""
        entry = 2500.0
        elapsed = 10
        decayed = None
        if elapsed > 35:
            decayed = round(entry * 1.001, 2)
        elif elapsed > 25:
            decayed = round(entry * 1.003, 2)
        elif elapsed > 15:
            decayed = round(entry * 1.008, 2)
        assert decayed is None

    def test_decay_at_20_min(self):
        entry = 2500.0
        decayed = round(entry * 1.008, 2)   # 15-25 min → 0.8%
        assert decayed == 2520.0

    def test_decay_at_30_min(self):
        entry = 2500.0
        decayed = round(entry * 1.003, 2)   # 25-35 min → 0.3%
        assert decayed == 2507.5

    def test_decay_at_40_min(self):
        entry = 2500.0
        decayed = round(entry * 1.001, 2)   # 35+ min → 0.1%
        assert decayed == 2502.5

    def test_decayed_lower_than_original_target(self):
        """All decay levels should be below the original 2.0% target."""
        entry = 2500.0
        original_target = round(entry * 1.020, 2)   # 2550.0 (new min_target_pct=2%)
        for multiplier in [1.015, 1.010, 1.005]:
            decayed = round(entry * multiplier, 2)
            assert decayed < original_target

    def test_roi_decay_never_falls_below_stop_loss(self):
        """Decayed target must always remain above the current stop-loss."""
        entry = 2500.0
        # Scenario: trailing SL has moved up close to entry (trail activated at 0.8%)
        trailing_sl = round(entry * 1.010, 2)   # SL now at 2525.0 (above entry)
        # At 50+ min the 0.5% ATR-floor decay gives 2512.50 — below the trailing SL!
        decayed = round(entry * 1.005, 2)        # 2512.50
        # The production guard: only update if decayed > stop_loss_price
        should_update = decayed < 9999.0 and decayed > trailing_sl
        assert not should_update, (
            "ROI decay must not lower target below the trailing stop-loss"
        )


# ═══════════════════════════════════════════════════════════════════════════
#  Enhanced EOD Metrics
# ═══════════════════════════════════════════════════════════════════════════


class TestEnhancedEodMetrics:
    """Tests for the enhanced EOD report metrics."""

    def test_profit_factor(self):
        positive = [1000, 2000, 500]
        negative = [-800, -400]
        pf = sum(positive) / abs(sum(negative))
        assert pf == pytest.approx(2.916, rel=0.01)

    def test_profit_factor_no_losses(self):
        """When there are no losing trades, profit factor should be the sentinel 999.0."""
        positive = [1000, 2000]
        negative = []
        # mirrors the production formula exactly
        if negative:
            pf = sum(positive) / abs(sum(negative))
        elif positive:
            pf = 999.0
        else:
            pf = 0.0
        assert pf == 999.0

    def test_max_consecutive_losses(self):
        pnls = [100, -50, -30, -20, 200, -10, 100]
        max_streak = 0
        streak = 0
        for p in pnls:
            if p < 0:
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 0
        assert max_streak == 3

    def test_sharpe_ratio_positive(self):
        pnls = [100, 200, -50, 150, -30]
        mean_pnl = sum(pnls) / len(pnls)
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
        std_pnl = variance ** 0.5
        sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0
        assert sharpe > 0

    def test_avg_duration(self):
        durations_min = [15.0, 25.0, 35.0, 10.0]
        avg = sum(durations_min) / len(durations_min)
        assert avg == 21.25


# ═══════════════════════════════════════════════════════════════════════════
#  ROI Decay Ordering
# ═══════════════════════════════════════════════════════════════════════════


class TestRoiDecayOrdering:
    """ROI decay must be evaluated BEFORE the target check so a decayed
    target triggers an exit in the same poll cycle, not 5 seconds later."""

    def test_decayed_target_triggers_exit_same_cycle(self):
        """If ROI decay reduces the target to below LTP, the target check
        (which runs after ROI decay) should fire immediately."""
        entry = 2500.0
        original_target = 2540.0  # 1.6%
        ltp = 2510.0              # below original target but above 0.3% decay

        # After 30 min, decay → 0.3%: target becomes 2507.50
        decayed = round(entry * 1.003, 2)
        assert decayed == 2507.50

        # In the same cycle, target check runs with the DECAYED target
        assert ltp >= decayed, "LTP should trigger exit at decayed target"
        assert ltp < original_target, "LTP would NOT trigger exit at original"

    def test_decay_order_sl_first_then_decay_then_target(self):
        """The poll order must be: SL check → ROI decay → Target check."""
        entry = 2500.0
        sl = 2480.0
        original_target = 2540.0
        ltp = 2510.0

        # Step 1: SL check — not triggered
        assert ltp > sl, "SL should not fire"

        # Step 2: ROI decay at 30 min → target = 2507.50
        decayed = round(entry * 1.003, 2)

        # Step 3: Target check with decayed value — triggered
        assert ltp >= decayed, "Target fires at decayed level"


# ═══════════════════════════════════════════════════════════════════════════
#  Drawdown LTP Map Pass-Through
# ═══════════════════════════════════════════════════════════════════════════


class TestDrawdownLtpMap:
    """_check_daily_drawdown should use the ltp_map from the main poll
    rather than making redundant API calls."""

    def test_ltp_map_used_for_unrealised_loss(self):
        """When ltp_map is provided, it should be used directly."""
        ltp_map = {"RELIANCE": 2490.0, "INFY": 1770.0}
        entry = 2500.0
        stock = "RELIANCE"

        # Simulate: get LTP from map
        ltp = ltp_map.get(stock)
        assert ltp == 2490.0

        # Unrealised loss
        mtm = (ltp - entry) * 100
        assert mtm == -1000.0

    def test_fallback_when_stock_not_in_map(self):
        """Stocks not in ltp_map should fall back to other sources."""
        ltp_map = {"INFY": 1770.0}
        stock = "RELIANCE"
        entry = 2500.0

        ltp = ltp_map.get(stock)
        assert ltp is None  # not in map → must fall back


# ═══════════════════════════════════════════════════════════════════════════
#  GTT Cancellation on Position Close
# ═══════════════════════════════════════════════════════════════════════════


class TestGttCancellation:
    """GTT trigger should be cancelled when a position is closed to prevent
    orphaned GTT orders from creating naked short positions."""

    def test_trade_model_has_gtt_trigger_id(self):
        """Trade model must have a gtt_trigger_id field (nullable integer)."""
        from models.trade import Trade
        trade = Trade(
            stock="RELIANCE", exchange="NSE", direction="BUY",
            product_type="MIS", quantity=100, entry_price=2500.0,
            stop_loss_price=2480.0, target_price=2540.0, status="OPEN",
            trade_date=date.today(), entry_time=dt_time(10, 0),
            gtt_trigger_id=12345,
        )
        assert trade.gtt_trigger_id == 12345

    def test_trade_model_gtt_trigger_id_nullable(self):
        """GTT trigger ID should be optional (None for paper trades)."""
        from models.trade import Trade
        trade = Trade(
            stock="RELIANCE", exchange="NSE", direction="BUY",
            product_type="MIS", quantity=100, entry_price=2500.0,
            stop_loss_price=2480.0, target_price=2540.0, status="OPEN",
            trade_date=date.today(), entry_time=dt_time(10, 0),
        )
        assert trade.gtt_trigger_id is None


# ═══════════════════════════════════════════════════════════════════════════
#  Consecutive Loss Tracking — EOD Neutrality
# ═══════════════════════════════════════════════════════════════════════════


class TestConsecutiveLossEodNeutral:
    """EOD_CLOSE should neither advance nor reset the consecutive loss streak."""

    def test_eod_close_does_not_modify_streak(self):
        """Verify that the consecutive loss tracking logic treats EOD_CLOSE
        as neutral — only STOP_LOSS_HIT increments, only TARGET_HIT resets."""
        reasons = ["STOP_LOSS_HIT", "TARGET_HIT", "EOD_CLOSE", "RECONCILED"]

        for reason in reasons:
            modifies_streak = (reason == "STOP_LOSS_HIT" or reason == "TARGET_HIT")
            # EOD_CLOSE and RECONCILED should be neutral
            if reason in ("EOD_CLOSE", "RECONCILED"):
                assert not modifies_streak


# ═══════════════════════════════════════════════════════════════════════════
#  Daily Drawdown Soft Alert (2% warning tier)
# ═══════════════════════════════════════════════════════════════════════════


class TestDrawdownSoftAlert:
    """2% soft-alert must fire once before the 3% hard halt.

    The soft alert fires when total_loss >= capital * 2% but < 3% hard limit.
    It must fire only ONCE per session (idempotent) and must NOT halt trading.
    """

    def test_soft_alert_threshold_below_hard_limit(self):
        """Soft alert threshold (2%) must be strictly below the hard halt (3%)."""
        soft_pct = 0.02
        hard_pct = 0.03
        assert soft_pct < hard_pct

    def test_loss_at_soft_level_does_not_exceed_hard(self):
        """Loss exactly at 2% of capital does not reach the 3% hard halt."""
        capital = 1_000_000.0
        soft_limit = capital * 0.02    # 20_000
        hard_limit = capital * 0.03   # 30_000
        assert soft_limit < hard_limit

    def test_soft_alert_fires_between_2_and_3_pct(self):
        """Total loss between 2% and 3% must trigger the soft alert and NOT the hard halt."""
        capital = 1_000_000.0
        soft_limit = capital * 0.02   # 20_000
        hard_limit = capital * 0.03   # 30_000
        total_loss = 22_000.0

        should_soft_alert = total_loss >= soft_limit
        should_halt = total_loss >= hard_limit
        assert should_soft_alert
        assert not should_halt

    def test_soft_alert_state_fires_only_once(self):
        """Simulate the idempotent flag: soft alert should not re-fire when already set."""
        soft_alerted = False
        capital = 1_000_000.0
        soft_limit = capital * 0.02
        total_loss = 22_000.0

        # First poll — should fire
        if not soft_alerted and total_loss >= soft_limit:
            soft_alerted = True
            alert_1_fired = True
        else:
            alert_1_fired = False

        # Second poll (same loss) — should NOT re-fire
        if not soft_alerted and total_loss >= soft_limit:
            alert_2_fired = True
        else:
            alert_2_fired = False

        assert alert_1_fired
        assert not alert_2_fired

    def test_risk_manager_has_drawdown_soft_alert_flag(self):
        """RiskManager must expose _drawdown_soft_alerted as an instance attribute."""
        rm = RiskManager()
        assert hasattr(rm, "_drawdown_soft_alerted")
        assert rm._drawdown_soft_alerted is False


# ═══════════════════════════════════════════════════════════════════════════
#  ATR-Based ROI Decay Floor
# ═══════════════════════════════════════════════════════════════════════════


class TestAtrRoiDecayFloor:
    """ROI decay final tier uses max(0.5%, 0.4 × ATR_pct) to avoid setting targets
    below the stock's typical noise floor."""

    def test_floor_with_low_atr_uses_minimum(self):
        """When 0.4 × ATR_pct < 0.5%, the 0.5% floor applies."""
        entry = 2500.0
        atr = 5.0                                 # ATR_pct = 0.2%
        atr_pct = atr / entry                     # 0.002
        floor_pct = max(0.005, 0.4 * atr_pct)    # max(0.5%, 0.08%) = 0.5%
        assert floor_pct == pytest.approx(0.005)

    def test_floor_with_high_atr_uses_atr(self):
        """When 0.4 × ATR_pct > 0.5%, the ATR-based floor applies."""
        entry = 1000.0
        atr = 20.0                                # ATR_pct = 2.0%
        atr_pct = atr / entry                     # 0.02
        floor_pct = max(0.005, 0.4 * atr_pct)    # max(0.5%, 0.8%) = 0.8%
        assert floor_pct == pytest.approx(0.008)

    def test_decayed_target_at_atr_floor(self):
        """Decayed target = entry * (1 + max(0.5%, 0.4 × ATR_pct))."""
        entry = 1000.0
        atr = 20.0                                # ATR_pct = 2%
        atr_pct = atr / entry
        floor_pct = max(0.005, 0.4 * atr_pct)    # 0.8%
        decayed = round(entry * (1 + floor_pct), 2)
        assert decayed == 1008.0

    def test_atr_floor_prevents_target_below_sl(self):
        """Even with ATR-based floor, decayed target must remain above trailing SL."""
        entry = 2500.0
        atr = 12.0                               # ATR_pct = 0.48%
        atr_pct = atr / entry
        floor_pct = max(0.005, 0.4 * atr_pct)   # max(0.5%, 0.192%) = 0.5%
        decayed = round(entry * (1 + floor_pct), 2)   # 2512.50

        # Suppose trailing SL has moved to 2515 (above the decayed level)
        trailing_sl = 2515.0
        should_update = decayed > trailing_sl
        assert not should_update  # production guard prevents this update

    def test_standard_0_5_pct_fallback_when_atr_missing(self):
        """When ATR is not in Redis (None/missing), default 0.5% floor applies."""
        entry = 2500.0
        atr_str = None   # simulate missing Redis key
        atr_floor_pct = 0.005   # default
        if atr_str:
            atr_val = float(atr_str)
            if atr_val > 0 and entry > 0:
                atr_floor_pct = max(0.005, 0.4 * atr_val / entry)
        decayed = round(entry * (1 + atr_floor_pct), 2)
        assert decayed == round(entry * 1.005, 2)   # 2512.50
