"""
BrokerPort contract (audit Part B D1): one suite run against BOTH
implementations — ``PaperBroker`` and ``GrowwBroker`` (over the real
``GrowwClient`` driving a stateful simulator of the ``growwapi.GrowwAPI``
surface, autospecced so a wrong SDK method / kwarg fails the test).

Plus PaperBroker-specific simulation behaviour and the ``get_broker`` factory.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec, patch

import pytest

from agents import order_lifecycle
from integrations.broker import BrokerPort, get_broker
from integrations.groww_broker import GrowwBroker
from integrations.paper_broker import PaperBroker, PaperBrokerError


@pytest.fixture(autouse=True)
def _fast_lifecycle(monkeypatch):
    monkeypatch.setattr(order_lifecycle, "ORDER_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(order_lifecycle, "ENTRY_FILL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(order_lifecycle, "EXIT_FILL_TIMEOUT_S", 0.05)
    monkeypatch.setattr(order_lifecycle, "CANCEL_CONFIRM_TIMEOUT_S", 0.05)


# ═══════════════════════════════════════════════════════════════════════════
#  Harnesses
# ═══════════════════════════════════════════════════════════════════════════


class _SdkError(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


class GrowwSdkSim:
    """Stateful stand-in for ``growwapi.GrowwAPI`` (autospecced)."""

    def __init__(self) -> None:
        from growwapi import GrowwAPI

        self.prices: dict = {}
        self.circuits: dict = {}
        self.orders: dict = {}
        self.refs: dict = {}
        self.net: dict = {}
        self._n = 0
        sdk = create_autospec(GrowwAPI, instance=True)
        sdk.place_order.side_effect = self._place_order
        sdk.cancel_order.side_effect = self._cancel
        sdk.get_order_detail.side_effect = self._detail
        sdk.get_positions_for_user.side_effect = self._positions
        sdk.get_order_list.side_effect = self._order_list
        sdk.get_ltp.side_effect = self._ltp
        sdk.get_quote.side_effect = self._quote
        sdk.get_order_status_by_reference.side_effect = self._by_ref
        self.sdk = sdk

    def _place_order(self, **kw):
        self._n += 1
        oid = f"GMK{self._n:010d}"
        sym, side, qty = kw["trading_symbol"], kw["transaction_type"], int(kw["quantity"])
        order = {
            "groww_order_id": oid, "trading_symbol": sym, "transaction_type": side,
            "order_type": kw["order_type"], "quantity": qty, "filled_quantity": 0,
            "order_status": "OPEN", "average_fill_price": None, "product": kw["product"],
            "order_reference_id": kw["order_reference_id"],
        }
        if kw["order_type"] == "MARKET":
            px = self.prices[sym]
            order.update(order_status="EXECUTED", filled_quantity=qty, average_fill_price=px)
            self.net[sym] = self.net.get(sym, 0) + (qty if side == "BUY" else -qty)
        else:
            order["order_status"] = "TRIGGER_PENDING"
        self.orders[oid] = order
        self.refs[kw["order_reference_id"]] = oid
        return {"groww_order_id": oid, "order_status": "OPEN"}

    def _cancel(self, groww_order_id, segment, timeout):
        order = self.orders[groww_order_id]
        if order["order_status"] in ("EXECUTED", "CANCELLED", "REJECTED"):
            raise _SdkError("order not cancellable", "400")
        order["order_status"] = "CANCELLED"
        return {"groww_order_id": groww_order_id, "order_status": "CANCELLATION_REQUESTED"}

    def _detail(self, segment, groww_order_id, timeout):
        return dict(self.orders[groww_order_id])

    def _positions(self, segment, timeout):
        return {"positions": [
            {"trading_symbol": s, "quantity": q, "product": "MIS", "net_price": 0}
            for s, q in self.net.items()
        ]}

    def _order_list(self, page, segment, timeout):
        return {"order_list": [dict(o) for o in self.orders.values()] if page == 0 else []}

    def _ltp(self, segment, exchange_trading_symbols, timeout):
        wanted = set(exchange_trading_symbols)
        return {f"NSE_{s}": p for s, p in self.prices.items() if f"NSE_{s}" in wanted}

    def _quote(self, exchange, segment, trading_symbol, timeout):
        lower, upper = self.circuits.get(trading_symbol, (None, None))
        return {"last_price": self.prices.get(trading_symbol),
                "lower_circuit_limit": lower, "upper_circuit_limit": upper}

    def _by_ref(self, segment, order_reference_id, timeout):
        oid = self.refs.get(order_reference_id)
        if oid is None:
            raise _SdkError("not found", "404")
        return dict(self.orders[oid])


class _Harness:
    def __init__(self, broker, set_price, set_circuit):
        self.broker = broker
        self.set_price = set_price
        self.set_circuit = set_circuit


def _paper_harness() -> _Harness:
    prices: dict = {}
    broker = PaperBroker(price_source=prices.get)
    return _Harness(
        broker,
        lambda s, p: prices.__setitem__(s, p),
        lambda s, lo, up: broker.set_circuit_limits(s, lo, up),
    )


def _groww_harness() -> _Harness:
    from integrations.groww_client import GrowwClient

    sim = GrowwSdkSim()
    client = GrowwClient.__new__(GrowwClient)
    client._settings = SimpleNamespace(paper_trading=False, groww_client_id="x")
    client._groww = sim.sdk
    client._last_failure = 0.0
    client._cached_token = "tok"
    client._zero_volume_warned_symbols = set()
    client.get_groww = AsyncMock(return_value=sim.sdk)
    client._handle_failure = AsyncMock()
    return _Harness(
        GrowwBroker(client),
        lambda s, p: sim.prices.__setitem__(s, p),
        lambda s, lo, up: sim.circuits.__setitem__(s, (lo, up)),
    )


@pytest.fixture(params=["paper", "groww"])
def harness(request) -> _Harness:
    h = _paper_harness() if request.param == "paper" else _groww_harness()
    h.set_price("INFY", 100.0)
    return h


def _trade(**kw):
    base = dict(id=1, stock="INFY", exchange="NSE", quantity=10, sl_order_id=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ═══════════════════════════════════════════════════════════════════════════
#  Contract (both brokers)
# ═══════════════════════════════════════════════════════════════════════════


class TestBrokerContract:
    def test_satisfies_protocol(self, harness):
        assert isinstance(harness.broker, BrokerPort)

    @pytest.mark.asyncio
    async def test_market_buy_fills_terminal_with_qty_and_price(self, harness):
        oid = await harness.broker.place_order("INFY", quantity=5, order_reference_id="ATCONTRACT0001")
        assert isinstance(oid, str) and oid
        state = await harness.broker.get_order_state(oid)
        assert state["status"] == "COMPLETE"
        assert state["filled_quantity"] == 5
        assert state["average_price"] == pytest.approx(100.0)

    @pytest.mark.asyncio
    async def test_positions_report_signed_mis_quantity(self, harness):
        await harness.broker.place_order("INFY", quantity=5)
        assert await order_lifecycle.broker_net_qty(harness.broker, "INFY") == 5

    @pytest.mark.asyncio
    async def test_reference_resolves_to_the_order(self, harness):
        oid = await harness.broker.place_order("INFY", quantity=1, order_reference_id="ATCONTRACT0002")
        assert await harness.broker.find_order_by_reference("ATCONTRACT0002") == oid

    @pytest.mark.asyncio
    async def test_stop_rests_and_cancel_is_confirmed(self, harness):
        await harness.broker.place_order("INFY", quantity=5)
        sl = await harness.broker.place_stop_loss_order("INFY", 5, 95.0)
        assert not (await order_lifecycle.read_order(harness.broker, sl)).terminal
        fill = await order_lifecycle.cancel_and_confirm(harness.broker, sl)
        assert fill.terminal and fill.status == "CANCELLED"

    @pytest.mark.asyncio
    async def test_cancelling_a_filled_order_raises(self, harness):
        oid = await harness.broker.place_order("INFY", quantity=1)
        with pytest.raises(Exception):
            await harness.broker.cancel_order(oid)

    @pytest.mark.asyncio
    async def test_ltp_omits_unknown_symbols_never_zero(self, harness):
        assert await harness.broker.get_ltp(["NSE:INFY", "NSE:NOPE"]) == {
            "NSE:INFY": {"last_price": 100.0},
        }

    @pytest.mark.asyncio
    async def test_quote_reports_circuit_limits(self, harness):
        harness.set_circuit("INFY", 90.0, 110.0)
        q = (await harness.broker.get_quote(["NSE:INFY"]))["NSE:INFY"]
        assert q["lower_circuit_limit"] == 90.0 and q["upper_circuit_limit"] == 110.0

    @pytest.mark.asyncio
    async def test_execute_exit_end_to_end(self, harness):
        """The shared exit path: stop cancelled + confirmed, SELL filled, flat."""
        await harness.broker.place_order("INFY", quantity=10)
        sl = await harness.broker.place_stop_loss_order("INFY", 10, 95.0)
        harness.set_price("INFY", 102.0)
        res = await order_lifecycle.execute_exit(harness.broker, _trade(sl_order_id=sl), 10, 95.0)
        assert res.outcome == "DONE"
        assert res.sold_qty == 10 and res.remaining_qty == 0
        assert res.exit_price == pytest.approx(102.0)
        assert (await harness.broker.get_order_state(sl))["status"] == "CANCELLED"
        assert await order_lifecycle.broker_net_qty(harness.broker, "INFY") == 0

    @pytest.mark.asyncio
    async def test_exit_at_lower_circuit_touches_nothing(self, harness):
        await harness.broker.place_order("INFY", quantity=10)
        sl = await harness.broker.place_stop_loss_order("INFY", 10, 95.0)
        harness.set_price("INFY", 96.0)
        harness.set_circuit("INFY", 96.0, 120.0)
        res = await order_lifecycle.execute_exit(harness.broker, _trade(sl_order_id=sl), 10, 95.0)
        assert res.outcome == "CIRCUIT_LOCKED"
        assert res.sl_order_id == sl
        assert not (await order_lifecycle.read_order(harness.broker, sl)).terminal
        assert await order_lifecycle.broker_net_qty(harness.broker, "INFY") == 10


# ═══════════════════════════════════════════════════════════════════════════
#  Lower-circuit detection
# ═══════════════════════════════════════════════════════════════════════════


class TestLowerCircuit:
    @pytest.mark.asyncio
    async def test_quote_failure_does_not_block_exit(self):
        broker = SimpleNamespace(get_quote=AsyncMock(side_effect=RuntimeError("429")))
        assert await order_lifecycle.at_lower_circuit(broker, "INFY") is False

    @pytest.mark.asyncio
    async def test_missing_band_is_not_locked(self):
        broker = SimpleNamespace(get_quote=AsyncMock(return_value={"NSE:INFY": {
            "last_price": 90.0, "lower_circuit_limit": None}}))
        assert await order_lifecycle.at_lower_circuit(broker, "INFY") is False

    @pytest.mark.asyncio
    async def test_above_band_is_not_locked(self):
        broker = SimpleNamespace(get_quote=AsyncMock(return_value={"NSE:INFY": {
            "last_price": 91.0, "lower_circuit_limit": 90.0}}))
        assert await order_lifecycle.at_lower_circuit(broker, "INFY") is False


# ═══════════════════════════════════════════════════════════════════════════
#  PaperBroker simulation
# ═══════════════════════════════════════════════════════════════════════════


def _paper(price=100.0):
    prices = {"INFY": price}
    return PaperBroker(price_source=prices.get), prices


class TestPaperBroker:
    @pytest.mark.asyncio
    async def test_stop_fires_lazily_when_price_crosses_trigger(self):
        broker, prices = _paper()
        await broker.place_order("INFY", quantity=10)
        sl = await broker.place_stop_loss_order("INFY", 10, 95.0)
        prices["INFY"] = 94.9                     # limit = 94.50
        state = await broker.get_order_state(sl)
        assert state["status"] == "COMPLETE"
        assert state["average_price"] == 94.9
        assert await order_lifecycle.broker_net_qty(broker, "INFY") == 0

    @pytest.mark.asyncio
    async def test_gap_through_stop_limit_leaves_it_unfilled(self):
        broker, prices = _paper()
        await broker.place_order("INFY", quantity=10)
        sl = await broker.place_stop_loss_order("INFY", 10, 95.0)
        prices["INFY"] = 94.0                     # below the 94.50 limit
        assert (await broker.get_order_state(sl))["status"] == "OPEN"
        assert await order_lifecycle.broker_net_qty(broker, "INFY") == 10
        prices["INFY"] = 94.6
        assert (await broker.get_order_state(sl))["status"] == "COMPLETE"

    @pytest.mark.asyncio
    async def test_stop_trigger_not_below_ltp_is_rejected(self):
        broker, _ = _paper()
        sl = await broker.place_stop_loss_order("INFY", 10, 100.0)
        assert (await broker.get_order_state(sl))["status"] == "REJECTED"

    @pytest.mark.asyncio
    async def test_buy_without_price_is_rejected(self):
        broker = PaperBroker(price_source=lambda s: None)
        oid = await broker.place_order("INFY", quantity=5)
        state = await broker.get_order_state(oid)
        assert state["status"] == "REJECTED" and state["filled_quantity"] == 0

    @pytest.mark.asyncio
    async def test_sell_without_price_fills_at_last_known_price(self):
        broker, prices = _paper()
        await broker.place_order("INFY", quantity=5)
        del prices["INFY"]
        oid = await broker.place_order("INFY", transaction_type="SELL", quantity=5)
        assert (await broker.get_order_state(oid))["average_price"] == 100.0

    @pytest.mark.asyncio
    async def test_duplicate_reference_is_refused(self):
        broker, _ = _paper()
        await broker.place_order("INFY", quantity=1, order_reference_id="ATDUP0000001")
        with pytest.raises(PaperBrokerError):
            await broker.place_order("INFY", quantity=1, order_reference_id="ATDUP0000001")

    @pytest.mark.asyncio
    async def test_overselling_produces_a_short(self):
        broker, _ = _paper()
        await broker.place_order("INFY", quantity=5)
        await broker.place_order("INFY", transaction_type="SELL", quantity=8)
        assert await order_lifecycle.broker_net_qty(broker, "INFY") == -3

    @pytest.mark.asyncio
    async def test_modify_moves_trigger_and_rejects_above_ltp(self):
        broker, _ = _paper()
        await broker.place_order("INFY", quantity=5)
        sl = await broker.place_stop_loss_order("INFY", 5, 95.0)
        await broker.modify_stop_loss_order(sl, "INFY", 5, 98.0)
        assert (await broker.get_orders())[-1]["trigger_price"] == 98.0
        with pytest.raises(PaperBrokerError):
            await broker.modify_stop_loss_order(sl, "INFY", 5, 101.0)

    @pytest.mark.asyncio
    async def test_new_day_clears_orders_and_positions(self):
        day = {"d": date(2026, 11, 13)}
        broker = PaperBroker(price_source=lambda s: 100.0, today=lambda: day["d"])
        await broker.place_order("INFY", quantity=5)
        day["d"] = date(2026, 11, 16)
        assert (await broker.get_positions())["net"] == []
        assert await broker.get_orders() == []

    @pytest.mark.asyncio
    async def test_adopt_position_is_idempotent_and_keeps_stop_id(self):
        broker, _ = _paper()
        sl = broker.adopt_position("INFY", 10, 99.0, stop_order_id="SL-OLD", stop_price=95.0)
        assert sl == "SL-OLD"
        assert broker.adopt_position("INFY", 10, 99.0, stop_order_id="SL-OLD", stop_price=95.0) == "SL-OLD"
        assert await order_lifecycle.broker_net_qty(broker, "INFY") == 10
        assert (await broker.get_order_state("SL-OLD"))["status"] == "TRIGGER_PENDING"


class TestGetBroker:
    def test_paper_mode_returns_paper_broker(self):
        assert isinstance(get_broker(SimpleNamespace(paper_trading=True)), PaperBroker)

    def test_live_mode_returns_groww_broker(self):
        assert isinstance(get_broker(SimpleNamespace(paper_trading=False)), GrowwBroker)
