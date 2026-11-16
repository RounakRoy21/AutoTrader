"""
PositionService ownership + outbox entries (audit Part B §3.1, C4, C9) and the
lower-circuit exit guard, across the RiskManager, the manual-close API and the
TradingAgent.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from agents import order_lifecycle, position_service
from agents.position_service import CloseOutcome, PositionDeps, PositionService
from integrations.paper_broker import PaperBroker
from tests.test_order_lifecycle import FakeBroker, _Db, _agent, _agent_patches, _decision, _signal
from tests.test_risk_state_concurrency import (
    IST,
    CasDb,
    _api_db,
    _api_patches,
    _paper_broker,
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


def _service(db, broker, notify=None):
    return PositionService(_settings(), PositionDeps(
        db_context=db,
        broker=lambda: broker,
        publish=AsyncMock(),
        notify=notify or AsyncMock(),
        costs=lambda *a: 0.0,
        record_outcome=AsyncMock(),
    ))


# ═══════════════════════════════════════════════════════════════════════════
#  Outbox transitions
# ═══════════════════════════════════════════════════════════════════════════


class TestOutbox:
    @pytest.mark.asyncio
    async def test_create_pending_writes_row_with_reference(self):
        db = _Db()
        trade = await _service(db, None).create_pending(
            stock="INFY", exchange="NSE", quantity=10, entry_price=1500.0,
            stop_loss_price=1485.0, target_price=1530.0, order_reference_id="ATREF0000001",
            rationale="r", now_ist=IST.localize(datetime(2026, 11, 13, 10, 0)),
        )
        assert db.added == [trade]
        assert (trade.status, trade.entry_order_ref, trade.product_type) == ("PENDING", "ATREF0000001", "MIS")

    @pytest.mark.asyncio
    async def test_confirm_entry_is_a_cas_from_pending(self):
        db = CasDb(status="OPEN")                 # someone else already resolved it
        trade = _trade(status="PENDING")
        ok = await _service(db, None).confirm_entry(
            trade, order_id="G1", quantity=10, entry_price=1.0, stop_loss_price=0.9,
            target_price=1.2, partial_target_price=None, sl_order_id="S1",
        )
        assert ok is False and trade.status == "PENDING" and db.updates == []

    @pytest.mark.asyncio
    async def test_reject_entry_moves_pending_to_rejected(self):
        db = CasDb(status="PENDING")
        trade = _trade(status="PENDING")
        assert await _service(db, None).reject_entry(trade, "nothing filled") is True
        assert db.status == "REJECTED" and trade.status == "REJECTED"


class TestEntryOutbox:
    @pytest.mark.asyncio
    async def test_db_failure_before_order_sends_nothing(self):
        broker = FakeBroker(held=0)
        broker.get_quote = AsyncMock(return_value={})

        def _broken_db(*a, **kw):
            raise ConnectionError("postgres down")

        stack, _, _ = _agent_patches(broker, _broken_db)
        with stack:
            await _agent()._execute_trade(_signal(), _decision(10))
        assert broker.calls == []                 # no order without its PENDING row

    @pytest.mark.asyncio
    async def test_reference_on_the_row_is_the_one_sent(self):
        broker = FakeBroker(held=0)
        broker.get_quote = AsyncMock(return_value={})
        sent = {}
        original = broker.place_order

        async def _spy(*a, **kw):
            sent.update(kw)
            return await original(*a, **kw)

        broker.place_order = _spy
        db = _Db()
        stack, _, _ = _agent_patches(broker, db)
        with stack:
            await _agent()._execute_trade(_signal(), _decision(10))
        assert sent["order_reference_id"] == db.added[0].entry_order_ref

    @pytest.mark.asyncio
    async def test_open_write_failure_after_fill_halts_and_keeps_pending(self):
        broker = FakeBroker(held=0)
        broker.get_quote = AsyncMock(return_value={})
        db = _Db(claim_ok=False)                  # PENDING → OPEN CAS misses
        stack, sv, tg = _agent_patches(broker, db)
        with stack:
            await _agent()._execute_trade(_signal(), _decision(10))
        assert db.added[0].status == "PENDING"    # left for the reconciler
        sv.set_halt.assert_awaited()
        assert broker.held == 10                  # the fill is real; nothing undone blindly

    @pytest.mark.asyncio
    async def test_paper_entry_runs_through_paper_broker_with_stop(self):
        broker = PaperBroker(price_source=lambda s: 2503.0)
        db = _Db()
        agent = _agent()
        agent._settings.paper_trading = True
        stack, _, _ = _agent_patches(broker, db)
        with stack:
            await agent._execute_trade(_signal(), _decision(10))
        row = db.added[0]
        assert row.status == "OPEN" and row.entry_price == 2503.0
        assert (await broker.get_order_state(row.sl_order_id))["status"] == "TRIGGER_PENDING"
        assert await order_lifecycle.broker_net_qty(broker, "RELIANCE") == 10

    @pytest.mark.asyncio
    async def test_one_failing_signal_does_not_end_the_decision_loop(self):
        from agents.trading_agent import TradingAgent

        agent = TradingAgent.__new__(TradingAgent)
        agent._settings = SimpleNamespace(paper_trading=True)
        agent._running = True
        agent._signal_queue = asyncio.Queue()
        for stock in ("BAD", "GOOD"):
            await agent._signal_queue.put(SimpleNamespace(stock=stock))
        agent._decision_engine = SimpleNamespace(process_signal=AsyncMock(return_value=object()))
        seen = []

        async def _execute(signal, decision):
            seen.append(signal.stock)
            if signal.stock == "BAD":
                raise RuntimeError("boom")
            agent._running = False

        agent._execute_trade = _execute

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return tz.localize(datetime(2026, 11, 13, 11, 0))

        with patch("agents.trading_agent.datetime", _DT), \
             patch("agents.trading_agent.set_value", new=AsyncMock()), \
             patch("agents.trading_agent.publish", new=AsyncMock()), \
             patch("agents.trading_agent.is_halted_strict", new=AsyncMock(return_value=False)):
            await asyncio.wait_for(agent._process_decisions(), timeout=5)
        assert seen == ["BAD", "GOOD"]


# ═══════════════════════════════════════════════════════════════════════════
#  Lower circuit (exit guard)
# ═══════════════════════════════════════════════════════════════════════════


class TestCircuitLockedExit:
    @pytest.mark.asyncio
    async def test_close_at_lower_circuit_keeps_stop_reverts_open_alerts_once(self):
        position_service._circuit_alerted.clear()
        broker, sl_id = _paper_broker(2480.0)            # locked just above the stop
        broker.set_circuit_limits("RELIANCE", 2480.0, 2900.0)
        db = CasDb(status="OPEN")
        stack, tg, _ = _rm_patches(broker, db)
        with stack:
            rm = _rm()
            assert await rm._close_position(_trade(sl_order_id=sl_id), 2480.0, "EOD_CLOSE") is False
            assert await rm._close_position(_trade(sl_order_id=sl_id), 2480.0, "EOD_CLOSE") is False
        assert db.status == "OPEN"
        assert (await broker.get_order_state(sl_id))["status"] == "TRIGGER_PENDING"
        assert await order_lifecycle.broker_net_qty(broker, "RELIANCE") == 100
        assert tg.await_count == 1                # one alert, retried silently

    @pytest.mark.asyncio
    async def test_scale_out_at_lower_circuit_is_aborted(self):
        broker, sl_id = _paper_broker(2400.0)
        broker.set_circuit_limits("RELIANCE", 2400.0, 2900.0)
        db = CasDb(status="OPEN")
        trade = _trade(sl_order_id=sl_id)
        stack, _, _ = _rm_patches(broker, db)
        with stack:
            await _rm(partial_booking_enabled=True)._book_partial(trade, 2400.0)
        assert db.status == "OPEN" and trade.quantity == 100 and trade.partial_booked is False


# ═══════════════════════════════════════════════════════════════════════════
#  Manual close via PositionService
# ═══════════════════════════════════════════════════════════════════════════


class TestManualCloseService:
    @pytest.mark.asyncio
    async def test_paper_manual_close_books_paper_fill(self):
        from api.routes import trades as trades_route

        broker, sl_id = _paper_broker(2530.0)
        cas = CasDb(status="OPEN")
        trade = _trade(sl_order_id=sl_id)
        with _api_patches(trades_route, broker, cas, settings=_settings(paper_trading=True)), \
             patch.object(trades_route.ltp_store, "get_ltp", return_value=2530.0):
            out = await trades_route.close_trade_manually(7, _api_db(trade))
        assert out.status == "CLOSED" and out.exit_price == 2530.0
        assert out.exit_reason == "MANUAL"
        assert await order_lifecycle.broker_net_qty(broker, "RELIANCE") == 0

    @pytest.mark.asyncio
    async def test_lost_claim_is_409_and_touches_nothing(self):
        from api.routes import trades as trades_route

        broker = FakeBroker(held=100)
        broker.get_ltp = AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2530.0}})
        cas = CasDb(status="CLOSING")             # the RiskManager claimed it first
        with _api_patches(trades_route, broker, cas):
            with pytest.raises(HTTPException) as ei:
                await trades_route.close_trade_manually(7, _api_db(_trade()))
        assert ei.value.status_code == 409
        assert broker.calls == []

    @pytest.mark.asyncio
    async def test_request_session_never_flushes_the_trade(self):
        """The route detaches the row so its session cannot overwrite the
        service's guarded writes on commit."""
        from api.routes import trades as trades_route

        broker = FakeBroker(held=100)
        broker.get_ltp = AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2530.0}})
        trade = _trade()
        api_db = _api_db(trade)
        with _api_patches(trades_route, broker, CasDb(status="OPEN")):
            await trades_route.close_trade_manually(7, api_db)
        api_db.expunge.assert_called_once_with(trade)

    def test_close_outcome_is_falsy_unless_closed(self):
        assert not CloseOutcome(False, "x")
        assert CloseOutcome(True)


# ═══════════════════════════════════════════════════════════════════════════
#  Paper restart: PaperBroker hydrated from the DB
# ═══════════════════════════════════════════════════════════════════════════


class TestPaperHydration:
    @pytest.mark.asyncio
    async def test_open_rows_are_adopted_with_stops(self):
        from agents.trading_agent import TradingAgent

        broker = PaperBroker(price_source=lambda s: 2510.0)
        row = _trade(status="OPEN", sl_order_id=None)
        db = CasDb(status="OPEN", select_rows=[row])
        agent = TradingAgent.__new__(TradingAgent)
        agent._settings = _settings(paper_trading=True)
        with patch("agents.trading_agent.get_broker", return_value=broker), \
             patch("agents.trading_agent.get_db_context", db), \
             patch("agents.trading_agent.set_halt", new=AsyncMock()) as halt:
            await agent.state_recovery()
        assert await order_lifecycle.broker_net_qty(broker, "RELIANCE") == 100
        assert row.sl_order_id and db.updates[-1][1]["sl_order_id"] == row.sl_order_id
        assert (await broker.get_order_state(row.sl_order_id))["status"] == "TRIGGER_PENDING"
        halt.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
#  Exposure / count queries see the outbox statuses
# ═══════════════════════════════════════════════════════════════════════════


def _capturing_db(statements, scalar=0):
    class _Ctx:
        async def __aenter__(self):
            session = MagicMock()

            async def _execute(stmt, *a, **kw):
                statements.append(str(stmt.compile(compile_kwargs={"literal_binds": True})))
                res = MagicMock()
                res.scalar.return_value = scalar
                return res

            session.execute = _execute
            return session

        async def __aexit__(self, *exc):
            return False

    return lambda *a, **kw: _Ctx()


class TestOutboxStatusesInQueries:
    @pytest.mark.asyncio
    async def test_open_position_count_includes_pending(self):
        from agents.decision_engine import DecisionEngine

        statements: list = []
        engine = DecisionEngine(asyncio.Queue())
        with patch("agents.decision_engine.get_db_context", _capturing_db(statements)):
            await engine._count_open_positions()
        assert "'PENDING'" in statements[0] and "'OPEN'" in statements[0]

    @pytest.mark.asyncio
    async def test_restored_trade_count_excludes_rejected(self):
        from agents.trading_agent_manager import TradingAgentManager

        statements: list = []
        mgr = TradingAgentManager()
        mgr._restore_risk_state = AsyncMock()
        with patch("agents.trading_agent_manager.get_db_context", _capturing_db(statements, 2)), \
             patch("agents.trading_agent_manager.set_value", new=AsyncMock()):
            await mgr._restore_trade_count()
        assert "!= 'REJECTED'" in statements[0]
