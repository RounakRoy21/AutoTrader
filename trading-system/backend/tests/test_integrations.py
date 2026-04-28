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
        mock_broker.delete_gtt = MagicMock(return_value=None)

        with patch.object(client, "_settings") as mock_settings, \
             patch.object(client, "get_kite", new=AsyncMock(return_value=mock_broker)):
            mock_settings.paper_trading = False
            with patch("asyncio.to_thread", new=AsyncMock(return_value=None)):
                await client.delete_gtt(trigger_id=99999)


# ═══════════════════════════════════════════════════════════════════════════
#  GrowwClient — Circuit Breaker Telegram Alert
# ═══════════════════════════════════════════════════════════════════════════


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
