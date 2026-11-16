"""
Regression tests for the reviewer findings raised against the Phase-0 fixes:

  * GrowwClient reads use the REAL growwapi 1.5.0 surface
    (get_positions_for_user / get_order_list / get_order_detail) with explicit
    timeouts, verified against ``create_autospec(GrowwAPI)``; AttributeError /
    TypeError are not retried.
  * execute_exit sells nothing while the broker-held quantity is unknown, and
    treats "accepted without an order id" / "COMPLETE with no filled qty" as
    SELL_UNKNOWN (no stop re-placed, never re-sold).
  * An ambiguous stop placement is persisted as ``ref:<id>`` and resolved /
    cancelled before any SELL.
  * _book_partial claims the row before touching the broker; _close_position
    re-reads the claimed row; a flat broker with no fill is never booked at
    zero P&L.
  * CLOSING reconciliation skips rows owned by an in-flight closer, ignores the
    row's own resting stop as a "pending exit" and keeps it.
  * The run loop alerts on a stalled poll and start() clears a hung poll.
  * Definitive 4xx rejections do not arm the API-unreachable circuit breaker.
  * The manual halt endpoint never reports an unpersisted halt.
  * State recovery adopts MIS only, tick-aligns SL / target and gives the row a
    broker stop (adopting one already resting).

Everything broker / DB / Redis / Telegram is mocked — no network.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from growwapi import GrowwAPI
from sqlalchemy.sql.dml import Update

from agents import order_lifecycle
from integrations.groww_client import OrderStateUnknownError
from models.trade import Trade
from tests.test_order_lifecycle import (
    FakeBroker,
    _Db,
    _agent,
    _agent_patches,
    _decision,
    _live_rm,
    _open_trade,
    _rm_patches as _lc_rm_patches,
    _signal,
)
from tests.test_risk_state_concurrency import (
    IST,
    CasDb,
    OrderBookBroker,
    _agent_live,
    _api_db,
    _api_patches,
    _rm,
    _rm_patches,
    _settings,
    _trade,
)


@pytest.fixture(autouse=True)
def _fast_lifecycle(monkeypatch):
    monkeypatch.setattr(order_lifecycle, "ORDER_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(order_lifecycle, "ENTRY_FILL_TIMEOUT_S", 0.02)
    monkeypatch.setattr(order_lifecycle, "EXIT_FILL_TIMEOUT_S", 0.02)
    monkeypatch.setattr(order_lifecycle, "CANCEL_CONFIRM_TIMEOUT_S", 0.02)


def _sdk():
    return create_autospec(GrowwAPI, instance=True)


def _live_client(fake_groww, real_breaker: bool = False):
    from integrations.groww_client import GrowwClient

    client = GrowwClient.__new__(GrowwClient)
    client._settings = SimpleNamespace(paper_trading=False, groww_client_id="x")
    client._groww = fake_groww
    client._last_failure = 0.0
    client._cached_token = "tok"
    client._zero_volume_warned_symbols = set()
    client.get_groww = AsyncMock(return_value=fake_groww)
    if not real_breaker:
        client._handle_failure = AsyncMock()
    return client


class _Exc(Exception):
    def __init__(self, msg="err", code=None):
        super().__init__(msg)
        if code is not None:
            self.code = code


# ═══════════════════════════════════════════════════════════════════════════
#  Broker read contract against the real SDK surface
# ═══════════════════════════════════════════════════════════════════════════


class TestBrokerReadContract:
    @pytest.mark.asyncio
    async def test_get_positions_uses_get_positions_for_user_with_timeout(self):
        from integrations.groww_client import READ_TIMEOUT_S

        fake = _sdk()
        fake.get_positions_for_user.return_value = {"positions": [
            {"trading_symbol": "INFY", "quantity": 5, "product": "MIS", "net_price": 1500.0},
            {"trading_symbol": "TCS", "credit_quantity": 10, "debit_quantity": 4, "product": "CNC"},
        ]}
        client = _live_client(fake)
        pos = await client.get_positions()
        kw = fake.get_positions_for_user.call_args.kwargs
        assert kw == {"segment": "CASH", "timeout": READ_TIMEOUT_S}
        by_sym = {p["tradingsymbol"]: p for p in pos["net"]}
        assert by_sym["INFY"]["quantity"] == 5 and by_sym["INFY"]["average_price"] == 1500.0
        assert by_sym["TCS"]["quantity"] == 6 and by_sym["TCS"]["product"] == "CNC"

    @pytest.mark.asyncio
    async def test_position_without_quantity_field_is_unknown_not_flat(self):
        fake = _sdk()
        fake.get_positions_for_user.return_value = {"positions": [
            {"trading_symbol": "INFY", "product": "MIS"},
        ]}
        client = _live_client(fake)
        assert await order_lifecycle.broker_net_qty(client, "INFY") is None

    @pytest.mark.asyncio
    async def test_broker_net_qty_ignores_cnc_holding(self):
        broker = SimpleNamespace(get_positions=AsyncMock(return_value={"net": [
            {"tradingsymbol": "INFY", "quantity": 20, "product": "CNC"},
            {"tradingsymbol": "INFY", "quantity": 4, "product": "MIS"},
        ]}))
        assert await order_lifecycle.broker_net_qty(broker, "INFY") == 4

    @pytest.mark.asyncio
    async def test_get_orders_pages_get_order_list(self):
        fake = _sdk()
        page0 = [{"groww_order_id": f"G{i}", "trading_symbol": "X"} for i in range(25)]
        page1 = [{"groww_order_id": f"H{i}", "trading_symbol": "X"} for i in range(3)]
        fake.get_order_list.side_effect = [{"order_list": page0}, {"order_list": page1}]
        client = _live_client(fake)
        orders = await client.get_orders()
        assert len(orders) == 28
        assert [c.kwargs["page"] for c in fake.get_order_list.call_args_list] == [0, 1]
        assert all(c.kwargs["segment"] == "CASH" and c.kwargs["timeout"]
                   for c in fake.get_order_list.call_args_list)

    @pytest.mark.asyncio
    async def test_get_orders_stops_when_paging_is_ignored(self):
        fake = _sdk()
        page = [{"groww_order_id": f"G{i}"} for i in range(25)]
        fake.get_order_list.return_value = {"order_list": page}   # same page every time
        client = _live_client(fake)
        orders = await client.get_orders()
        assert len(orders) == 25
        assert fake.get_order_list.call_count == 2

    @pytest.mark.asyncio
    async def test_order_state_and_ltp_reads_pass_timeouts(self):
        fake = _sdk()
        fake.get_order_detail.return_value = {"order_status": "OPEN", "quantity": 5}
        fake.get_ltp.return_value = {"NSE_INFY": 1500.0}
        client = _live_client(fake)
        await client.get_order_state("G1")
        await client.get_ltp(["NSE:INFY"])
        assert fake.get_order_detail.call_args.kwargs["timeout"]
        assert fake.get_ltp.call_args.kwargs["timeout"]

    @pytest.mark.asyncio
    async def test_find_order_by_reference(self):
        fake = _sdk()
        fake.get_order_status_by_reference.return_value = {"groww_order_id": "GMK9"}
        client = _live_client(fake)
        assert await client.find_order_by_reference("AT1") == "GMK9"

    def test_complete_without_filled_quantity_normalises_to_quantity(self):
        from integrations.groww_client import normalise_order_state

        st = normalise_order_state("G1", {"order_status": "EXECUTED", "quantity": 10,
                                          "average_fill_price": 99.5})
        assert st["status"] == "COMPLETE" and st["filled_quantity"] == 10

    def test_attribute_error_is_not_retried(self):
        from integrations.groww_client import _retry_sync

        calls = []

        @_retry_sync
        def _bad():
            calls.append(1)
            raise AttributeError("'GrowwAPI' object has no attribute 'get_positions'")

        with patch("integrations.groww_client.time.sleep") as sleep:
            with pytest.raises(AttributeError):
                _bad()
        assert calls == [1]
        sleep.assert_not_called()


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_definitive_rejection_does_not_arm_breaker(self):
        client = _live_client(_sdk(), real_breaker=True)
        with patch("integrations.groww_client.set_halt", new=AsyncMock()) as halt:
            await client._handle_failure(_Exc("order already executed", code="400"))
            assert client._last_failure == 0.0
            client._last_failure = 1.0          # stale, armed long ago
            await client._handle_failure(_Exc("trigger invalid", code="400"))
        halt.assert_not_called()

    @pytest.mark.asyncio
    async def test_ambiguous_failure_still_arms_breaker(self):
        client = _live_client(_sdk(), real_breaker=True)
        await client._handle_failure(_Exc("timeout", code="504"))
        assert client._last_failure > 0

    @pytest.mark.asyncio
    async def test_successful_cancel_resets_breaker(self):
        client = _live_client(_sdk())
        client._last_failure = 123.0
        await client.cancel_order("GMK1")
        assert client._last_failure == 0.0


# ═══════════════════════════════════════════════════════════════════════════
#  execute_exit safety
# ═══════════════════════════════════════════════════════════════════════════


class _NoIdBroker(FakeBroker):
    async def place_order(self, *a, **kw):
        await super().place_order(*a, **kw)
        return ""


class _RefBroker(FakeBroker):
    def __init__(self, *a, resolve=None, **kw):
        super().__init__(*a, **kw)
        self.resolve = resolve

    async def find_order_by_reference(self, ref):
        self.calls.append(("find_ref", ref))
        if isinstance(self.resolve, Exception):
            raise self.resolve
        return self.resolve


class TestExecuteExitSafety:
    @pytest.mark.asyncio
    async def test_unreadable_positions_sell_nothing_and_keep_stop(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.positions_raise = True
        res = await order_lifecycle.execute_exit(broker, _open_trade(sl_order_id=sl_id), 100, 2475.0)
        assert res.outcome == "POSITION_UNKNOWN"
        assert broker.calls == []                       # stop NOT cancelled, nothing sold
        assert broker.orders[sl_id]["status"] == "TRIGGER_PENDING"

    @pytest.mark.asyncio
    async def test_close_with_unreadable_positions_reverts_to_open(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.positions_raise = True
        db = CasDb(status="OPEN")
        stack, _, _ = _rm_patches(broker, db)
        with stack:
            assert await _rm()._close_position(_trade(sl_order_id=sl_id), 2550.0, "TARGET_HIT") is False
        assert broker.sells() == [] and db.status == "OPEN"

    @pytest.mark.asyncio
    async def test_sell_accepted_without_order_id_is_sell_unknown(self):
        broker = _NoIdBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        res = await order_lifecycle.execute_exit(broker, _open_trade(sl_order_id=sl_id), 100, 2475.0)
        assert res.outcome == "SELL_UNKNOWN"
        assert "ref AT" in res.message
        assert [c for c in broker.calls if c[0] == "place_sl"] == []   # never re-protected

    @pytest.mark.asyncio
    async def test_complete_sell_without_filled_qty_is_sell_unknown(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.sell_result = ("COMPLETE", 0, 2550.0)
        res = await order_lifecycle.execute_exit(broker, _open_trade(sl_order_id=sl_id), 100, 2475.0)
        assert res.outcome == "SELL_UNKNOWN"
        assert [c for c in broker.calls if c[0] == "place_sl"] == []

    @pytest.mark.asyncio
    async def test_ambiguous_stop_placement_returns_reference(self):
        broker = FakeBroker(held=10)

        async def _amb(**kw):
            raise OrderStateUnknownError("timeout", "AT123")

        broker.place_stop_loss_order = _amb
        sl = await order_lifecycle.place_stop(broker, "RELIANCE", "NSE", 10, 2475.0)
        assert sl == "ref:AT123"
        assert order_lifecycle.is_unresolved_stop(sl)

    @pytest.mark.asyncio
    async def test_unresolved_stop_is_resolved_and_cancelled_before_sell(self):
        broker = _RefBroker(held=100)
        real_sl = broker.add_resting_sl(100)
        broker.resolve = real_sl
        res = await order_lifecycle.execute_exit(
            broker, _open_trade(sl_order_id="ref:AT123"), 100, 2475.0,
        )
        assert res.outcome == "DONE" and res.sold_qty == 100
        ops = broker.ops()
        assert ops.index("find_ref") < ops.index("cancel") < ops.index("place_order")
        assert broker.orders[real_sl]["status"] == "CANCELLED"

    @pytest.mark.asyncio
    async def test_unresolvable_stop_blocks_the_sell(self):
        broker = _RefBroker(held=100, resolve=OrderStateUnknownError("?", "AT123"))
        res = await order_lifecycle.execute_exit(
            broker, _open_trade(sl_order_id="ref:AT123"), 100, 2475.0,
        )
        assert res.outcome == "STOP_UNCONFIRMED"
        assert broker.sells() == []

    @pytest.mark.asyncio
    async def test_entry_ambiguous_stop_is_persisted_and_halts(self):
        broker = FakeBroker(held=0)
        broker.get_quote = AsyncMock(return_value={})

        async def _amb(**kw):
            raise OrderStateUnknownError("timeout", "AT777")

        broker.place_stop_loss_order = _amb
        db = _Db()
        stack, sv, _ = _agent_patches(broker, db)
        with stack:
            await _agent()._execute_trade(_signal(), _decision(10))
        assert db.added[0].sl_order_id == "ref:AT777"
        sv.set_halt.assert_awaited()


# ═══════════════════════════════════════════════════════════════════════════
#  RiskManager: claims, re-read, flat-with-no-fill
# ═══════════════════════════════════════════════════════════════════════════


class _FreshDb(CasDb):
    """CasDb whose SELECT returns a given (fresh) Trade row."""

    def __init__(self, fresh: Trade, **kw):
        super().__init__(**kw)
        self.fresh = fresh

    async def _execute(self, stmt, *a, **kw):
        res = await super()._execute(stmt, *a, **kw)
        if not isinstance(stmt, Update):
            res.scalar_one_or_none.return_value = self.fresh
        return res


class TestRiskManagerClaims:
    @pytest.mark.asyncio
    async def test_book_partial_skips_row_already_claimed_by_another_closer(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        db = CasDb(status="CLOSING")          # the manual-close API owns it
        stack, _, _ = _rm_patches(broker, db)
        with stack:
            await _rm(partial_booking_enabled=True)._book_partial(_trade(sl_order_id=sl_id), 2525.0)
        assert broker.calls == []

    @pytest.mark.asyncio
    async def test_book_partial_claims_then_returns_row_to_open(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.sell_result = ("COMPLETE", None, 2526.0)
        db = CasDb(status="OPEN")
        trade = _trade(sl_order_id=sl_id)
        stack, _, _ = _rm_patches(broker, db)
        with stack:
            await _rm(partial_booking_enabled=True)._book_partial(trade, 2525.0)
        assert db.updates[0] == ("OPEN", {"status": "CLOSING"})
        required, values = db.updates[-1]
        assert required == "CLOSING" and values["status"] == "OPEN" and values["quantity"] == 50
        assert db.status == "OPEN" and trade.status == "OPEN"
        assert not order_lifecycle.is_exit_in_flight(trade.id)

    @pytest.mark.asyncio
    async def test_manual_close_and_scale_out_race_never_go_short(self):
        from api.routes import trades as trades_route

        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.get_ltp = AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2530.0}})
        cas = CasDb(status="OPEN")
        api_trade = _trade(sl_order_id=sl_id)
        rm = _rm(partial_booking_enabled=True)
        stack, _, _ = _rm_patches(broker, cas)
        with stack, _api_patches(trades_route, broker, cas):
            await asyncio.gather(
                trades_route.close_trade_manually(7, _api_db(api_trade)),
                rm._book_partial(_trade(sl_order_id=sl_id), 2525.0),
                return_exceptions=True,
            )
        assert broker.held == 0                     # flat, never short
        assert len(broker.sells()) == 1             # only one closer touched the broker
        resting = [o for o in broker.orders.values()
                   if o["side"] == "SELL" and o["status"] == "TRIGGER_PENDING"]
        assert resting == []                        # no orphan stop on a flat account

    @pytest.mark.asyncio
    async def test_close_uses_re_read_row_after_claim(self):
        broker = FakeBroker(held=40)
        old_sl = broker.add_resting_sl(100)
        broker.orders[old_sl]["status"] = "CANCELLED"
        new_sl = broker.add_resting_sl(40)
        fresh = _trade(quantity=40, sl_order_id=new_sl, booked_pnl=500.0)
        db = _FreshDb(fresh, status="OPEN")
        stale = _trade(quantity=100, sl_order_id=old_sl)
        stack, _, _ = _rm_patches(broker, db)
        with stack:
            assert await _rm()._close_position(stale, 2550.0, "TARGET_HIT") is True
        assert ("cancel", new_sl) in broker.calls
        assert broker.orders[new_sl]["status"] == "CANCELLED"
        assert broker.sells() == [("place_order", "SELL", 40, "MIS")]

    @pytest.mark.asyncio
    async def test_flat_broker_with_no_fill_is_left_closing_not_booked_at_zero(self):
        broker = FakeBroker(held=0)
        db = CasDb(status="OPEN")
        stack, tg, _ = _rm_patches(broker, db)
        with stack:
            ok = await _rm()._close_position(_trade(sl_order_id=None), 2470.0, "STOP_LOSS_HIT")
        assert ok is False
        assert db.status == "CLOSING"
        assert not any(v.get("status") == "CLOSED" for _, v in db.updates)
        tg.assert_awaited()

    @pytest.mark.asyncio
    async def test_manual_close_flat_broker_left_closing(self):
        from fastapi import HTTPException
        from api.routes import trades as trades_route

        broker = FakeBroker(held=0)
        broker.get_ltp = AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2470.0}})
        cas = CasDb(status="OPEN")
        api_trade = _trade(sl_order_id=None)
        with _api_patches(trades_route, broker, cas):
            with pytest.raises(HTTPException) as ei:
                await trades_route.close_trade_manually(7, _api_db(api_trade))
        assert ei.value.status_code == 502
        assert cas.status == "CLOSING"


# ═══════════════════════════════════════════════════════════════════════════
#  CLOSING reconciliation vs in-flight closers / own stop
# ═══════════════════════════════════════════════════════════════════════════


class TestReconcileOwnership:
    @pytest.mark.asyncio
    async def test_in_flight_row_skipped_even_after_1520(self):
        rm = _rm()
        rm._reconcile_closing = AsyncMock()
        t = _trade(status="CLOSING")
        with order_lifecycle.exit_in_flight(t.id):
            await rm._reconcile_closing_trades([t], {}, IST.localize(datetime(2026, 11, 13, 15, 21)))
        rm._reconcile_closing.assert_not_called()
        await rm._reconcile_closing_trades([t], {}, IST.localize(datetime(2026, 11, 13, 15, 21)))
        rm._reconcile_closing.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_own_resting_stop_is_not_a_pending_exit_and_is_kept(self):
        broker = OrderBookBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.order_book = [{"groww_order_id": sl_id, "trading_symbol": "RELIANCE",
                              "transaction_type": "SELL", "order_status": "TRIGGER_PENDING",
                              "order_type": "SL"}]
        db = CasDb(status="CLOSING")
        trade = _trade(status="CLOSING", sl_order_id=sl_id)
        stack, _, _ = _rm_patches(broker, db)
        with stack:
            await _rm()._reconcile_closing(trade, 2540.0, IST.localize(datetime(2026, 11, 13, 11, 0)))
        assert db.status == "OPEN"
        assert trade.sl_order_id == sl_id
        assert [c for c in broker.calls if c[0] in ("place_sl", "cancel", "place_order")] == []

    @pytest.mark.asyncio
    async def test_has_pending_sell_still_sees_other_working_sell(self):
        broker = OrderBookBroker(order_book=[
            {"groww_order_id": "MY_SL", "trading_symbol": "RELIANCE",
             "transaction_type": "SELL", "order_status": "TRIGGER_PENDING"},
            {"groww_order_id": "EXIT1", "trading_symbol": "RELIANCE",
             "transaction_type": "SELL", "order_status": "OPEN"},
        ])
        assert await order_lifecycle.has_pending_sell(broker, "RELIANCE", "MY_SL") is True
        broker.order_book = broker.order_book[:1]
        assert await order_lifecycle.has_pending_sell(broker, "RELIANCE", "MY_SL") is False


# ═══════════════════════════════════════════════════════════════════════════
#  Stalled poll
# ═══════════════════════════════════════════════════════════════════════════


class TestStalledPoll:
    def test_alert_after_n_skipped_cycles(self, monkeypatch):
        import agents.risk_manager as rm_mod

        monkeypatch.setattr(rm_mod, "POLL_STUCK_ALERT_CYCLES", 2)
        rm = _rm()
        rm._loop = MagicMock()
        rm._running = True
        rm._poll_future = concurrent.futures.Future()     # hung poll
        submitted = []
        cycles = []

        def _submit(coro, loop):
            submitted.append(coro.cr_code.co_name)
            coro.close()
            return MagicMock()

        def _sleep(_s):
            cycles.append(1)
            if len(cycles) >= 3:
                rm._running = False

        with patch("agents.risk_manager.asyncio.run_coroutine_threadsafe", side_effect=_submit), \
             patch("agents.risk_manager.time.sleep", side_effect=_sleep):
            rm._run_loop()
        assert submitted == ["_alert_poll_stuck"]           # once, no new poll submitted

    def test_start_clears_a_hung_poll(self):
        rm = _rm()
        rm._poll_future = concurrent.futures.Future()
        rm._poll_in_flight = True
        loop = MagicMock()
        with patch("agents.risk_manager.threading.Thread"), \
             patch("agents.risk_manager.asyncio.run_coroutine_threadsafe", side_effect=lambda c, l: c.close()):
            rm.start(loop)
        assert rm._poll_future is None and rm._poll_in_flight is False


# ═══════════════════════════════════════════════════════════════════════════
#  Manual halt endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestHaltEndpoint:
    @pytest.mark.asyncio
    async def test_failed_halt_write_is_not_reported_as_success(self):
        from fastapi import HTTPException
        from api.routes import system

        r = MagicMock()
        r.setex = AsyncMock(side_effect=ConnectionError("reset"))
        with patch("core.risk_state.get_redis", new=AsyncMock(return_value=r)):
            with pytest.raises(HTTPException) as ei:
                await system.halt_trading()
        assert ei.value.status_code == 503

    @pytest.mark.asyncio
    async def test_halt_not_read_back_is_not_success(self):
        from fastapi import HTTPException
        from api.routes import system

        r = MagicMock()
        r.setex = AsyncMock()
        with patch("core.risk_state.get_redis", new=AsyncMock(return_value=r)), \
             patch("core.risk_state.get_value_strict", new=AsyncMock(return_value=None)):
            with pytest.raises(HTTPException):
                await system.halt_trading()

    @pytest.mark.asyncio
    async def test_verified_halt_succeeds(self):
        from api.routes import system

        r = MagicMock()
        r.setex = AsyncMock()
        with patch("core.risk_state.get_redis", new=AsyncMock(return_value=r)), \
             patch("core.risk_state.get_value_strict", new=AsyncMock(return_value="TRUE")):
            out = await system.halt_trading()
        assert out["success"] is True and out["data"]["trading_halted"] is True


# ═══════════════════════════════════════════════════════════════════════════
#  State recovery: MIS only, tick-aligned, broker stop
# ═══════════════════════════════════════════════════════════════════════════


class TestStateRecoveryAdoption:
    async def _run(self, positions, order_book=None):
        db = CasDb(status="OPEN", select_rows=[])
        groww = SimpleNamespace(
            get_positions=AsyncMock(return_value={"net": positions}),
            get_orders=AsyncMock(return_value=list(order_book or [])),
            place_order=AsyncMock(),
            place_stop_loss_order=AsyncMock(return_value="GMKSL-NEW"),
            cancel_order=AsyncMock(),
            get_order_state=AsyncMock(return_value={
                "status": "CANCELLED", "filled_quantity": 0, "average_price": None,
            }),
        )
        with patch("agents.trading_agent.get_broker", return_value=groww), \
             patch("agents.trading_agent.get_db_context", db), \
             patch("agents.trading_agent.publish", new=AsyncMock()), \
             patch("agents.trading_agent.send_telegram", new=AsyncMock()), \
             patch("agents.trading_agent.set_halt", new=AsyncMock()) as halt:
            await _agent_live().state_recovery()
        return db, groww, halt

    @pytest.mark.asyncio
    async def test_cnc_position_is_not_adopted(self):
        db, groww, halt = await self._run(
            [{"tradingsymbol": "INFY", "quantity": 20, "average_price": 1500.0, "product": "CNC"}],
        )
        assert db.added == []
        groww.place_stop_loss_order.assert_not_called()
        halt.assert_not_called()

    @pytest.mark.asyncio
    async def test_mis_orphan_gets_tick_aligned_levels_and_a_broker_stop(self):
        db, groww, halt = await self._run(
            [{"tradingsymbol": "INFY", "quantity": 20, "average_price": 1500.03, "product": "MIS"}],
        )
        row = db.added[0]
        assert row.product_type == "MIS"
        assert row.sl_order_id == "GMKSL-NEW"
        assert round(row.stop_loss_price / 0.05, 6) == int(round(row.stop_loss_price / 0.05))
        assert row.stop_loss_price <= 1500.03 * 0.99
        assert row.target_price >= 1500.03 * 1.02
        assert groww.place_stop_loss_order.call_args.kwargs["quantity"] == 20
        halt.assert_not_called()

    @pytest.mark.asyncio
    async def test_resting_stop_is_adopted_and_extras_cancelled(self):
        book = [
            {"groww_order_id": "SL_A", "trading_symbol": "INFY", "transaction_type": "SELL",
             "order_type": "SL", "order_status": "TRIGGER_PENDING", "created_at": "10:00"},
            {"groww_order_id": "SL_B", "trading_symbol": "INFY", "transaction_type": "SELL",
             "order_type": "SL", "order_status": "TRIGGER_PENDING", "created_at": "10:01"},
        ]
        db, groww, halt = await self._run(
            [{"tradingsymbol": "INFY", "quantity": 20, "average_price": 1500.0, "product": "MIS"}],
            order_book=book,
        )
        assert db.added[0].sl_order_id == "SL_A"
        groww.place_stop_loss_order.assert_not_called()
        groww.cancel_order.assert_awaited_once_with("SL_B")
        halt.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_stop_placement_halts(self):
        db = CasDb(status="OPEN", select_rows=[])
        groww = SimpleNamespace(
            get_positions=AsyncMock(return_value={"net": [
                {"tradingsymbol": "INFY", "quantity": 20, "average_price": 1500.0, "product": "MIS"},
            ]}),
            get_orders=AsyncMock(return_value=[]),
            place_stop_loss_order=AsyncMock(side_effect=RuntimeError("rejected")),
        )
        with patch("agents.trading_agent.get_broker", return_value=groww), \
             patch("agents.trading_agent.get_db_context", db), \
             patch("agents.trading_agent.publish", new=AsyncMock()), \
             patch("agents.trading_agent.send_telegram", new=AsyncMock()), \
             patch("agents.trading_agent.set_halt", new=AsyncMock()) as halt:
            await _agent_live().state_recovery()
        assert db.added[0].sl_order_id is None
        halt.assert_awaited_once()
