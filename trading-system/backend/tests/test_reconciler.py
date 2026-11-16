"""
Reconciler (audit Part B C7 / C9): periodic two-way broker ⇄ DB diff, run
against a real ``PaperBroker`` and the CAS-enforcing fake DB.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import date, datetime, time as dt_time
from unittest.mock import AsyncMock, patch

import pytest

from agents import order_lifecycle
from agents.reconciler import Reconciler
from integrations.paper_broker import PaperBroker
from tests.test_risk_state_concurrency import IST, CasDb, _settings, _trade

NOW = IST.localize(datetime(2026, 11, 13, 11, 0))


@pytest.fixture(autouse=True)
def _fast_lifecycle(monkeypatch):
    monkeypatch.setattr(order_lifecycle, "ORDER_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(order_lifecycle, "CANCEL_CONFIRM_TIMEOUT_S", 0.02)


def _broker(price=2500.0):
    return PaperBroker(price_source=lambda s: price, today=lambda: NOW.date())


def _patches(broker, db):
    stack = ExitStack()
    stack.enter_context(patch("agents.reconciler.get_broker", return_value=broker))
    stack.enter_context(patch("agents.reconciler.get_db_context", db))
    stack.enter_context(patch("agents.reconciler.publish", new=AsyncMock()))
    tg = stack.enter_context(patch("agents.reconciler.send_telegram", new=AsyncMock()))
    halt = stack.enter_context(patch("agents.reconciler.set_halt", new=AsyncMock()))
    return stack, tg, halt


def _row(**kw):
    kw.setdefault("trade_date", NOW.date())
    kw.setdefault("entry_time", dt_time(10, 0))
    return _trade(**kw)


async def _cycle(rec, broker, db):
    stack, tg, halt = _patches(broker, db)
    with stack:
        report = await rec.reconcile_once(NOW)
    return report, tg, halt


class TestPositions:
    @pytest.mark.asyncio
    async def test_net_short_halts_and_alerts_once(self):
        broker = _broker()
        await broker.place_order("SBIN", transaction_type="SELL", quantity=50)
        rec = Reconciler(_settings())
        report, tg, halt = await _cycle(rec, broker, CasDb())
        assert report["net_shorts"] == ["SBIN"]
        halt.assert_awaited_once()
        tg.assert_awaited_once()
        _, tg2, halt2 = await _cycle(rec, broker, CasDb())
        halt2.assert_not_called()
        tg2.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_open_broker_flat_moves_to_closing_after_two_cycles(self):
        broker = _broker()                         # flat: the broker stop fired
        row = _row(status="OPEN")
        db = CasDb(status="OPEN", select_rows=[row])
        rec = Reconciler(_settings())
        first, _, _ = await _cycle(rec, broker, db)
        assert first["flat_to_closing"] == [] and db.status == "OPEN"
        second, _, _ = await _cycle(rec, broker, db)
        assert second["flat_to_closing"] == ["RELIANCE#7"]
        assert db.status == "CLOSING" and row.status == "CLOSING"
        assert not any(v.get("status") == "CLOSED" for _, v in db.updates)   # never booked here

    @pytest.mark.asyncio
    async def test_transient_mismatch_that_clears_is_not_acted_on(self):
        row = _row(status="OPEN", quantity=100)
        db = CasDb(status="OPEN", select_rows=[row])
        rec = Reconciler(_settings())
        await _cycle(rec, _broker(), db)                     # broker lagging: flat
        held = _broker()
        held.adopt_position("RELIANCE", 100, 2500.0)
        await _cycle(rec, held, db)                          # now matches → forgotten
        await _cycle(rec, _broker(), db)                     # flat again: first sighting only
        assert db.status == "OPEN" and db.updates == []

    @pytest.mark.asyncio
    async def test_row_with_exit_in_flight_is_skipped(self):
        broker = _broker()
        row = _row(status="OPEN")
        db = CasDb(status="OPEN", select_rows=[row])
        rec = Reconciler(_settings())
        with order_lifecycle.exit_in_flight(row.id):
            await _cycle(rec, broker, db)
            await _cycle(rec, broker, db)
        assert db.status == "OPEN"

    @pytest.mark.asyncio
    async def test_orphan_broker_long_is_alerted_not_touched(self):
        broker = _broker()
        await broker.place_order("INFY", quantity=20)
        db = CasDb()
        rec = Reconciler(_settings())
        await _cycle(rec, broker, db)
        report, tg, halt = await _cycle(rec, broker, db)
        assert report["orphan_longs"] == ["INFY"]
        tg.assert_awaited()
        halt.assert_not_called()
        assert db.updates == [] and db.added == []
        assert await order_lifecycle.broker_net_qty(broker, "INFY") == 20


class TestOrphanStops:
    @pytest.mark.asyncio
    async def test_orphan_stop_on_flat_symbol_is_cancelled_after_two_cycles(self):
        broker = _broker()
        sl = await broker.place_stop_loss_order("INFY", 10, 2400.0)
        rec = Reconciler(_settings())
        first, _, _ = await _cycle(rec, broker, CasDb())
        assert first["orphan_stops_cancelled"] == []
        second, _, _ = await _cycle(rec, broker, CasDb())
        assert second["orphan_stops_cancelled"] == [sl]
        assert (await broker.get_order_state(sl))["status"] == "CANCELLED"

    @pytest.mark.asyncio
    async def test_a_positions_own_stop_is_left_alone(self):
        broker = _broker()
        sl = broker.adopt_position("RELIANCE", 100, 2500.0, stop_price=2475.0)
        row = _row(status="OPEN", sl_order_id=sl)
        db = CasDb(status="OPEN", select_rows=[row])
        rec = Reconciler(_settings())
        for _ in range(3):
            await _cycle(rec, broker, db)
        assert (await broker.get_order_state(sl))["status"] == "TRIGGER_PENDING"
        assert db.updates == []


class TestPendingEntries:
    @pytest.mark.asyncio
    async def test_never_placed_entry_is_rejected(self):
        broker = _broker()
        row = _row(status="PENDING", entry_order_ref="ATNEVER000001")
        db = CasDb(status="PENDING", select_rows=[row])
        report, _, _ = await _cycle(Reconciler(_settings()), broker, db)
        assert report["pending_rejected"] == ["RELIANCE#7"]
        assert db.status == "REJECTED"

    @pytest.mark.asyncio
    async def test_filled_entry_is_opened_with_its_resting_stop_adopted(self):
        broker = _broker(price=2502.0)
        oid = await broker.place_order("RELIANCE", quantity=10, order_reference_id="ATFILLED00001")
        sl = await broker.place_stop_loss_order("RELIANCE", 10, 2480.0)   # placed before the crash
        row = _row(status="PENDING", quantity=10, entry_order_ref="ATFILLED00001",
                   stop_loss_price=2480.0, sl_order_id=None)
        db = CasDb(status="PENDING", select_rows=[row])
        report, tg, halt = await _cycle(Reconciler(_settings()), broker, db)
        assert report["pending_opened"] == ["RELIANCE#7"]
        _, values = db.updates[-1]
        assert values["status"] == "OPEN"
        assert values["entry_order_id"] == oid
        assert values["quantity"] == 10 and values["entry_price"] == 2502.0
        assert values["sl_order_id"] == sl                 # adopted, not doubled
        assert len([o for o in await broker.get_orders() if o["order_type"] == "SL"]) == 1
        halt.assert_not_called()

    @pytest.mark.asyncio
    async def test_filled_entry_without_stop_gets_one_placed(self):
        broker = _broker(price=2502.0)
        await broker.place_order("RELIANCE", quantity=10, order_reference_id="ATFILLED00002")
        row = _row(status="PENDING", quantity=10, entry_order_ref="ATFILLED00002",
                   stop_loss_price=2480.0, sl_order_id=None)
        db = CasDb(status="PENDING", select_rows=[row])
        await _cycle(Reconciler(_settings()), broker, db)
        new_sl = db.updates[-1][1]["sl_order_id"]
        state = await broker.get_order_state(new_sl)
        assert state["status"] == "TRIGGER_PENDING" and state["quantity"] == 10

    @pytest.mark.asyncio
    async def test_young_pending_row_is_left_for_its_entry(self):
        broker = _broker()
        row = _row(status="PENDING", entry_order_ref="ATYOUNG000001", entry_time=dt_time(10, 59, 50))
        db = CasDb(status="PENDING", select_rows=[row])
        await _cycle(Reconciler(_settings()), broker, db)
        assert db.status == "PENDING" and db.updates == []

    @pytest.mark.asyncio
    async def test_in_flight_pending_row_is_left_alone(self):
        broker = _broker()
        row = _row(status="PENDING", entry_order_ref="ATFLIGHT00001")
        db = CasDb(status="PENDING", select_rows=[row])
        with order_lifecycle.trade_in_flight(row.id):
            await _cycle(Reconciler(_settings()), broker, db)
        assert db.status == "PENDING"


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_cycle_errors_never_escape_and_loop_stops(self):
        rec = Reconciler(_settings(reconcile_interval_s=0.001))
        rec.reconcile_once = AsyncMock(side_effect=RuntimeError("boom"))
        calls = {"n": 0}

        def _running():
            calls["n"] += 1
            return calls["n"] <= 2

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW

        with patch("agents.reconciler.datetime", _DT):
            await asyncio.wait_for(rec.run(_running), timeout=2)
        assert rec.reconcile_once.await_count == 2
