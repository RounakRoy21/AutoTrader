"""
Regression tests for the broker-client contract (audit Part A R2/R5/R10/R12,
Part B C2/C3/C6):

  * get_ltp never returns a 0.0 sentinel; RiskManager never exits on a
    missing / 0.0 / implausible LTP and alerts after N consecutive misses.
  * Order writes are sent exactly once with an order_reference_id; ambiguous
    failures are resolved by a reference lookup, never by resubmitting.
  * Every price sent to the broker is snapped to the tick size.
  * The broker stop is an SL-limit with the limit buffered below the trigger.

All broker / DB / Redis interactions are mocked.
"""

from datetime import datetime, time as dt_time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core import tick_size
from core.tick_size import ceil_to_tick, floor_to_tick, round_to_tick, sl_limit_price


# ═══════════════════════════════════════════════════════════════════════════
#  Tick-size snapping (R10 / C6)
# ═══════════════════════════════════════════════════════════════════════════


class TestTickSnapping:
    def test_floor_misaligned_sl(self):
        assert floor_to_tick(1423.37) == 1423.35
        assert floor_to_tick(1423.34) == 1423.30

    def test_ceil_misaligned_target(self):
        assert ceil_to_tick(1423.37) == 1423.40
        assert ceil_to_tick(1423.31) == 1423.35

    def test_aligned_values_unchanged(self):
        assert floor_to_tick(1423.35) == 1423.35
        assert ceil_to_tick(1423.35) == 1423.35
        assert floor_to_tick(100.0) == 100.0

    def test_float_noise_does_not_drop_a_tick(self):
        # 0.1 + 0.2 style noise: 100.05 computed as 100.04999999999
        assert floor_to_tick(100.04999999999) == 100.05
        assert ceil_to_tick(100.05000000001) == 100.05

    def test_round_to_nearest(self):
        assert round_to_tick(1423.37) == 1423.35
        assert round_to_tick(1423.38) == 1423.40

    def test_custom_tick(self):
        assert floor_to_tick(2512.37, 0.10) == 2512.30
        assert ceil_to_tick(2512.31, 0.10) == 2512.40
        assert floor_to_tick(12.347, 0.01) == 12.34

    def test_invalid_tick_falls_back_to_default(self):
        # A tick expressed in paise (5) or garbage must not snap to ₹5 steps.
        assert floor_to_tick(1423.37, 5) == 1423.35
        assert floor_to_tick(1423.37, None) == 1423.35
        assert floor_to_tick(1423.37, "x") == 1423.35

    def test_sl_limit_buffered_below_trigger(self):
        limit = sl_limit_price(1000.0, 0.05, 0.005)
        assert limit == 995.0
        assert limit < 1000.0

    def test_sl_limit_at_least_one_tick_below(self):
        limit = sl_limit_price(10.0, 0.05, 0.0)
        assert limit == 9.95

    def test_sl_limit_is_tick_aligned(self):
        limit = sl_limit_price(1423.37, 0.05, 0.005)
        # trigger floors to 1423.35; 1423.35 * 0.995 = 1416.23… → 1416.20
        assert limit == 1416.20

    def test_get_tick_size_uses_instrument_service(self):
        from integrations import instrument_service

        instrument_service._set_tick_sizes({"MRF": 0.5, "BAD": 50.0})
        try:
            assert tick_size.get_tick_size("MRF") == 0.5
            assert tick_size.get_tick_size("BAD") == tick_size.DEFAULT_TICK_SIZE
            assert tick_size.get_tick_size("UNKNOWN") == tick_size.DEFAULT_TICK_SIZE
            assert tick_size.get_tick_size(None) == tick_size.DEFAULT_TICK_SIZE
        finally:
            instrument_service._set_tick_sizes({})


# ═══════════════════════════════════════════════════════════════════════════
#  GrowwClient helpers
# ═══════════════════════════════════════════════════════════════════════════


class _Exc(Exception):
    def __init__(self, msg="err", code=None):
        super().__init__(msg)
        if code is not None:
            self.code = code


def _sdk():
    """A fake Groww SDK that enforces the REAL ``growwapi.GrowwAPI`` surface:
    a wrong method name or keyword argument fails the test (autospec)."""
    from unittest.mock import create_autospec

    from growwapi import GrowwAPI

    return create_autospec(GrowwAPI, instance=True)


def _live_client(fake_groww):
    from integrations.groww_client import GrowwClient

    client = GrowwClient.__new__(GrowwClient)
    client._settings = SimpleNamespace(paper_trading=False, groww_client_id="x")
    client._groww = fake_groww
    client._last_failure = 0.0
    client._cached_token = "tok"
    client._zero_volume_warned_symbols = set()
    client.get_groww = AsyncMock(return_value=fake_groww)
    client._handle_failure = AsyncMock()
    return client


# ═══════════════════════════════════════════════════════════════════════════
#  get_ltp contract (R2 / C2)
# ═══════════════════════════════════════════════════════════════════════════


class TestGetLtpContract:
    @pytest.mark.asyncio
    async def test_missing_zero_and_null_prices_are_omitted(self):
        fake = _sdk()
        fake.get_ltp.return_value = {"NSE_RELIANCE": 2500.5, "NSE_INFY": 0, "NSE_TCS": None}
        client = _live_client(fake)
        with patch("integrations.groww_client.time.sleep", return_value=None):
            res = await client.get_ltp(["NSE:RELIANCE", "NSE:INFY", "NSE:TCS", "NSE:SBIN"])
        assert res == {"NSE:RELIANCE": {"last_price": 2500.5}}

    @pytest.mark.asyncio
    async def test_fetch_failure_raises_instead_of_zero(self):
        fake = _sdk()
        fake.get_ltp.side_effect = _Exc("429 rate limited")
        client = _live_client(fake)
        with patch("integrations.groww_client.time.sleep", return_value=None):
            with pytest.raises(Exception):
                await client.get_ltp(["NSE:RELIANCE"])

    @pytest.mark.asyncio
    async def test_paper_get_quote_has_no_zero_sentinel(self):
        from integrations.groww_client import GrowwClient

        client = GrowwClient.__new__(GrowwClient)
        client._settings = SimpleNamespace(paper_trading=True)
        res = await client.get_quote(["NSE:RELIANCE"])
        assert res["NSE:RELIANCE"]["last_price"] is None
        assert 0.0 not in res["NSE:RELIANCE"].values()


# ═══════════════════════════════════════════════════════════════════════════
#  Order writes (R5 / C3, R10, R12)
# ═══════════════════════════════════════════════════════════════════════════


def _ref_ok(ref):
    return isinstance(ref, str) and 8 <= len(ref) <= 20 and ref.isalnum()


class TestOrderWrites:
    @pytest.mark.asyncio
    async def test_place_order_sent_once_with_reference_and_sdk_params(self):
        fake = _sdk()
        fake.place_order.return_value = {"groww_order_id": "GMK123", "order_status": "OPEN"}
        client = _live_client(fake)

        oid = await client.place_order("RELIANCE", transaction_type="BUY", quantity=5)

        assert oid == "GMK123"
        assert fake.place_order.call_count == 1
        kw = fake.place_order.call_args.kwargs
        assert _ref_ok(kw["order_reference_id"])
        assert kw["validity"] == "DAY"
        assert kw["segment"] == "CASH"
        assert kw["product"] == "MIS"
        assert "source" not in kw
        assert kw["timeout"]

    @pytest.mark.asyncio
    async def test_reference_ids_are_unique(self):
        from integrations.groww_client import new_order_reference_id

        refs = {new_order_reference_id() for _ in range(200)}
        assert len(refs) == 200
        assert all(_ref_ok(r) for r in refs)

    @pytest.mark.asyncio
    async def test_ambiguous_failure_found_by_reference_is_not_resubmitted(self):
        fake = _sdk()
        fake.place_order.side_effect = _Exc("Read timed out", code="504")
        fake.get_order_status_by_reference.return_value = {
            "groww_order_id": "GMK999", "order_status": "EXECUTED",
        }
        client = _live_client(fake)

        with patch("integrations.groww_client.ORDER_LOOKUP_DELAY_S", 0):
            oid = await client.place_order("RELIANCE", transaction_type="SELL", quantity=5)

        assert oid == "GMK999"
        assert fake.place_order.call_count == 1  # never resubmitted
        sent_ref = fake.place_order.call_args.kwargs["order_reference_id"]
        lookup_ref = fake.get_order_status_by_reference.call_args.kwargs["order_reference_id"]
        assert sent_ref == lookup_ref

    @pytest.mark.asyncio
    async def test_connection_error_without_code_is_ambiguous(self):
        fake = _sdk()
        fake.place_order.side_effect = ConnectionResetError("reset by peer")
        fake.get_order_status_by_reference.return_value = {"groww_order_id": "GMK1"}
        client = _live_client(fake)
        with patch("integrations.groww_client.ORDER_LOOKUP_DELAY_S", 0):
            assert await client.place_order("RELIANCE") == "GMK1"
        assert fake.place_order.call_count == 1

    @pytest.mark.asyncio
    async def test_definitive_rejection_raises_without_lookup_or_retry(self):
        fake = _sdk()
        fake.place_order.side_effect = _Exc("Bad request", code="400")
        client = _live_client(fake)

        with pytest.raises(Exception):
            await client.place_order("RELIANCE")
        assert fake.place_order.call_count == 1
        fake.get_order_status_by_reference.assert_not_called()

    @pytest.mark.asyncio
    async def test_ambiguous_failure_not_found_reraises_original(self):
        fake = _sdk()
        fake.place_order.side_effect = _Exc("timeout", code="504")
        fake.get_order_status_by_reference.side_effect = _Exc("not found", code="404")
        client = _live_client(fake)

        with patch("integrations.groww_client.ORDER_LOOKUP_DELAY_S", 0):
            with pytest.raises(_Exc) as ei:
                await client.place_order("RELIANCE")
        assert ei.value.code == "504"
        assert fake.place_order.call_count == 1

    @pytest.mark.asyncio
    async def test_ambiguous_failure_undetermined_raises_state_unknown(self):
        from integrations.groww_client import ORDER_LOOKUP_ATTEMPTS, OrderStateUnknownError

        fake = _sdk()
        fake.place_order.side_effect = _Exc("timeout", code="504")
        fake.get_order_status_by_reference.side_effect = _Exc("timeout", code="504")
        client = _live_client(fake)

        with patch("integrations.groww_client.ORDER_LOOKUP_DELAY_S", 0):
            with pytest.raises(OrderStateUnknownError) as ei:
                await client.place_order("RELIANCE")
        assert _ref_ok(ei.value.order_reference_id)
        assert fake.place_order.call_count == 1
        assert fake.get_order_status_by_reference.call_count == ORDER_LOOKUP_ATTEMPTS

    @pytest.mark.asyncio
    async def test_place_order_snaps_limit_prices(self):
        fake = _sdk()
        fake.place_order.return_value = {"groww_order_id": "1"}
        client = _live_client(fake)

        await client.place_order("RELIANCE", transaction_type="SELL", order_type="LIMIT", price=1423.37)
        assert fake.place_order.call_args.kwargs["price"] == 1423.35

        await client.place_order("RELIANCE", transaction_type="BUY", order_type="LIMIT", price=1423.37)
        assert fake.place_order.call_args.kwargs["price"] == 1423.40

    @pytest.mark.asyncio
    async def test_stop_is_buffered_tick_aligned_sl_limit_sent_once(self):
        fake = _sdk()
        fake.place_order.return_value = {"groww_order_id": "GMK12345678"}
        client = _live_client(fake)

        sl_id = await client.place_stop_loss_order("RELIANCE", 7, 1423.37)

        assert sl_id == "GMK12345678"
        assert fake.place_order.call_count == 1
        kw = fake.place_order.call_args.kwargs
        assert kw["order_type"] == "SL"
        assert kw["trigger_price"] == 1423.35          # floored to tick
        assert kw["price"] == 1416.20                  # 0.5% buffer, floored
        assert kw["price"] < kw["trigger_price"]
        assert kw["quantity"] == 7
        assert kw["transaction_type"] == "SELL"
        assert kw["product"] == "MIS"
        assert _ref_ok(kw["order_reference_id"])

    @pytest.mark.asyncio
    async def test_stop_placement_failure_is_not_retried(self):
        fake = _sdk()
        fake.place_order.side_effect = _Exc("rejected", code="GA001")
        client = _live_client(fake)
        with pytest.raises(Exception):
            await client.place_stop_loss_order("RELIANCE", 1, 1000.0)
        assert fake.place_order.call_count == 1

    @pytest.mark.asyncio
    async def test_cancel_is_sent_once_with_sdk_params(self):
        fake = _sdk()
        fake.cancel_order.side_effect = _Exc("timeout", code="504")
        client = _live_client(fake)

        with pytest.raises(Exception):
            await client.cancel_order("GMK1")
        assert fake.cancel_order.call_count == 1
        kw = fake.cancel_order.call_args.kwargs
        assert kw["groww_order_id"] == "GMK1"
        assert kw["segment"] == "CASH"

    def test_retry_sync_does_not_sleep_after_final_attempt(self):
        from integrations.groww_client import MAX_RETRIES, _retry_sync

        @_retry_sync
        def _flaky():
            raise Exception("Connection reset by peer")

        with patch("integrations.groww_client.time.sleep") as sleep:
            with pytest.raises(Exception):
                _flaky()
        assert sleep.call_count == MAX_RETRIES - 1


# ═══════════════════════════════════════════════════════════════════════════
#  RiskManager never exits on a bad LTP (R2 / C2)
# ═══════════════════════════════════════════════════════════════════════════


class _FixedDT(datetime):
    @classmethod
    def now(cls, tz=None):
        return tz.localize(datetime(2026, 11, 13, 11, 0, 0))


def _trade(stock="RELIANCE"):
    return SimpleNamespace(
        id=1, stock=stock, exchange="NSE", entry_price=2500.0, stop_loss_price=2475.0,
        target_price=2550.0, quantity=10, product_type="MIS", partial_booked=False,
        partial_target_price=None, entry_time=dt_time(10, 30),
        trade_date=_FixedDT(2026, 11, 13).date(), booked_pnl=0.0,
    )


def _live_rm(**extra):
    from agents.risk_manager import RiskManager

    rm = RiskManager()
    settings = dict(
        paper_trading=False, paper_risk_use_broker_ltp=True,
        partial_booking_enabled=False, roi_decay_enabled=False,
        trailing_sl_activation_pct=0.008, trailing_sl_trail_pct=0.007,
        risk_ltp_sanity_band_pct=0.05, risk_ltp_miss_alert_threshold=3,
    )
    settings.update(extra)
    rm._settings = SimpleNamespace(**settings)
    rm._close_position = AsyncMock()
    rm._check_daily_drawdown = AsyncMock()
    rm._generate_eod_report = AsyncMock()
    return rm


async def _run_poll(rm, trades, groww, ws_tick=None):
    with patch("agents.risk_manager.datetime", _FixedDT), \
         patch.object(rm, "_get_open_trades", new=AsyncMock(return_value=trades)), \
         patch("agents.risk_manager.get_groww_client", return_value=groww), \
         patch("agents.risk_manager.ltp_store.get_ltp", return_value=ws_tick), \
         patch("agents.risk_manager.send_stop_loss_alert", new=AsyncMock()), \
         patch("agents.risk_manager.send_target_hit_alert", new=AsyncMock()), \
         patch("agents.risk_manager.publish", new=AsyncMock()):
        await rm._poll()


class TestRiskManagerBadLtp:
    @pytest.mark.asyncio
    async def test_zero_ltp_does_not_exit(self):
        rm = _live_rm()
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 0.0}}))
        await _run_poll(rm, [_trade()], groww)
        rm._close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_symbol_does_not_exit(self):
        rm = _live_rm()
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={}))
        await _run_poll(rm, [_trade()], groww)
        rm._close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_ltp_failure_does_not_exit(self):
        rm = _live_rm()
        groww = SimpleNamespace(get_ltp=AsyncMock(side_effect=RuntimeError("429")))
        await _run_poll(rm, [_trade()], groww)
        rm._close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_negative_or_garbage_ltp_does_not_exit(self):
        rm = _live_rm()
        for bad in (-5.0, None, "nan", float("nan")):
            groww = SimpleNamespace(get_ltp=AsyncMock(return_value={"NSE:RELIANCE": {"last_price": bad}}))
            await _run_poll(rm, [_trade()], groww)
        rm._close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_implausible_ltp_vs_ws_tick_does_not_exit(self):
        rm = _live_rm()
        # Broker says 2300 (below SL) but the WS tick says 2510 → >5% apart.
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2300.0}}))
        await _run_poll(rm, [_trade()], groww, ws_tick=2510.0)
        rm._close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_ltp_below_stop_still_exits(self):
        rm = _live_rm()
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2470.0}}))
        await _run_poll(rm, [_trade()], groww, ws_tick=2471.0)
        rm._close_position.assert_awaited_once()
        assert rm._close_position.call_args.args[1] == 2470.0
        assert rm._close_position.call_args.args[2] == "STOP_LOSS_HIT"

    @pytest.mark.asyncio
    async def test_alerts_once_after_n_consecutive_misses_and_resets(self):
        rm = _live_rm(risk_ltp_miss_alert_threshold=3)
        bad = SimpleNamespace(get_ltp=AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 0.0}}))
        good = SimpleNamespace(get_ltp=AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2501.0}}))
        with patch("agents.risk_manager.send_telegram", new=AsyncMock()) as tg:
            await _run_poll(rm, [_trade()], bad)
            await _run_poll(rm, [_trade()], bad)
            assert tg.await_count == 0
            await _run_poll(rm, [_trade()], bad)
            assert tg.await_count == 1
            await _run_poll(rm, [_trade()], bad)
            assert tg.await_count == 1           # not re-sent every poll
            await _run_poll(rm, [_trade()], good)
            assert rm._ltp_miss_counts.get("RELIANCE") is None
        rm._close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_paper_zero_broker_ltp_falls_back_to_tick_store(self):
        from agents.risk_manager import RiskManager

        rm = RiskManager()
        rm._settings = SimpleNamespace(
            paper_trading=True, paper_risk_use_broker_ltp=True, risk_ltp_sanity_band_pct=0.05,
        )
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 0.0}}))
        with patch("agents.risk_manager.get_groww_client", return_value=groww):
            with patch("agents.risk_manager.ltp_store.get_ltp", return_value=2504.0):
                ltp_map = await rm._build_ltp_map([_trade()])
        assert ltp_map == {"RELIANCE": 2504.0}

    @pytest.mark.asyncio
    async def test_get_trade_ltp_rejects_zero(self):
        rm = _live_rm()
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 0.0}}))
        with patch("agents.risk_manager.get_groww_client", return_value=groww):
            with patch("agents.risk_manager.ltp_store.get_ltp", return_value=None):
                assert await rm._get_trade_ltp(_trade()) is None

    @pytest.mark.asyncio
    async def test_trailing_sl_is_tick_aligned(self):
        rm = _live_rm()
        rm._update_stop_loss = AsyncMock()
        # 2537.13 * (1 - 0.007) = 2519.37… → floored to 2519.35
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2537.13}}))
        await _run_poll(rm, [_trade()], groww, ws_tick=2537.0)
        new_sl = rm._update_stop_loss.call_args.args[1]
        assert new_sl == 2519.35


# ═══════════════════════════════════════════════════════════════════════════
#  TradingAgent: an unconfirmed BUY halts and is never resubmitted (R5)
# ═══════════════════════════════════════════════════════════════════════════


class TestTradingAgentUnknownBuy:
    @pytest.mark.asyncio
    async def test_unknown_buy_state_halts_and_does_not_retry(self):
        from agents.trading_agent import TradingAgent
        from integrations.groww_client import OrderStateUnknownError

        agent = TradingAgent.__new__(TradingAgent)
        agent._settings = SimpleNamespace(paper_trading=False)
        groww = SimpleNamespace(
            get_quote=AsyncMock(return_value={}),
            place_order=AsyncMock(side_effect=OrderStateUnknownError("unknown", "ATABCDEF12345678")),
            place_stop_loss_order=AsyncMock(),
        )
        signal = SimpleNamespace(stock="RELIANCE", ltp=2500.0, atr=10.0, exchange="NSE")
        decision = SimpleNamespace(
            adjusted_qty=5, product_type=SimpleNamespace(value="MIS"),
            stop_loss_price=2485.0, target_price=2530.0, rationale="x",
        )
        from tests.test_order_lifecycle import _Db

        db = _Db()
        with patch("agents.trading_agent.get_broker", return_value=groww), \
             patch("agents.trading_agent.set_halt", new=AsyncMock()) as halt, \
             patch("agents.trading_agent.send_telegram", new=AsyncMock()) as tg, \
             patch("agents.trading_agent.get_db_context", db):
            await agent._execute_trade(signal, decision)

        assert groww.place_order.await_count == 1
        assert groww.place_order.await_args.kwargs["order_reference_id"] == db.added[0].entry_order_ref
        halt.assert_awaited_once()
        tg.assert_awaited()
        groww.place_stop_loss_order.assert_not_called()
        # The outbox row stays PENDING for the reconciler — never OPENed blindly.
        assert [r.status for r in db.added] == ["PENDING"]
