"""
Regression tests for the live order lifecycle (audit Part A R1 / R3 / R4 /
R11, Part B C1 / C5):

  * The full raw broker SL order id is persisted (string) at entry.
  * Every exit path (RiskManager close, partial booking, manual close API)
    cancels the resting SL and CONFIRMS the cancel before any SELL; an SL that
    already executed is booked instead of selling again.
  * Exits sell min(DB qty, broker-held qty), are booked from the broker's
    average_price, and an unfilled / rejected exit keeps the trade OPEN.
  * Partial booking re-places the broker SL for the remaining quantity.
  * Trailing SL moves modify the broker stop.
  * Entry fills are confirmed to a terminal state; nothing filled → no row,
    no SL; partial fill → position and SL sized to the fill.
  * product_type is always MIS.

Everything broker / DB / Redis / Telegram is mocked — no network.
"""

from __future__ import annotations

from contextlib import ExitStack
from datetime import date, time as dt_time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest
from growwapi import GrowwAPI

from agents import order_lifecycle
from models.trade import Trade


# ═══════════════════════════════════════════════════════════════════════════
#  Fake broker
# ═══════════════════════════════════════════════════════════════════════════


class FakeBroker:
    """A stateful in-memory stand-in for GrowwClient's order API.

    ``self.calls`` records every write in order so tests can assert sequencing
    (e.g. the SL cancel happens before the SELL).
    """

    def __init__(self, held: int = 0, symbol: str = "RELIANCE") -> None:
        self.symbol = symbol
        self.held = held
        self.orders: dict = {}
        self.calls: list = []
        self._n = 0
        # Behaviour knobs
        self.buy_result = ("COMPLETE", None, 2501.0)   # (status, filled or None=all, avg)
        self.sell_result = ("COMPLETE", None, 2550.0)
        self.cancel_raises = False
        self.cancel_takes_effect = True
        self.positions_raise = False

    def _new_id(self, prefix: str) -> str:
        self._n += 1
        # Long raw ids: an 8-digit truncation would lose information.
        return f"{prefix}2611140000{self._n:010d}"

    # ── writes ──
    async def place_order(self, tradingsymbol, transaction_type="BUY", quantity=1,
                          product="MIS", order_type="MARKET", exchange="NSE", tag="", **kw):
        oid = self._new_id("GMK")
        self.calls.append(("place_order", transaction_type, quantity, product))
        status, filled, avg = self.buy_result if transaction_type == "BUY" else self.sell_result
        filled = quantity if filled is None else filled
        if status == "REJECTED":
            filled = 0
        self.orders[oid] = {"status": status, "filled_quantity": filled,
                            "quantity": quantity, "average_price": avg if filled else None,
                            "side": transaction_type}
        if transaction_type == "BUY":
            self.held += filled
        else:
            self.held -= filled
        return oid

    async def place_stop_loss_order(self, tradingsymbol, quantity, trigger_price,
                                    exchange="NSE", product="MIS", transaction_type="SELL"):
        oid = self._new_id("GMKSL")
        self.calls.append(("place_sl", quantity, trigger_price, product))
        self.orders[oid] = {"status": "TRIGGER_PENDING", "filled_quantity": 0,
                            "quantity": quantity, "average_price": None, "side": "SELL"}
        return oid

    async def modify_stop_loss_order(self, order_id, tradingsymbol, quantity, trigger_price):
        self.calls.append(("modify_sl", order_id, quantity, trigger_price))

    async def cancel_order(self, order_id):
        self.calls.append(("cancel", order_id))
        if self.cancel_raises:
            raise RuntimeError("cancel failed")
        o = self.orders[order_id]
        if o["status"] in ("COMPLETE", "REJECTED", "CANCELLED"):
            raise RuntimeError("order not cancellable")
        if self.cancel_takes_effect:
            o["status"] = "CANCELLED"

    # ── reads ──
    async def get_order_state(self, order_id):
        o = self.orders[order_id]
        return {"order_id": order_id, "status": o["status"],
                "filled_quantity": o["filled_quantity"], "quantity": o["quantity"],
                "average_price": o["average_price"]}

    async def get_positions(self):
        if self.positions_raise:
            raise RuntimeError("positions down")
        return {"net": [{"tradingsymbol": self.symbol, "quantity": self.held}]}

    # ── helpers ──
    def add_resting_sl(self, qty: int) -> str:
        oid = self._new_id("GMKSL")
        self.orders[oid] = {"status": "TRIGGER_PENDING", "filled_quantity": 0,
                            "quantity": qty, "average_price": None, "side": "SELL"}
        return oid

    def execute_sl(self, oid: str, avg: float) -> None:
        o = self.orders[oid]
        o.update(status="COMPLETE", filled_quantity=o["quantity"], average_price=avg)
        self.held -= o["quantity"]

    def ops(self):
        return [c[0] for c in self.calls]

    def sells(self):
        return [c for c in self.calls if c[0] == "place_order" and c[1] == "SELL"]


@pytest.fixture(autouse=True)
def _fast_lifecycle(monkeypatch):
    """Keep the fill / cancel polling loops fast."""
    monkeypatch.setattr(order_lifecycle, "ORDER_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(order_lifecycle, "ENTRY_FILL_TIMEOUT_S", 0.02)
    monkeypatch.setattr(order_lifecycle, "EXIT_FILL_TIMEOUT_S", 0.02)
    monkeypatch.setattr(order_lifecycle, "CANCEL_CONFIRM_TIMEOUT_S", 0.02)


class _Db:
    """Fake get_db_context: records added rows and executed statements."""

    def __init__(self, claim_ok: bool = True) -> None:
        self.added: list = []
        self.statements: list = []
        self.claim_ok = claim_ok

    def __call__(self, *a, **kw):
        db = self

        class _Ctx:
            async def __aenter__(self):
                session = MagicMock()
                session.add = lambda obj: db.added.append(obj)
                session.flush = AsyncMock()

                async def _execute(stmt, *args, **kwargs):
                    db.statements.append(stmt)
                    res = MagicMock()
                    res.scalar_one_or_none.return_value = 1 if db.claim_ok else None
                    return res

                session.execute = _execute
                return session

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _open_trade(**kw) -> Trade:
    t = Trade(
        id=7, entry_order_id="GMK-BUY", stock="RELIANCE", exchange="NSE",
        direction="BUY", product_type="MIS", quantity=100, entry_price=2500.0,
        stop_loss_price=2475.0, target_price=2550.0, status="OPEN",
        trade_date=date(2026, 11, 14), entry_time=dt_time(10, 0),
        booked_pnl=0.0, partial_booked=False,
    )
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def _live_rm(**overrides):
    from agents.risk_manager import RiskManager

    rm = RiskManager.__new__(RiskManager)
    settings = dict(
        paper_trading=False,
        stock_lock_after_sl=False,
        consecutive_loss_pause_threshold=99,
        consecutive_loss_pause_minutes=30,
        partial_booking_enabled=True,
        partial_booking_fraction=0.5,
        partial_booking_move_sl_to_breakeven=True,
    )
    settings.update(overrides)
    rm._settings = SimpleNamespace(**settings)
    return rm


def _rm_patches(broker, db):
    stack = ExitStack()
    stack.enter_context(patch("agents.risk_manager.get_groww_client", return_value=broker))
    stack.enter_context(patch("agents.risk_manager.get_broker", return_value=broker))
    stack.enter_context(patch("agents.risk_manager.get_db_context", db))
    stack.enter_context(patch("agents.risk_manager.publish", new=AsyncMock()))
    stack.enter_context(patch("agents.risk_manager.get_value", new=AsyncMock(return_value="0")))
    stack.enter_context(patch("agents.risk_manager.set_value", new=AsyncMock()))
    stack.enter_context(patch("agents.risk_manager._transaction_costs", return_value=0.0))
    tg = stack.enter_context(patch("agents.risk_manager.send_telegram", new=AsyncMock()))
    return stack, tg


# ═══════════════════════════════════════════════════════════════════════════
#  R1 / C1 — RiskManager exits cancel+confirm the broker SL first
# ═══════════════════════════════════════════════════════════════════════════


class TestCloseCancelsStopFirst:
    @pytest.mark.asyncio
    async def test_target_close_cancels_sl_before_sell_and_books_avg_price(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        trade = _open_trade(sl_order_id=sl_id)
        broker.sell_result = ("COMPLETE", None, 2549.5)
        db = _Db()
        rm = _live_rm()
        stack, _ = _rm_patches(broker, db)
        with stack:
            closed = await rm._close_position(trade, 2550.0, "TARGET_HIT")

        assert closed is True
        assert broker.ops()[:2] == ["cancel", "place_order"]
        assert broker.calls[0] == ("cancel", sl_id)
        assert broker.sells() == [("place_order", "SELL", 100, "MIS")]
        assert broker.orders[sl_id]["status"] == "CANCELLED"
        assert trade.status == "CLOSED"
        assert trade.exit_price == 2549.5            # broker average_price, not LTP
        assert trade.exit_reason == "TARGET_HIT"

    @pytest.mark.asyncio
    async def test_sl_already_executed_books_sl_fill_and_sends_no_sell(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.execute_sl(sl_id, avg=2474.0)
        trade = _open_trade(sl_order_id=sl_id)
        db = _Db()
        rm = _live_rm()
        stack, _ = _rm_patches(broker, db)
        with stack:
            closed = await rm._close_position(trade, 2550.0, "TARGET_HIT")

        assert closed is True
        assert broker.sells() == []                  # NO extra SELL → no short
        assert trade.status == "CLOSED"
        assert trade.exit_reason == "STOP_LOSS_HIT"
        assert trade.exit_price == 2474.0

    @pytest.mark.asyncio
    async def test_eod_close_also_cancels_sl_first(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        trade = _open_trade(sl_order_id=sl_id)
        rm = _live_rm()
        stack, _ = _rm_patches(broker, _Db())
        with stack:
            await rm._close_position(trade, 2510.0, "EOD_CLOSE")
        assert broker.ops().index("cancel") < broker.ops().index("place_order")

    @pytest.mark.asyncio
    async def test_unconfirmed_cancel_sends_no_sell_and_keeps_trade_open(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.cancel_raises = True                  # SL stays TRIGGER_PENDING
        trade = _open_trade(sl_order_id=sl_id)
        rm = _live_rm()
        stack, tg = _rm_patches(broker, _Db())
        with stack:
            closed = await rm._close_position(trade, 2550.0, "TARGET_HIT")

        assert closed is False
        assert broker.sells() == []
        assert trade.status == "OPEN"
        assert trade.sl_order_id == sl_id            # still the live stop
        tg.assert_awaited()

    @pytest.mark.asyncio
    async def test_sell_sized_to_min_of_db_and_broker_qty(self):
        broker = FakeBroker(held=60)                 # broker holds fewer than DB
        sl_id = broker.add_resting_sl(100)
        trade = _open_trade(sl_order_id=sl_id)
        rm = _live_rm()
        stack, _ = _rm_patches(broker, _Db())
        with stack:
            await rm._close_position(trade, 2550.0, "TARGET_HIT")
        assert broker.sells() == [("place_order", "SELL", 60, "MIS")]
        assert trade.status == "CLOSED"

    @pytest.mark.asyncio
    async def test_concurrently_closed_trade_is_not_sold_again(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        trade = _open_trade(sl_order_id=sl_id)
        rm = _live_rm()
        stack, _ = _rm_patches(broker, _Db(claim_ok=False))
        with stack:
            closed = await rm._close_position(trade, 2550.0, "TARGET_HIT")
        assert closed is False
        assert broker.calls == []


# ═══════════════════════════════════════════════════════════════════════════
#  R4 — exits are confirmed to COMPLETE; a failed exit stays managed
# ═══════════════════════════════════════════════════════════════════════════


class TestExitConfirmation:
    @pytest.mark.asyncio
    async def test_rejected_sell_keeps_trade_open_with_stop_replaced(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.sell_result = ("REJECTED", 0, None)
        trade = _open_trade(sl_order_id=sl_id)
        rm = _live_rm()
        stack, tg = _rm_patches(broker, _Db())
        with stack:
            closed = await rm._close_position(trade, 2550.0, "TARGET_HIT")

        assert closed is False
        assert trade.status == "OPEN"                # NOT marked CLOSED
        new_sls = [c for c in broker.calls if c[0] == "place_sl"]
        assert new_sls and new_sls[-1][1] == 100     # full-size stop restored
        assert trade.sl_order_id and trade.sl_order_id != sl_id
        tg.assert_awaited()                          # operator alerted

    @pytest.mark.asyncio
    async def test_unfilled_sell_is_cancelled_and_trade_kept_open(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.sell_result = ("OPEN", 0, None)       # never fills → timeout → cancel
        trade = _open_trade(sl_order_id=sl_id)
        rm = _live_rm()
        stack, _ = _rm_patches(broker, _Db())
        with stack:
            closed = await rm._close_position(trade, 2550.0, "TARGET_HIT")
        assert closed is False
        assert trade.status == "OPEN"
        assert broker.ops().count("cancel") == 2     # the SL, then the unfilled SELL

    @pytest.mark.asyncio
    async def test_partially_filled_sell_books_leg_and_keeps_remainder(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.sell_result = ("CANCELLED", 40, 2548.0)
        trade = _open_trade(sl_order_id=sl_id)
        rm = _live_rm()
        stack, _ = _rm_patches(broker, _Db())
        with stack:
            closed = await rm._close_position(trade, 2550.0, "TARGET_HIT")
        assert closed is False
        assert trade.status == "OPEN"
        assert trade.quantity == 60
        assert trade.booked_pnl == pytest.approx((2548.0 - 2500.0) * 40)
        assert [c for c in broker.calls if c[0] == "place_sl"][-1][1] == 60


# ═══════════════════════════════════════════════════════════════════════════
#  Partial booking re-sizes the broker stop; trailing modifies it
# ═══════════════════════════════════════════════════════════════════════════


class TestPartialAndTrailing:
    @pytest.mark.asyncio
    async def test_partial_book_cancels_sl_sells_leg_and_replaces_sl_for_remaining(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.sell_result = ("COMPLETE", None, 2526.0)
        trade = _open_trade(sl_order_id=sl_id)
        rm = _live_rm()
        stack, _ = _rm_patches(broker, _Db())
        with stack:
            await rm._book_partial(trade, 2525.0)

        ops = broker.ops()
        assert ops.index("cancel") < ops.index("place_order") < ops.index("place_sl")
        assert broker.orders[sl_id]["status"] == "CANCELLED"
        assert broker.sells() == [("place_order", "SELL", 50, "MIS")]
        new_sl = [c for c in broker.calls if c[0] == "place_sl"][-1]
        assert new_sl[1] == 50                       # sized to the remainder only
        assert new_sl[2] == 2500.0                   # breakeven
        assert trade.quantity == 50
        assert trade.partial_booked is True
        assert trade.sl_order_id != sl_id
        assert trade.booked_pnl == pytest.approx((2526.0 - 2500.0) * 50)

    @pytest.mark.asyncio
    async def test_partial_book_when_sl_already_executed_closes_without_sell(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.execute_sl(sl_id, avg=2474.0)
        trade = _open_trade(sl_order_id=sl_id)
        rm = _live_rm()
        stack, _ = _rm_patches(broker, _Db())
        with stack:
            await rm._book_partial(trade, 2525.0)
        assert broker.sells() == []
        assert trade.status == "CLOSED"
        assert trade.exit_reason == "STOP_LOSS_HIT"

    @pytest.mark.asyncio
    async def test_partial_book_rejected_sell_restores_old_stop_full_size(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.sell_result = ("REJECTED", 0, None)
        trade = _open_trade(sl_order_id=sl_id)
        rm = _live_rm()
        stack, _ = _rm_patches(broker, _Db())
        with stack:
            await rm._book_partial(trade, 2525.0)
        restored = [c for c in broker.calls if c[0] == "place_sl"][-1]
        assert restored[1] == 100 and restored[2] == 2475.0
        assert trade.quantity == 100 and trade.partial_booked is False
        assert trade.stop_loss_price == 2475.0

    @pytest.mark.asyncio
    async def test_trailing_sl_modifies_broker_stop(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        trade = _open_trade(sl_order_id=sl_id)
        rm = _live_rm()
        stack, _ = _rm_patches(broker, _Db())
        with stack:
            await rm._update_stop_loss(trade, 2490.0)
        assert ("modify_sl", sl_id, 100, 2490.0) in broker.calls
        assert trade.stop_loss_price == 2490.0

    @pytest.mark.asyncio
    async def test_paper_partial_book_runs_through_paper_broker(self):
        """Paper mode uses the same broker path (audit V8): the leg is sold at
        the PaperBroker and the stop re-sized there."""
        from integrations.paper_broker import PaperBroker

        broker = PaperBroker(price_source=lambda s: 2525.0)
        sl_id = broker.adopt_position("RELIANCE", 100, 2500.0, stop_price=2475.0)
        trade = _open_trade(sl_order_id=sl_id)
        rm = _live_rm(paper_trading=True)
        stack, _ = _rm_patches(broker, _Db())
        with stack:
            await rm._book_partial(trade, 2525.0)
        assert trade.quantity == 50
        assert trade.booked_pnl == pytest.approx((2525.0 - 2500.0) * 50)
        pos = (await broker.get_positions())["net"][0]
        assert pos["quantity"] == 50
        assert (await broker.get_order_state(sl_id))["status"] == "CANCELLED"
        new_stop = await broker.get_order_state(trade.sl_order_id)
        assert new_stop["status"] == "TRIGGER_PENDING" and new_stop["quantity"] == 50


# ═══════════════════════════════════════════════════════════════════════════
#  Manual close API
# ═══════════════════════════════════════════════════════════════════════════


def _api_db(trade):
    db = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def _execute(stmt, *a, **kw):
        res = MagicMock()
        res.scalar_one_or_none.return_value = trade if "SELECT" in str(stmt).upper()[:10] else trade.id
        return res

    db.execute = AsyncMock(side_effect=_execute)
    return db


def _api_patches(trades_route, broker, svc_db):
    stack = ExitStack()
    stack.enter_context(patch.object(
        trades_route, "get_settings", return_value=SimpleNamespace(paper_trading=False),
    ))
    stack.enter_context(patch.object(trades_route, "get_groww_client", return_value=broker))
    stack.enter_context(patch.object(trades_route, "get_broker", return_value=broker))
    stack.enter_context(patch.object(trades_route, "get_db_context", svc_db))
    stack.enter_context(patch.object(trades_route, "send_telegram", new=AsyncMock()))
    stack.enter_context(patch.object(trades_route, "record_close_outcome", new=AsyncMock()))
    pub = stack.enter_context(patch.object(trades_route, "publish", new=AsyncMock()))
    return stack, pub


class TestManualCloseApi:
    @pytest.mark.asyncio
    async def test_manual_close_cancels_sl_before_sell(self):
        from api.routes import trades as trades_route

        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.get_ltp = AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2530.0}})
        broker.sell_result = ("COMPLETE", None, 2529.0)
        trade = _open_trade(sl_order_id=sl_id)
        stack, pub = _api_patches(trades_route, broker, _Db())
        with stack:
            await trades_route.close_trade_manually(7, _api_db(trade))

        assert broker.ops().index("cancel") < broker.ops().index("place_order")
        assert broker.sells() == [("place_order", "SELL", 100, "MIS")]
        assert pub.await_args.args[1]["exit_price"] == 2529.0

    @pytest.mark.asyncio
    async def test_manual_close_with_executed_sl_sends_no_sell(self):
        from api.routes import trades as trades_route

        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.execute_sl(sl_id, avg=2474.0)
        broker.get_ltp = AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2470.0}})
        trade = _open_trade(sl_order_id=sl_id)
        stack, pub = _api_patches(trades_route, broker, _Db())
        with stack:
            await trades_route.close_trade_manually(7, _api_db(trade))
        assert broker.sells() == []
        assert pub.await_args.args[1]["exit_reason"] == "STOP_LOSS_HIT"

    @pytest.mark.asyncio
    async def test_manual_close_rejected_sell_returns_502_and_reopens(self):
        from fastapi import HTTPException
        from api.routes import trades as trades_route

        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.get_ltp = AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2530.0}})
        broker.sell_result = ("REJECTED", 0, None)
        trade = _open_trade(sl_order_id=sl_id)
        svc_db = _Db()
        stack, _ = _api_patches(trades_route, broker, svc_db)
        with stack:
            with pytest.raises(HTTPException) as ei:
                await trades_route.close_trade_manually(7, _api_db(trade))
        assert ei.value.status_code == 502
        last_update = str(svc_db.statements[-1].compile(
            compile_kwargs={"literal_binds": True}))
        assert "status='OPEN'" in last_update.replace(" ", "")
        assert trade.status == "OPEN"


# ═══════════════════════════════════════════════════════════════════════════
#  R3 / C5 — entry fill confirmation, SL id persisted (TradingAgent)
# ═══════════════════════════════════════════════════════════════════════════


def _agent():
    from agents.trading_agent import TradingAgent

    agent = TradingAgent.__new__(TradingAgent)
    agent._settings = SimpleNamespace(
        paper_trading=False,
        atr_sl_multiplier=1.5,
        atr_target_multiplier=3.0,
        stop_loss_pct=0.01,
        min_target_pct=0.02,
        partial_booking_enabled=False,
        partial_booking_trigger_r=1.0,
    )
    return agent


def _decision(qty=10, product="MIS"):
    from schemas.decision import Decision, DecisionOutput

    return DecisionOutput(
        decision=Decision.EXECUTE, adjusted_qty=qty, stop_loss_price=2485.0,
        target_price=2530.0, product_type=product, rationale="test",
    )


def _signal():
    return SimpleNamespace(stock="RELIANCE", ltp=2500.0, atr=10.0, exchange="NSE")


def _agent_patches(broker, db):
    stack = ExitStack()
    stack.enter_context(patch("agents.trading_agent.get_broker", return_value=broker))
    stack.enter_context(patch("agents.trading_agent.get_db_context", db))
    stack.enter_context(patch("agents.trading_agent.publish", new=AsyncMock()))
    stack.enter_context(patch("agents.trading_agent.increment", new=AsyncMock()))
    stack.enter_context(patch("agents.trading_agent.send_trade_entry_alert", new=AsyncMock()))
    sv = stack.enter_context(patch("agents.trading_agent.set_value", new=AsyncMock()))
    # Halts go through the date-scoped core.risk_state.set_halt (audit R7 / C8).
    sv.set_halt = stack.enter_context(patch("agents.trading_agent.set_halt", new=AsyncMock()))
    tg = stack.enter_context(patch("agents.trading_agent.send_telegram", new=AsyncMock()))
    return stack, sv, tg


class TestEntryFillConfirmation:
    @pytest.mark.asyncio
    async def test_complete_fill_persists_full_raw_sl_order_id(self):
        broker = FakeBroker(held=0)
        broker.get_quote = AsyncMock(return_value={})
        broker.buy_result = ("COMPLETE", None, 2502.0)
        db = _Db()
        stack, _, _ = _agent_patches(broker, db)
        with stack:
            await _agent()._execute_trade(_signal(), _decision(10))

        assert len(db.added) == 1
        row = db.added[0]
        assert row.status == "OPEN"                 # PENDING outbox row promoted
        assert row.entry_order_ref                  # written before the order
        sl_calls = [c for c in broker.calls if c[0] == "place_sl"]
        assert len(sl_calls) == 1 and sl_calls[0][1] == 10
        assert isinstance(row.sl_order_id, str)
        assert row.sl_order_id.startswith("GMKSL") and len(row.sl_order_id) > 8
        assert row.sl_order_id in broker.orders     # the exact raw broker id
        assert row.quantity == 10
        assert row.entry_price == 2502.0            # broker average_price
        assert row.product_type == "MIS"

    @pytest.mark.asyncio
    async def test_rejected_buy_marks_row_rejected_and_places_no_sl(self):
        broker = FakeBroker(held=0)
        broker.get_quote = AsyncMock(return_value={})
        broker.buy_result = ("REJECTED", 0, None)
        db = _Db()
        stack, _, tg = _agent_patches(broker, db)
        with stack:
            await _agent()._execute_trade(_signal(), _decision(10))
        assert [r.status for r in db.added] == ["REJECTED"]
        assert [c for c in broker.calls if c[0] == "place_sl"] == []
        tg.assert_awaited()

    @pytest.mark.asyncio
    async def test_unfilled_buy_is_cancelled_and_row_rejected(self):
        broker = FakeBroker(held=0)
        broker.get_quote = AsyncMock(return_value={})
        broker.buy_result = ("OPEN", 0, None)        # never fills → timeout
        db = _Db()
        stack, _, _ = _agent_patches(broker, db)
        with stack:
            await _agent()._execute_trade(_signal(), _decision(10))
        assert "cancel" in broker.ops()
        assert [r.status for r in db.added] == ["REJECTED"]
        assert [c for c in broker.calls if c[0] == "place_sl"] == []

    @pytest.mark.asyncio
    async def test_partial_fill_sizes_position_and_sl_to_filled_qty(self):
        broker = FakeBroker(held=0)
        broker.get_quote = AsyncMock(return_value={})
        broker.buy_result = ("OPEN", 4, 2501.0)      # 4/10 then timeout → cancel remainder
        db = _Db()
        stack, _, _ = _agent_patches(broker, db)
        with stack:
            await _agent()._execute_trade(_signal(), _decision(10))
        assert "cancel" in broker.ops()
        assert db.added[0].quantity == 4
        assert db.added[0].original_quantity == 4
        assert [c for c in broker.calls if c[0] == "place_sl"][0][1] == 4

    @pytest.mark.asyncio
    async def test_unconfirmable_buy_halts_and_leaves_row_pending(self):
        broker = FakeBroker(held=0)
        broker.get_quote = AsyncMock(return_value={})
        broker.buy_result = ("OPEN", 0, None)
        broker.cancel_takes_effect = False           # remainder never confirmed cancelled
        db = _Db()
        stack, sv, _ = _agent_patches(broker, db)
        with stack:
            await _agent()._execute_trade(_signal(), _decision(10))
        assert [r.status for r in db.added] == ["PENDING"]   # for the reconciler
        sv.set_halt.assert_awaited()


# ═══════════════════════════════════════════════════════════════════════════
#  R11 — product_type is always MIS
# ═══════════════════════════════════════════════════════════════════════════


class TestForceMis:
    def test_llm_tool_schema_has_no_cnc(self):
        from schemas.decision import DecisionOutput

        schema_text = str(DecisionOutput.model_json_schema())
        assert "CNC" not in schema_text

    def test_cnc_from_llm_is_coerced_to_mis(self):
        from schemas.decision import DecisionOutput, ProductType

        d = DecisionOutput.model_validate(
            {"decision": "EXECUTE", "product_type": "CNC", "rationale": "x"}
        )
        assert d.product_type == ProductType.MIS

    def test_validate_decision_forces_mis(self, settings, signal):
        import asyncio

        from agents.decision_engine import DecisionEngine
        from schemas.decision import DecisionOutput, ProductType

        engine = DecisionEngine(asyncio.Queue())
        engine._settings = settings
        bad = DecisionOutput.model_construct(
            decision="EXECUTE", adjusted_qty=5, stop_loss_price=0.0, target_price=0.0,
            product_type="CNC", rationale="x",
            signal_audit=DecisionOutput.model_fields["signal_audit"].default_factory(),
        )
        out = engine._validate_decision(signal, bad)
        assert out.product_type == ProductType.MIS

    @pytest.mark.asyncio
    async def test_execute_trade_sends_mis_even_for_cnc_decision(self):
        broker = FakeBroker(held=0)
        broker.get_quote = AsyncMock(return_value={})
        decision = _decision(10).model_copy(
            update={"product_type": SimpleNamespace(value="CNC")}
        )
        db = _Db()
        stack, _, _ = _agent_patches(broker, db)
        with stack:
            await _agent()._execute_trade(_signal(), decision)
        buys = [c for c in broker.calls if c[0] == "place_order"]
        assert buys[0][3] == "MIS"
        assert all(c[3] == "MIS" for c in broker.calls if c[0] == "place_sl")
        assert db.added[0].product_type == "MIS"


# ═══════════════════════════════════════════════════════════════════════════
#  GrowwClient: raw SL id, cancel, order-state normalisation
# ═══════════════════════════════════════════════════════════════════════════


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


class TestGrowwClientLifecycle:
    @pytest.mark.asyncio
    async def test_place_stop_loss_order_returns_full_raw_id(self):
        fake = create_autospec(GrowwAPI, instance=True)
        fake.place_order.return_value = {"groww_order_id": "GMK2611140000123456789"}
        client = _live_client(fake)
        oid = await client.place_stop_loss_order("RELIANCE", 7, 1423.37)
        assert oid == "GMK2611140000123456789"
        kw = fake.place_order.call_args.kwargs
        assert kw["order_type"] == "SL" and kw["quantity"] == 7 and kw["product"] == "MIS"

    @pytest.mark.asyncio
    async def test_cancel_order_sent_once_with_raw_id(self):
        fake = create_autospec(GrowwAPI, instance=True)
        client = _live_client(fake)
        await client.cancel_order("GMK2611140000123456789")
        fake.cancel_order.assert_called_once()
        assert fake.cancel_order.call_args.kwargs["groww_order_id"] == "GMK2611140000123456789"

    @pytest.mark.asyncio
    async def test_modify_stop_loss_order_sends_tick_aligned_trigger(self):
        fake = create_autospec(GrowwAPI, instance=True)
        client = _live_client(fake)
        await client.modify_stop_loss_order("GMK1", "RELIANCE", 50, 2490.03)
        kw = fake.modify_order.call_args.kwargs
        assert kw["groww_order_id"] == "GMK1"
        assert kw["trigger_price"] == 2490.0
        assert kw["price"] < kw["trigger_price"]
        assert kw["quantity"] == 50

    @pytest.mark.asyncio
    async def test_get_order_state_normalises_groww_detail(self):
        fake = create_autospec(GrowwAPI, instance=True)
        fake.get_order_detail.return_value = {
            "groww_order_id": "GMK1", "order_status": "EXECUTED",
            "filled_quantity": 7, "quantity": 7, "average_fill_price": 1423.5,
        }
        client = _live_client(fake)
        st = await client.get_order_state("GMK1")
        assert st["status"] == "COMPLETE"
        assert st["filled_quantity"] == 7
        assert st["average_price"] == 1423.5

    def test_status_normalisation(self):
        from integrations.groww_client import normalise_order_status

        assert normalise_order_status("COMPLETED") == "COMPLETE"
        assert normalise_order_status("failed") == "REJECTED"
        assert normalise_order_status("CANCELLED") == "CANCELLED"
        assert normalise_order_status("TRIGGER_PENDING") == "TRIGGER_PENDING"
        assert normalise_order_status(None) == "UNKNOWN"
