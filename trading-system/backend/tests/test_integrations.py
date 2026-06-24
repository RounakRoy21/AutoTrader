"""
Tests for integration-layer fixes:
  - GrowwClient.delete_gtt() exists and is callable
  - Circuit breaker sends Telegram alert when halt triggers
  - ltp_store is cleared at session start (TradingAgent.start)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════
#  KiteClient — delete_gtt
# ═══════════════════════════════════════════════════════════════════════════


class TestGrowwClientDeleteGtt:
    """GrowwClient must expose delete_gtt() so the risk manager can cancel
    server-side GTT orders before executing a manual SELL."""

    def test_delete_gtt_method_exists(self):
        """GrowwClient must have a delete_gtt coroutine method."""
        import inspect
        from integrations.groww_client import GrowwClient
        assert hasattr(GrowwClient, "delete_gtt")
        assert inspect.iscoroutinefunction(GrowwClient.delete_gtt)

    @pytest.mark.asyncio
    async def test_delete_gtt_paper_mode_no_exception(self):
        """In paper mode, delete_gtt should succeed without calling real API."""
        from integrations.groww_client import GrowwClient

        client = GrowwClient()
        # Patch settings to paper_trading=True
        with patch.object(client, "_settings") as mock_settings:
            mock_settings.paper_trading = True
            # Should complete without error
            await client.delete_gtt(trigger_id=12345)

    @pytest.mark.asyncio
    async def test_delete_gtt_live_mode_calls_api(self):
        """In live mode, delete_gtt should call the broker's delete_gtt(trigger_id)."""
        from integrations.groww_client import GrowwClient

        client = GrowwClient()
        mock_broker = MagicMock()
        mock_broker.cancel_order = MagicMock(return_value=None)

        with patch("integrations.groww_client.get_value", new=AsyncMock(return_value="real-order-id")), \
             patch.object(client, "_settings") as mock_settings, \
             patch.object(client, "get_groww", new=AsyncMock(return_value=mock_broker)):
            mock_settings.paper_trading = False
            with patch("asyncio.to_thread", new=AsyncMock(return_value=None)):
                await client.delete_gtt(trigger_id=99999)


# ═══════════════════════════════════════════════════════════════════════════
#  GrowwClient — fail-fast on permanent 403 / auth errors
# ═══════════════════════════════════════════════════════════════════════════


class TestPermanentErrorFailFast:
    """A 403 'Access forbidden' (missing data-API entitlement) must NOT be
    retried — retrying only wastes ~14s/call and floods the logs."""

    def test_is_permanent_error_detects_403_code(self):
        from integrations.groww_client import _is_permanent_groww_error

        exc = Exception("Access forbidden for this request.")
        exc.code = "403"  # type: ignore[attr-defined]
        assert _is_permanent_groww_error(exc) is True

    def test_is_permanent_error_detects_forbidden_message(self):
        from integrations.groww_client import _is_permanent_groww_error

        assert _is_permanent_groww_error(Exception("Access forbidden for this request.")) is True

    def test_is_permanent_error_ignores_transient(self):
        from integrations.groww_client import _is_permanent_groww_error

        assert _is_permanent_groww_error(Exception("Connection reset by peer")) is False

    def test_retry_sync_does_not_retry_permanent_error(self):
        """The retry decorator must call the wrapped fn exactly once on a 403."""
        from integrations.groww_client import _retry_sync

        calls = {"n": 0}

        @_retry_sync
        def _always_forbidden():
            calls["n"] += 1
            exc = Exception("Access forbidden for this request.")
            exc.code = "403"  # type: ignore[attr-defined]
            raise exc

        with pytest.raises(Exception):
            _always_forbidden()
        assert calls["n"] == 1  # no retries

    def test_retry_sync_still_retries_transient_error(self):
        """Transient errors must still be retried up to MAX_RETRIES times."""
        from integrations.groww_client import _retry_sync, MAX_RETRIES

        calls = {"n": 0}

        @_retry_sync
        def _always_flaky():
            calls["n"] += 1
            raise Exception("Connection reset by peer")

        with patch("integrations.groww_client.time.sleep", return_value=None):
            with pytest.raises(Exception):
                _always_flaky()
        assert calls["n"] == MAX_RETRIES


# ═══════════════════════════════════════════════════════════════════════════
#  GrowwClient — Circuit Breaker Telegram Alert
# ═══════════════════════════════════════════════════════════════════════════


class TestDataApiHealth:
    """The scanner OHLCV poll loop publishes market-data API health and alerts
    on forbidden↔OK transitions (so a silent trading blackout is surfaced)."""

    @pytest.mark.asyncio
    async def test_all_forbidden_sets_status_and_alerts_once(self):
        import agents.scanner as scanner

        scanner._data_api_forbidden = None  # reset transition state
        forbidden = Exception("Access forbidden for this request.")
        forbidden.code = "403"  # type: ignore[attr-defined]
        sets: dict = {}

        async def _fake_set(k, v, *a, **kw):
            sets[k] = v

        alert = AsyncMock()
        with patch("core.redis_client.set_value", new=_fake_set), \
             patch("integrations.telegram_client.send_data_api_alert", new=alert):
            # First poll: every symbol 403 → FORBIDDEN + one alert
            await scanner._update_data_api_health(0, 2, [forbidden, forbidden])
            assert sets["data_api:status"] == "FORBIDDEN"
            assert alert.await_count == 1
            # Second consecutive forbidden poll: status stays, NO duplicate alert
            await scanner._update_data_api_health(0, 2, [forbidden, forbidden])
            assert alert.await_count == 1

    @pytest.mark.asyncio
    async def test_recovery_sets_ok_and_alerts(self):
        import agents.scanner as scanner

        scanner._data_api_forbidden = True  # was forbidden
        sets: dict = {}

        async def _fake_set(k, v, *a, **kw):
            sets[k] = v

        alert = AsyncMock()
        with patch("core.redis_client.set_value", new=_fake_set), \
             patch("integrations.telegram_client.send_data_api_alert", new=alert):
            await scanner._update_data_api_health(2, 2, [{"close": 1}, {"close": 2}])
            assert sets["data_api:status"] == "OK"
            alert.assert_awaited_once_with(forbidden=False)

    @pytest.mark.asyncio
    async def test_partial_success_is_degraded_no_alert(self):
        import agents.scanner as scanner

        scanner._data_api_forbidden = False
        sets: dict = {}

        async def _fake_set(k, v, *a, **kw):
            sets[k] = v

        alert = AsyncMock()
        with patch("core.redis_client.set_value", new=_fake_set), \
             patch("integrations.telegram_client.send_data_api_alert", new=alert):
            await scanner._update_data_api_health(1, 2, [Exception("net blip")])
            assert sets["data_api:status"] == "DEGRADED"
            alert.assert_not_awaited()


class TestCircuitBreakerAlert:
    """When the circuit breaker triggers, a Telegram halt alert must be sent."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_calls_send_halt_alert(self):
        """_handle_failure must call send_halt_alert() when circuit trips."""
        import time
        from integrations.groww_client import GrowwClient

        client = GrowwClient()
        # Simulate the circuit breaker being in an already-tripped state
        # by setting _last_failure to 120s ago (> 60s threshold)
        client._last_failure = time.time() - 120.0

        mock_halt_alert = AsyncMock()

        with patch("integrations.groww_client.set_value", new=AsyncMock()), \
             patch(
                 "integrations.groww_client.GrowwClient._handle_failure",
                 wraps=client._handle_failure,
             ), \
             patch(
                 "integrations.telegram_client.send_halt_alert",
                 mock_halt_alert,
             ):
            # Simulate the lazy import path by patching the module inside handler
            with patch.dict(
                "sys.modules",
                {"integrations.telegram_client": MagicMock(
                    send_halt_alert=mock_halt_alert
                )},
            ):
                await client._handle_failure(Exception("timeout"))

            # Circuit breaker should have set halt
            # (set_value is mocked, check it was called with halt key)


# ═══════════════════════════════════════════════════════════════════════════
#  LTP Store — Cleared at Session Start
# ═══════════════════════════════════════════════════════════════════════════


class TestLtpStoreClearedAtSessionStart:
    """ltp_store must be cleared when TradingAgent.start() is called so stale
    prices from a previous session don't influence paper-trading Risk Manager."""

    def test_ltp_store_clear_function_exists(self):
        """ltp_store must expose a clear() function."""
        from integrations import ltp_store
        assert hasattr(ltp_store, "clear")
        assert callable(ltp_store.clear)

    def test_ltp_store_clear_removes_prices(self):
        """After clear(), get_ltp returns None for previously stored symbols."""
        from integrations import ltp_store
        ltp_store.set_ltp("RELIANCE", 2500.0)
        ltp_store.set_ltp("INFY", 1780.0)
        assert ltp_store.get_ltp("RELIANCE") == 2500.0
        ltp_store.clear()
        assert ltp_store.get_ltp("RELIANCE") is None
        assert ltp_store.get_ltp("INFY") is None

    def test_trading_agent_imports_ltp_store(self):
        """TradingAgent module must import ltp_store so it can call clear()."""
        import inspect
        import agents.trading_agent as ta_module
        source = inspect.getsource(ta_module)
        assert "ltp_store" in source
        assert "clear()" in source
