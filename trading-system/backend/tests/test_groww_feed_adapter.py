"""Regression tests for the GrowwFeed -> KiteTicker adapter.

These tests target scanner crash paths that are hard to catch via pure indicator tests:
- callback payload parsing
- callback snapshot fallback failures
- graceful handling when feed snapshot getters raise nested-loop errors
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from integrations.groww_client import _GrowwFeedKiteTicker
from integrations.instrument_service import NIFTY50_TOKEN


class _FakeFeed:
    def __init__(self) -> None:
        self.stock_cb: Optional[Callable[[Dict[str, Any]], None]] = None
        self.index_cb: Optional[Callable[[Dict[str, Any]], None]] = None
        self.get_ltp_calls = 0
        self.get_index_value_calls = 0
        self.raise_ltp_snapshot_error = False

    def subscribe_ltp(self, instruments_list, on_data_received):
        self.stock_cb = on_data_received

    def subscribe_index_value(self, instruments_list, on_data_received):
        self.index_cb = on_data_received

    def get_ltp(self):
        self.get_ltp_calls += 1
        if self.raise_ltp_snapshot_error:
            raise RuntimeError("Cannot run the event loop while another loop is running")
        return {}

    def get_index_value(self):
        self.get_index_value_calls += 1
        return {}

    def consume(self):
        # Implemented per test by monkey-patching this method.
        return None


def test_stock_callback_uses_inline_ltp_without_snapshot_lookup():
    feed = _FakeFeed()
    ticker = _GrowwFeedKiteTicker(feed=feed)

    seen_ticks: List[Dict[str, Any]] = []

    def _on_connect(ws, _response):
        ws.subscribe([12345])

    def _on_ticks(_ws, ticks):
        seen_ticks.extend(ticks)

    def _consume():
        assert feed.stock_cb is not None
        feed.stock_cb(
            {
                "exchange": "NSE",
                "segment": "CASH",
                "feed_key": "12345",
                "ltp": 2510.25,
            }
        )

    feed.consume = _consume
    ticker.on_connect = _on_connect
    ticker.on_ticks = _on_ticks

    ticker.connect(True)

    assert feed.get_ltp_calls == 0
    assert len(seen_ticks) == 1
    assert seen_ticks[0]["instrument_token"] == 12345
    assert seen_ticks[0]["ltp"] == 2510.25


def test_stock_callback_snapshot_failure_does_not_crash_connect():
    feed = _FakeFeed()
    ticker = _GrowwFeedKiteTicker(feed=feed)

    seen_ticks: List[Dict[str, Any]] = []

    def _on_connect(ws, _response):
        ws.subscribe([67890])

    def _on_ticks(_ws, ticks):
        seen_ticks.extend(ticks)

    def _consume():
        assert feed.stock_cb is not None
        # No inline ltp/value fields -> fallback attempts get_ltp() and fails.
        feed.stock_cb(
            {
                "exchange": "NSE",
                "segment": "CASH",
                "feed_key": "67890",
            }
        )

    feed.consume = _consume
    feed.raise_ltp_snapshot_error = True
    ticker.on_connect = _on_connect
    ticker.on_ticks = _on_ticks

    # Regression: callback exceptions must be swallowed (no scanner crash).
    ticker.connect(True)

    assert feed.get_ltp_calls == 1
    assert seen_ticks == []


def test_index_callback_emits_nifty_tick_from_inline_value():
    feed = _FakeFeed()
    ticker = _GrowwFeedKiteTicker(feed=feed)

    seen_ticks: List[Dict[str, Any]] = []

    def _on_connect(ws, _response):
        ws.subscribe([NIFTY50_TOKEN])

    def _on_ticks(_ws, ticks):
        seen_ticks.extend(ticks)

    def _consume():
        assert feed.index_cb is not None
        feed.index_cb(
            {
                "exchange": "NSE",
                "segment": "CASH",
                "feed_key": "NIFTY",
                "value": 24450.7,
            }
        )

    feed.consume = _consume
    ticker.on_connect = _on_connect
    ticker.on_ticks = _on_ticks

    ticker.connect(True)

    assert feed.get_index_value_calls == 0
    assert len(seen_ticks) == 1
    assert seen_ticks[0]["instrument_token"] == NIFTY50_TOKEN
    assert seen_ticks[0]["last_price"] == 24450.7
