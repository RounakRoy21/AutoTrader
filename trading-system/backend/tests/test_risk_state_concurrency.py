"""
Regression tests for audit Cluster C — concurrency and durable risk state:

  * R6 / C4  — the OPEN→CLOSING claim is checked (two concurrent closes → one
               SELL); the CLOSED write is conditional on CLOSING; the risk poll
               is single-flight (an overrunning poll never overlaps the next).
  * R9       — CLOSING rows stay monitored and are reconciled against the
               broker; EOD never books them at entry price / zero P&L;
               MIS_FORCE_CLOSE (15:20) escalates anything still open.
  * R18      — the 15:00 square-off runs without an LTP and retries each poll.
  * R7 / C8  — halt / streak / pause / locks are date-scoped, never reset on a
               restart, rebuilt from today's DB rows, and read fail-closed.
  * R8       — state recovery never adopts a net short as a BUY and moves DB
               OPEN rows that are flat at the broker to CLOSING (not re-sold).
  * R14      — trailing / breakeven exits get their own reasons; the stock
               lock and loss streak follow the sign of net realised P&L.

Everything broker / DB / Redis / Telegram is mocked — no network.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
from contextlib import ExitStack
from datetime import date, datetime, time as dt_time, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz
from sqlalchemy.sql.dml import Update

from agents import order_lifecycle
from models.trade import Trade
from tests.test_order_lifecycle import FakeBroker

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _fast_lifecycle(monkeypatch):
    monkeypatch.setattr(order_lifecycle, "ORDER_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(order_lifecycle, "ENTRY_FILL_TIMEOUT_S", 0.02)
    monkeypatch.setattr(order_lifecycle, "EXIT_FILL_TIMEOUT_S", 0.02)
    monkeypatch.setattr(order_lifecycle, "CANCEL_CONFIRM_TIMEOUT_S", 0.02)


# ═══════════════════════════════════════════════════════════════════════════
#  Fakes
# ═══════════════════════════════════════════════════════════════════════════


class OrderBookBroker(FakeBroker):
    """FakeBroker with an order book (``get_orders``)."""

    def __init__(self, *a, order_book=None, **kw):
        super().__init__(*a, **kw)
        self.order_book = list(order_book or [])

    async def get_orders(self):
        return list(self.order_book)


def _bind_value(v):
    return getattr(v, "value", v)


class CasDb:
    """Fake ``get_db_context`` that enforces the status compare-and-swap of
    UPDATE statements against one in-memory row, like Postgres would.

    An UPDATE whose WHERE requires ``status = X`` matches only when the row's
    status is X; RETURNING then yields the id, otherwise None.  A SELECT
    returns ``select_rows``.
    """

    def __init__(self, status: str = "OPEN", row_id: int = 7, select_rows=None) -> None:
        self.status = status
        self.row_id = row_id
        self.select_rows = list(select_rows or [])
        self.updates: list = []        # (required_status, values) of matched updates
        self.added: list = []

    async def _execute(self, stmt, *a, **kw):
        res = MagicMock()
        if isinstance(stmt, Update):
            where = " ".join(
                str(c.compile(compile_kwargs={"literal_binds": True})) for c in stmt._where_criteria
            )
            m = re.search(r"trades\.status = '(\w+)'", where)
            required = m.group(1) if m else None
            values = {getattr(k, "key", str(k)): _bind_value(v) for k, v in stmt._values.items()}
            await asyncio.sleep(0)          # let a concurrent closer interleave
            if required is not None and required != self.status:
                res.scalar_one_or_none.return_value = None
                return res
            if "status" in values:
                self.status = values["status"]
            self.updates.append((required, values))
            res.scalar_one_or_none.return_value = self.row_id
            return res
        res.scalars.return_value.all.return_value = list(self.select_rows)
        res.scalars.return_value.first.return_value = None
        return res

    def __call__(self, *a, **kw):
        db = self

        class _Ctx:
            async def __aenter__(self):
                session = MagicMock()
                session.execute = db._execute
                session.add = lambda obj: db.added.append(obj)
                session.flush = AsyncMock()
                return session

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


def _paper_broker(price: float, qty: int = 100, stop: float = 2475.0):
    """A PaperBroker quoting *price* that holds *qty* RELIANCE with a resting stop."""
    from integrations.paper_broker import PaperBroker

    broker = PaperBroker(price_source=lambda s: price)
    sl_id = broker.adopt_position("RELIANCE", qty, 2500.0, stop_price=stop)
    return broker, sl_id


def _trade(**kw) -> Trade:
    t = Trade(
        id=7, entry_order_id="GMK-BUY", stock="RELIANCE", exchange="NSE",
        direction="BUY", product_type="MIS", quantity=100, entry_price=2500.0,
        stop_loss_price=2475.0, target_price=2550.0, status="OPEN",
        trade_date=date(2026, 11, 13), entry_time=dt_time(10, 0),
        booked_pnl=0.0, partial_booked=False,
    )
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def _settings(**over):
    base = dict(
        paper_trading=False, paper_risk_use_broker_ltp=True,
        stock_lock_after_sl=True, consecutive_loss_pause_threshold=3,
        consecutive_loss_pause_minutes=30, partial_booking_enabled=False,
        partial_booking_fraction=0.5, partial_booking_move_sl_to_breakeven=True,
        roi_decay_enabled=False, trailing_sl_activation_pct=0.008,
        trailing_sl_trail_pct=0.007, risk_ltp_sanity_band_pct=0.05,
        risk_ltp_miss_alert_threshold=3, daily_drawdown_limit=30000.0,
        total_capital=1_000_000.0, daily_drawdown_soft_alert_pct=0.02,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _rm(**over):
    from agents.risk_manager import RiskManager

    rm = RiskManager()
    rm._settings = _settings(**over)
    return rm


def _rm_patches(broker=None, db=None, redis=None):
    """Patch the RiskManager's broker / DB / Redis / alerts.  *redis* is a dict
    backing get_value / set_value so lock / streak writes can be asserted."""
    redis = {} if redis is None else redis

    async def _get(key):
        return redis.get(key)

    async def _set(key, value, ttl=None):
        redis[key] = value

    stack = ExitStack()
    if broker is not None:
        stack.enter_context(patch("agents.risk_manager.get_groww_client", return_value=broker))
        stack.enter_context(patch("agents.risk_manager.get_broker", return_value=broker))
    if db is not None:
        stack.enter_context(patch("agents.risk_manager.get_db_context", db))
    stack.enter_context(patch("agents.risk_manager.publish", new=AsyncMock()))
    stack.enter_context(patch("agents.risk_manager.get_value", new=AsyncMock(side_effect=_get)))
    stack.enter_context(patch("agents.risk_manager.set_value", new=AsyncMock(side_effect=_set)))
    stack.enter_context(patch("agents.risk_manager.set_halt", new=AsyncMock()))
    stack.enter_context(patch("agents.risk_manager._transaction_costs", return_value=0.0))
    tg = stack.enter_context(patch("agents.risk_manager.send_telegram", new=AsyncMock()))
    return stack, tg, redis


def _api_patches(trades_route, broker, db, settings=None):
    """Patch the manual-close route's settings / broker / PositionService IO."""
    stack = ExitStack()
    stack.enter_context(patch.object(trades_route, "get_settings", return_value=settings or _settings()))
    stack.enter_context(patch.object(trades_route, "get_groww_client", return_value=broker))
    stack.enter_context(patch.object(trades_route, "get_broker", return_value=broker))
    stack.enter_context(patch.object(trades_route, "get_db_context", db))
    stack.enter_context(patch.object(trades_route, "publish", new=AsyncMock()))
    stack.enter_context(patch.object(trades_route, "send_telegram", new=AsyncMock()))
    stack.enter_context(patch.object(trades_route, "record_close_outcome", new=AsyncMock()))
    stack.enter_context(patch.object(trades_route, "_transaction_costs", return_value=0.0))
    return stack


def _api_db(trade):
    """The route's own request session: only its SELECT is used."""
    api_db = MagicMock()
    res = MagicMock()
    res.scalar_one_or_none.return_value = trade
    api_db.execute = AsyncMock(return_value=res)
    return api_db


def _fixed_dt(h, m):
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return tz.localize(datetime(2026, 11, 13, h, m, 0))
    return _DT


# ═══════════════════════════════════════════════════════════════════════════
#  R6 / C4 — CAS claim, guarded finalisation, single-flight poll
# ═══════════════════════════════════════════════════════════════════════════


class TestCloseRace:
    @pytest.mark.asyncio
    async def test_two_concurrent_closes_send_exactly_one_sell(self):
        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        db = CasDb(status="OPEN")
        rm = _rm()
        # Two pollers holding the same stale snapshot of the OPEN row.
        t1 = _trade(sl_order_id=sl_id)
        t2 = _trade(sl_order_id=sl_id)
        stack, _, _ = _rm_patches(broker, db)
        with stack:
            r1, r2 = await asyncio.gather(
                rm._close_position(t1, 2550.0, "TARGET_HIT"),
                rm._close_position(t2, 2470.0, "STOP_LOSS_HIT"),
            )
        assert sorted([r1, r2]) == [False, True]
        assert len(broker.sells()) == 1
        assert broker.held == 0
        assert db.status == "CLOSED"
        # Exactly one CLOSED booking — the loser never overwrote it.
        assert sum(1 for _, v in db.updates if v.get("status") == "CLOSED") == 1

    @pytest.mark.asyncio
    async def test_unclaimed_close_sends_nothing(self):
        """Row already CLOSING (manual-close API won the claim) → no broker call."""
        broker = FakeBroker(held=100)
        db = CasDb(status="CLOSING")
        rm = _rm()
        stack, _, _ = _rm_patches(broker, db)
        with stack:
            assert await rm._close_position(_trade(), 2550.0, "TARGET_HIT") is False
        assert broker.calls == []

    @pytest.mark.asyncio
    async def test_finalise_never_overwrites_a_row_no_longer_closing(self):
        db = CasDb(status="CLOSED")          # another closer already booked it
        rm = _rm(paper_trading=True)
        stack, _, redis = _rm_patches(db=db)
        with stack:
            ok = await rm._finalise_close(_trade(status="CLOSING"), 2400.0, "STOP_LOSS_HIT", 100,
                                          IST.localize(datetime(2026, 11, 13, 11, 0)))
        assert ok is False
        assert db.updates == []
        assert redis == {}                    # no lock / streak side effects

    @pytest.mark.asyncio
    async def test_manual_close_and_risk_close_race_one_sell(self):
        """The manual-close API and a RiskManager poll on the same row."""
        from api.routes import trades as trades_route

        broker = FakeBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.get_ltp = AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2530.0}})
        cas = CasDb(status="OPEN")
        api_trade = _trade(sl_order_id=sl_id)
        rm = _rm()
        stack, _, _ = _rm_patches(broker, cas)
        with stack, _api_patches(trades_route, broker, cas):
            results = await asyncio.gather(
                trades_route.close_trade_manually(7, _api_db(api_trade)),
                rm._close_position(_trade(sl_order_id=sl_id), 2470.0, "STOP_LOSS_HIT"),
                return_exceptions=True,
            )
        assert len(broker.sells()) == 1
        assert broker.held == 0
        assert sum(1 for _, v in cas.updates if v.get("status") == "CLOSED") == 1
        assert any(r is False or isinstance(r, Exception) for r in results)


class TestSingleFlightPoll:
    @pytest.mark.asyncio
    async def test_overlapping_poll_is_skipped(self):
        rm = _rm()
        gate = asyncio.Event()
        calls = []

        async def _slow_poll():
            calls.append(1)
            await gate.wait()

        rm._poll = _slow_poll
        first = asyncio.create_task(rm._poll_once())
        await asyncio.sleep(0)
        assert await rm._poll_once() is False      # refused while in flight
        gate.set()
        assert await first is True
        assert len(calls) == 1
        assert await rm._poll_once() is True       # free again afterwards

    def test_run_loop_does_not_submit_while_previous_poll_runs(self):
        rm = _rm()
        rm._loop = MagicMock()
        rm._running = True
        rm._poll_future = concurrent.futures.Future()   # never completes (timed-out poll)

        def _stop_after_one(_s):
            rm._running = False

        with patch("agents.risk_manager.asyncio.run_coroutine_threadsafe") as submit, \
             patch("agents.risk_manager.time.sleep", side_effect=_stop_after_one):
            rm._run_loop()
        submit.assert_not_called()

    def test_run_loop_timeout_does_not_cancel_inflight_poll(self):
        rm = _rm()
        rm._loop = MagicMock()
        rm._running = True
        fut = MagicMock()
        fut.done.return_value = False
        fut.result.side_effect = concurrent.futures.TimeoutError()

        def _submit(coro, loop):
            coro.close()
            return fut

        def _stop_after_one(_s):
            rm._running = False

        with patch("agents.risk_manager.asyncio.run_coroutine_threadsafe", side_effect=_submit), \
             patch("agents.risk_manager.time.sleep", side_effect=_stop_after_one):
            rm._run_loop()
        fut.cancel.assert_not_called()
        assert rm._poll_future is fut           # next cycle will see it still running


# ═══════════════════════════════════════════════════════════════════════════
#  R9 — CLOSING rows reconciled, never booked at entry / zero
# ═══════════════════════════════════════════════════════════════════════════


class TestClosingReconciliation:
    @pytest.mark.asyncio
    async def test_closing_rows_are_monitored_by_poll(self):
        rm = _rm()
        closing = SimpleNamespace(**{**_trade(status="CLOSING").__dict__})
        rm._reconcile_closing_trades = AsyncMock()
        rm._check_daily_drawdown = AsyncMock()
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={}))
        with patch("agents.risk_manager.datetime", _fixed_dt(11, 0)), \
             patch.object(rm, "_get_open_trades", new=AsyncMock(return_value=[closing])), \
             patch("agents.risk_manager.get_groww_client", return_value=groww), \
             patch("agents.risk_manager.ltp_store.get_ltp", return_value=None), \
             patch("agents.risk_manager.send_telegram", new=AsyncMock()), \
             patch("agents.risk_manager.publish", new=AsyncMock()):
            await rm._poll()
        rm._reconcile_closing_trades.assert_awaited_once()
        assert rm._reconcile_closing_trades.call_args.args[0] == [closing]

    @pytest.mark.asyncio
    async def test_flat_broker_with_executed_stop_books_stop_fill(self):
        broker = OrderBookBroker(held=100)
        sl_id = broker.add_resting_sl(100)
        broker.execute_sl(sl_id, avg=2471.0)
        db = CasDb(status="CLOSING")
        rm = _rm()
        trade = _trade(status="CLOSING", sl_order_id=sl_id)
        stack, _, _ = _rm_patches(broker, db)
        with stack:
            await rm._reconcile_closing(trade, 2480.0, IST.localize(datetime(2026, 11, 13, 11, 0)))
        assert broker.sells() == []
        assert db.status == "CLOSED"
        _, values = db.updates[-1]
        assert values["exit_price"] == 2471.0
        assert values["exit_reason"] == "STOP_LOSS_HIT"
        assert values["realized_pnl"] == pytest.approx((2471.0 - 2500.0) * 100)

    @pytest.mark.asyncio
    async def test_flat_broker_books_from_order_book_not_entry_price(self):
        broker = OrderBookBroker(held=0, order_book=[
            {"trading_symbol": "RELIANCE", "transaction_type": "SELL",
             "order_status": "EXECUTED", "average_fill_price": 2541.5},
        ])
        db = CasDb(status="CLOSING")
        rm = _rm()
        stack, tg, _ = _rm_patches(broker, db)
        with stack:
            await rm._reconcile_closing(_trade(status="CLOSING"), 2540.0,
                                        IST.localize(datetime(2026, 11, 13, 11, 0)))
        _, values = db.updates[-1]
        assert values["status"] == "CLOSED"
        assert values["exit_price"] == 2541.5
        assert values["exit_price"] != 2500.0
        assert values["realized_pnl"] != 0.0
        assert values["exit_reason"] == "RECONCILED"
        assert broker.sells() == []

    @pytest.mark.asyncio
    async def test_unexecuted_exit_is_reprotected_and_reopened_not_resold(self):
        broker = OrderBookBroker(held=100, order_book=[
            {"trading_symbol": "RELIANCE", "transaction_type": "SELL", "order_status": "REJECTED"},
        ])
        db = CasDb(status="CLOSING")
        rm = _rm()
        trade = _trade(status="CLOSING")
        stack, _, _ = _rm_patches(broker, db)
        with stack:
            await rm._reconcile_closing(trade, 2540.0, IST.localize(datetime(2026, 11, 13, 11, 0)))
        assert broker.sells() == []
        assert [c for c in broker.calls if c[0] == "place_sl"] == [("place_sl", 100, 2475.0, "MIS")]
        assert db.status == "OPEN"
        assert trade.sl_order_id is not None

    @pytest.mark.asyncio
    async def test_pending_sell_waits(self):
        broker = OrderBookBroker(held=100, order_book=[
            {"trading_symbol": "RELIANCE", "transaction_type": "SELL", "order_status": "OPEN"},
        ])
        db = CasDb(status="CLOSING")
        rm = _rm()
        stack, tg, _ = _rm_patches(broker, db)
        with stack:
            await rm._reconcile_closing(_trade(status="CLOSING"), 2540.0,
                                        IST.localize(datetime(2026, 11, 13, 11, 0)))
        assert broker.calls == []
        assert db.status == "CLOSING"
        tg.assert_awaited()

    @pytest.mark.asyncio
    async def test_paper_closing_row_with_shares_held_returns_to_open(self):
        broker, sl_id = _paper_broker(2540.0)
        db = CasDb(status="CLOSING")
        rm = _rm(paper_trading=True)
        stack, _, _ = _rm_patches(broker, db)
        with stack:
            await rm._reconcile_closing(_trade(status="CLOSING", sl_order_id=sl_id), 2540.0,
                                        IST.localize(datetime(2026, 11, 13, 11, 0)))
        assert db.status == "OPEN"
        assert (await broker.get_order_state(sl_id))["status"] == "TRIGGER_PENDING"

    @pytest.mark.asyncio
    async def test_recheck_waits_until_grace_period_then_runs(self, monkeypatch):
        import agents.risk_manager as rm_mod

        rm = _rm()
        rm._reconcile_closing = AsyncMock()
        t = _trade(status="CLOSING")
        now = IST.localize(datetime(2026, 11, 13, 11, 0))
        await rm._reconcile_closing_trades([t], {}, now)
        rm._reconcile_closing.assert_not_called()          # just became CLOSING
        monkeypatch.setattr(rm_mod, "CLOSING_RECHECK_AFTER_S", 0.0)
        await rm._reconcile_closing_trades([t], {}, now)
        rm._reconcile_closing.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_eod_report_does_not_book_closing_rows(self):
        closing = _trade(status="CLOSING")
        db = CasDb(status="CLOSING", select_rows=[closing])
        rm = _rm()
        stack, tg, _ = _rm_patches(db=db)
        with stack, patch("agents.risk_manager.send_eod_report", new=AsyncMock()):
            await rm._generate_eod_report()
        assert db.updates == []                            # nothing force-closed
        assert db.status == "CLOSING"
        assert any("NOT confirmed flat" in str(c.args[0]) for c in tg.await_args_list)


# ═══════════════════════════════════════════════════════════════════════════
#  R18 / R9 — square-off without LTP, retries, 15:20 escalation
# ═══════════════════════════════════════════════════════════════════════════


def _ns_trade(**kw):
    base = dict(
        id=1, stock="RELIANCE", exchange="NSE", entry_price=2500.0, stop_loss_price=2475.0,
        target_price=2550.0, quantity=10, product_type="MIS", partial_booked=False,
        partial_target_price=None, entry_time=dt_time(10, 30), trade_date=date(2026, 11, 13),
        booked_pnl=0.0, status="OPEN", stop_kind=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


async def _poll_at(rm, h, m, trades, groww):
    with patch("agents.risk_manager.datetime", _fixed_dt(h, m)), \
         patch.object(rm, "_get_open_trades", new=AsyncMock(return_value=trades)), \
         patch("agents.risk_manager.get_groww_client", return_value=groww), \
         patch("agents.risk_manager.ltp_store.get_ltp", return_value=None), \
         patch("agents.risk_manager.send_intraday_close_alert", new=AsyncMock()), \
         patch("agents.risk_manager.send_stop_loss_alert", new=AsyncMock()), \
         patch("agents.risk_manager.send_telegram", new=AsyncMock()) as tg, \
         patch("agents.risk_manager.publish", new=AsyncMock()):
        await rm._poll()
    return tg


class TestSquareOff:
    @pytest.mark.asyncio
    async def test_square_off_runs_without_ltp(self):
        rm = _rm()
        rm._close_position = AsyncMock(return_value=False)
        rm._check_daily_drawdown = AsyncMock()
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={}))
        await _poll_at(rm, 15, 5, [_ns_trade()], groww)
        rm._close_position.assert_awaited_once()
        assert rm._close_position.call_args.args[2] == "EOD_CLOSE"

    @pytest.mark.asyncio
    async def test_square_off_runs_when_ltp_fetch_fails(self):
        rm = _rm()
        rm._close_position = AsyncMock(return_value=False)
        rm._check_daily_drawdown = AsyncMock()
        groww = SimpleNamespace(get_ltp=AsyncMock(side_effect=RuntimeError("429")))
        await _poll_at(rm, 15, 5, [_ns_trade()], groww)
        rm._close_position.assert_awaited_once()
        rm._check_daily_drawdown.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_square_off_is_retried_every_poll(self):
        rm = _rm()
        rm._close_position = AsyncMock(return_value=False)
        rm._check_daily_drawdown = AsyncMock()
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2510.0}}))
        for _ in range(3):
            await _poll_at(rm, 15, 5, [_ns_trade()], groww)
        assert rm._close_position.await_count == 3

    @pytest.mark.asyncio
    async def test_no_exit_without_ltp_before_square_off(self):
        rm = _rm()
        rm._close_position = AsyncMock()
        rm._check_daily_drawdown = AsyncMock()
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={}))
        await _poll_at(rm, 14, 55, [_ns_trade()], groww)
        rm._close_position.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_close_escalation_once_after_1520(self):
        rm = _rm()
        rm._close_position = AsyncMock(return_value=False)
        rm._check_daily_drawdown = AsyncMock()
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={}))
        tg1 = await _poll_at(rm, 15, 21, [_ns_trade()], groww)
        tg2 = await _poll_at(rm, 15, 22, [_ns_trade()], groww)
        assert any("still not flat" in str(c.args[0]) for c in tg1.await_args_list)
        assert not any("still not flat" in str(c.args[0]) for c in tg2.await_args_list)

    @pytest.mark.asyncio
    async def test_drawdown_checked_even_with_no_positions(self):
        rm = _rm()
        rm._check_daily_drawdown = AsyncMock()
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={}))
        await _poll_at(rm, 11, 0, [], groww)
        rm._check_daily_drawdown.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
#  R7 / C8 — durable, date-scoped, fail-closed risk state
# ═══════════════════════════════════════════════════════════════════════════


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.down = False

    def _check(self):
        if self.down:
            raise ConnectionError("Redis down")

    async def get(self, key):
        self._check()
        return self.data.get(key)

    async def set(self, key, value):
        self._check()
        self.data[key] = value

    async def setex(self, key, ttl, value):
        self._check()
        self.data[key] = value

    async def delete(self, key):
        self._check()
        self.data.pop(key, None)


def _redis_patches(fake: FakeRedis, today: date):
    async def _get_redis():
        fake._check()
        return fake

    async def _set_value(key, value, ttl=None):
        await fake.set(key, value)

    stack = ExitStack()
    stack.enter_context(patch("core.risk_state.get_redis", side_effect=_get_redis))
    stack.enter_context(patch("core.redis_client.get_redis", side_effect=_get_redis))
    stack.enter_context(patch("core.nse_calendar.ist_today", return_value=today))
    stack.enter_context(patch("agents.trading_agent_manager.ist_today", return_value=today))
    stack.enter_context(patch("agents.trading_agent_manager.set_value", side_effect=_set_value))
    return stack


def _closed(stock, pnl, exit_t):
    return SimpleNamespace(stock=stock, realized_pnl=pnl, exit_time=exit_t, status="CLOSED")


def _manager_db(rows):
    class _Ctx:
        async def __aenter__(self):
            session = MagicMock()

            async def _execute(stmt, *a, **kw):
                res = MagicMock()
                res.scalar.return_value = len(rows)
                res.scalars.return_value.all.return_value = list(rows)
                return res

            session.execute = _execute
            return session

        async def __aexit__(self, *exc):
            return False

    return lambda *a, **kw: _Ctx()


class TestDurableRiskState:
    def test_compute_daily_risk_state_uses_pnl_sign(self):
        from core.risk_state import compute_daily_risk_state

        rows = [
            _closed("A", -100.0, dt_time(10, 0)),
            _closed("B", 250.0, dt_time(10, 30)),     # win resets the streak
            _closed("C", -50.0, dt_time(11, 0)),
            _closed("D", 0.0, dt_time(11, 10)),       # neutral
            _closed("E", -75.0, dt_time(11, 20)),
            _closed("F", -10.0, dt_time(11, 40)),
        ]
        st = compute_daily_risk_state(rows, date(2026, 11, 13), pause_threshold=3, pause_minutes=30)
        assert st.streak == 3
        assert st.locked_stocks == {"A", "C", "E", "F"}
        assert st.realized_loss == pytest.approx(235.0)
        assert st.pause_until == IST.localize(datetime(2026, 11, 13, 12, 10))

    @pytest.mark.asyncio
    async def test_restart_after_halt_stays_halted_and_next_day_resets(self):
        from agents.trading_agent_manager import TradingAgentManager
        from core.risk_state import is_halted_strict, set_halt

        fake = FakeRedis()
        today = date(2026, 11, 13)
        settings = _settings(daily_drawdown_limit=30000.0)
        with _redis_patches(fake, today), \
             patch("agents.trading_agent_manager.get_db_context", _manager_db([])), \
             patch("core.config.get_settings", return_value=settings):
            await set_halt("manual halt via API")
            assert await is_halted_strict() is True
            # Mid-day restart / session start must NOT clear it.
            await TradingAgentManager()._restore_trade_count()
            assert await is_halted_strict() is True
            assert "FALSE" not in fake.data.values()

        with _redis_patches(fake, today + timedelta(days=1)):
            assert await is_halted_strict() is False       # new trading day → clean

    @pytest.mark.asyncio
    async def test_restart_rebuilds_streak_pause_locks_and_drawdown_halt(self):
        from agents.trading_agent_manager import TradingAgentManager
        from core.redis_keys import consecutive_losses_key, loss_pause_until_key, stock_lock_key
        from core.risk_state import is_halted_strict

        fake = FakeRedis()                      # Redis lost everything
        last_loss = datetime.now(IST) - timedelta(minutes=5)
        today = last_loss.date()                # real "today" so the pause is still live
        now_t = last_loss.time()
        rows = [
            _closed("TCS", -12000.0, now_t),
            _closed("INFY", -11000.0, now_t),
            _closed("HDFC", -9000.0, now_t),
        ]
        settings = _settings(daily_drawdown_limit=30000.0)
        with _redis_patches(fake, today), \
             patch("agents.trading_agent_manager.get_db_context", _manager_db(rows)), \
             patch("core.config.get_settings", return_value=settings):
            await TradingAgentManager()._restore_trade_count()
            assert fake.data[consecutive_losses_key(today)] == "3"
            assert fake.data[stock_lock_key("TCS", today)] == "TRUE"
            assert await is_halted_strict() is True          # 32 000 >= 30 000
        # 3 losses >= threshold, last one 5 min ago → 30-min pause still active.
        assert loss_pause_until_key(today) in fake.data

    @pytest.mark.asyncio
    async def test_unreadable_halt_fails_closed(self):
        from core.risk_state import is_halted_strict

        fake = FakeRedis()
        fake.down = True
        with _redis_patches(fake, date(2026, 11, 13)):
            assert await is_halted_strict() is True

    @pytest.mark.asyncio
    async def test_trading_agent_skips_signal_when_halt_unreadable(self):
        from agents.trading_agent import TradingAgent

        agent = TradingAgent.__new__(TradingAgent)
        agent._settings = SimpleNamespace(paper_trading=True)
        agent._running = True
        agent._signal_queue = asyncio.Queue()
        agent._decision_engine = SimpleNamespace(process_signal=AsyncMock())
        await agent._signal_queue.put(SimpleNamespace(stock="RELIANCE"))

        async def _halted():
            agent._running = False
            return True

        with patch("agents.trading_agent.datetime", _fixed_dt(11, 0)), \
             patch("agents.trading_agent.set_value", new=AsyncMock()), \
             patch("agents.trading_agent.publish", new=AsyncMock()), \
             patch("agents.trading_agent.is_halted_strict", side_effect=_halted):
            await agent._process_decisions()
        agent._decision_engine.process_signal.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
#  R8 — state recovery: sign-aware, two-way
# ═══════════════════════════════════════════════════════════════════════════


def _agent_live():
    from agents.trading_agent import TradingAgent

    agent = TradingAgent.__new__(TradingAgent)
    agent._settings = SimpleNamespace(paper_trading=False, stop_loss_pct=0.01, min_target_pct=0.02)
    return agent


class TestStateRecovery:
    async def _run(self, positions, db_rows, order_book=None):
        db = CasDb(status="OPEN", select_rows=db_rows)
        groww = SimpleNamespace(
            get_positions=AsyncMock(return_value={"net": positions}),
            get_orders=AsyncMock(return_value=list(order_book or [])),
            place_order=AsyncMock(),
            place_stop_loss_order=AsyncMock(return_value="GMKSL-RECOVERED-1"),
            cancel_order=AsyncMock(),
            get_order_state=AsyncMock(return_value={
                "status": "CANCELLED", "filled_quantity": 0, "average_price": None,
            }),
        )
        with patch("agents.trading_agent.get_broker", return_value=groww), \
             patch("agents.trading_agent.get_db_context", db), \
             patch("agents.trading_agent.publish", new=AsyncMock()), \
             patch("agents.trading_agent.send_telegram", new=AsyncMock()) as tg, \
             patch("agents.trading_agent.set_halt", new=AsyncMock()) as halt:
            await _agent_live().state_recovery()
        return db, groww, halt, tg

    @pytest.mark.asyncio
    async def test_net_short_is_not_restored_as_buy_and_halts(self):
        db, groww, halt, tg = await self._run(
            [{"tradingsymbol": "SBIN", "quantity": -50, "average_price": 800.0, "product": "MIS"}],
            [],
        )
        assert db.added == []
        halt.assert_awaited_once()
        tg.assert_awaited()
        groww.place_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_db_open_but_broker_flat_goes_to_closing_not_resold(self):
        stale = _trade(stock="RELIANCE", status="OPEN")
        db, groww, halt, _ = await self._run([], [stale])
        assert db.status == "CLOSING"
        groww.place_order.assert_not_called()
        halt.assert_not_called()

    @pytest.mark.asyncio
    async def test_orphan_long_is_adopted_with_positive_qty(self):
        db, _, halt, _ = await self._run(
            [{"tradingsymbol": "INFY", "quantity": 20, "average_price": 1500.0, "product": "MIS"}],
            [],
        )
        assert len(db.added) == 1
        row = db.added[0]
        assert (row.stock, row.direction, row.quantity) == ("INFY", "BUY", 20)
        halt.assert_not_called()

    @pytest.mark.asyncio
    async def test_matched_long_left_alone(self):
        db, _, halt, _ = await self._run(
            [{"tradingsymbol": "RELIANCE", "quantity": 100, "average_price": 2500.0}],
            [_trade(status="OPEN")],
        )
        assert db.added == [] and db.updates == []
        halt.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
#  R14 — exit reasons and P&L-sign-driven lock / streak
# ═══════════════════════════════════════════════════════════════════════════


class TestExitReasonsAndOutcome:
    def test_stop_exit_reason_mapping(self):
        from agents.risk_manager import stop_exit_reason

        assert stop_exit_reason(_ns_trade(stop_kind=None)) == "STOP_LOSS_HIT"
        assert stop_exit_reason(_ns_trade(stop_kind="TRAILING")) == "TRAILING_SL"
        assert stop_exit_reason(_ns_trade(stop_kind="BREAKEVEN")) == "BREAKEVEN"

    @pytest.mark.asyncio
    async def test_trailed_stop_hit_closes_with_trailing_reason(self):
        rm = _rm()
        rm._close_position = AsyncMock(return_value=False)
        rm._check_daily_drawdown = AsyncMock()
        trade = _ns_trade(stop_loss_price=2520.0, stop_kind="TRAILING")
        groww = SimpleNamespace(get_ltp=AsyncMock(return_value={"NSE:RELIANCE": {"last_price": 2519.0}}))
        await _poll_at(rm, 11, 0, [trade], groww)
        assert rm._close_position.call_args.args[2] == "TRAILING_SL"

    @pytest.mark.asyncio
    async def test_trailing_update_persists_stop_kind(self):
        db = CasDb(status="OPEN")
        rm = _rm(paper_trading=True)
        trade = _trade()
        stack, _, _ = _rm_patches(db=db)
        with stack:
            await rm._update_stop_loss(trade, 2520.0, kind="TRAILING")
        assert trade.stop_kind == "TRAILING"
        assert db.updates[-1][1]["stop_kind"] == "TRAILING"

    @pytest.mark.asyncio
    async def test_profitable_trailing_exit_does_not_lock_and_resets_streak(self):
        from core.redis_keys import consecutive_losses_key, stock_lock_key

        db = CasDb(status="OPEN")
        rm = _rm(paper_trading=True)
        redis = {consecutive_losses_key(): "2"}
        broker, sl_id = _paper_broker(2520.0)
        stack, _, redis = _rm_patches(broker, db=db, redis=redis)
        with stack:
            ok = await rm._close_position(
                _trade(stop_kind="TRAILING", sl_order_id=sl_id), 2520.0, "TRAILING_SL",
            )
        assert ok is True
        assert db.updates[-1][1]["exit_reason"] == "TRAILING_SL"
        assert stock_lock_key("RELIANCE") not in redis
        assert redis[consecutive_losses_key()] == "0"

    @pytest.mark.asyncio
    async def test_losing_exit_locks_and_advances_streak_regardless_of_reason(self):
        from core.redis_keys import consecutive_losses_key, loss_pause_until_key, stock_lock_key

        db = CasDb(status="OPEN")
        rm = _rm(paper_trading=True)
        redis = {consecutive_losses_key(): "2"}
        broker, sl_id = _paper_broker(2490.0)
        stack, _, redis = _rm_patches(broker, db=db, redis=redis)
        with stack:
            await rm._close_position(_trade(sl_order_id=sl_id), 2490.0, "MANUAL")
        assert redis[stock_lock_key("RELIANCE")] == "TRUE"
        assert redis[consecutive_losses_key()] == "3"
        assert loss_pause_until_key() in redis

    @pytest.mark.asyncio
    async def test_breakeven_after_partial_sets_stop_kind(self):
        db = CasDb(status="OPEN")
        rm = _rm(paper_trading=True)
        broker, sl_id = _paper_broker(2525.0)
        trade = _trade(quantity=100, partial_target_price=2525.0, sl_order_id=sl_id)
        stack, _, _ = _rm_patches(broker, db=db)
        with stack:
            await rm._book_partial(trade, 2525.0)
        assert trade.stop_kind == "BREAKEVEN"
        assert trade.stop_loss_price == 2500.0
