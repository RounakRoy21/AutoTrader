"""
Regression tests for the API security surface (audit R1 / R2, cluster D):

* every mutating route requires the admin API key (constant-time compare)
* /ws/live requires the key (first-message auth or X-Api-Key header)
* live mode refuses to boot with an empty / placeholder key
* secrets are redacted from logs and error messages
* Redis AUTH password is passed to the client
"""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import core.security as security
from core.config import Settings
from main import app

GOOD_KEY = "k" * 20 + "-Strong_Admin_Key"


def _settings(**overrides) -> Settings:
    base = dict(paper_trading=True, admin_api_key="", telegram_bot_token="")
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def keyed_settings():
    """Admin key configured (paper mode) — the normal hardened setup."""
    s = _settings(admin_api_key=GOOD_KEY)
    with patch.object(security, "get_settings", return_value=s):
        yield s


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Live-mode boot guard ──────────────────────────────────────────────────────


class TestLiveModeBootGuard:
    @pytest.mark.parametrize(
        "key",
        ["", "   ", "changeme", "CHANGE_ME", "your_admin_api_key", "admin", "short-key-123",
         "changeme_please_set_a_real_value"],
    )
    def test_live_mode_refuses_empty_or_placeholder_key(self, key, caplog):
        s = _settings(paper_trading=False, admin_api_key=key)
        with caplog.at_level(logging.CRITICAL, logger="core.security"):
            with pytest.raises(RuntimeError, match="ADMIN_API_KEY"):
                security.enforce_live_mode_api_key(s)
        assert any("REFUSING TO START IN LIVE MODE" in r.getMessage() for r in caplog.records)

    def test_live_mode_accepts_strong_key(self):
        security.enforce_live_mode_api_key(_settings(paper_trading=False, admin_api_key=GOOD_KEY))

    def test_paper_mode_allows_empty_key_with_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="core.security"):
            security.enforce_live_mode_api_key(_settings(paper_trading=True, admin_api_key=""))
        assert any("OPEN" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_lifespan_aborts_before_any_startup_work_in_live_mode_without_key(self):
        import main
        live = _settings(paper_trading=False, admin_api_key="")
        with patch.object(main, "settings", live), \
             patch.object(main, "check_db_health", new=AsyncMock(return_value=True)) as db_check:
            with pytest.raises(RuntimeError):
                async with main.lifespan(main.app):
                    pass  # pragma: no cover — must not get here
        db_check.assert_not_awaited()

    def test_env_example_documents_redis_password_and_live_key_requirement(self):
        from pathlib import Path
        env_example = Path(__file__).resolve().parents[2] / ".env.example"
        lines = env_example.read_text(encoding="utf-8").splitlines()
        values = dict(l.split("=", 1) for l in lines if l and not l.startswith("#") and "=" in l)
        assert "REDIS_PASSWORD" in values
        assert "REQUIRED FOR LIVE TRADING" in env_example.read_text(encoding="utf-8")
        # The template's placeholder must itself be rejected by the live boot guard.
        assert security.is_placeholder_key(values["ADMIN_API_KEY"].split("#")[0].strip())
        assert values["PAPER_TRADING"].split("#")[0].strip().lower() == "true"


# ── Key comparison ────────────────────────────────────────────────────────────


class TestApiKeyMatches:
    def test_paper_mode_without_key_is_open(self):
        assert security.api_key_matches(None, _settings(admin_api_key=""))

    def test_live_mode_without_key_fails_closed(self):
        # Defence in depth: even if the boot guard were bypassed.
        assert not security.api_key_matches(None, _settings(paper_trading=False, admin_api_key=""))
        assert not security.api_key_matches("x", _settings(paper_trading=False, admin_api_key=""))

    def test_wrong_and_missing_key_rejected(self):
        s = _settings(admin_api_key=GOOD_KEY)
        assert not security.api_key_matches(None, s)
        assert not security.api_key_matches("", s)
        assert not security.api_key_matches(GOOD_KEY[:-1], s)
        assert security.api_key_matches(GOOD_KEY, s)

    def test_uses_constant_time_compare(self):
        s = _settings(admin_api_key=GOOD_KEY)
        with patch.object(security.secrets, "compare_digest", wraps=security.secrets.compare_digest) as cd:
            assert security.api_key_matches(GOOD_KEY, s)
        cd.assert_called_once()


# ── HTTP route protection ─────────────────────────────────────────────────────


def _route_requires_key(route: APIRoute) -> bool:
    return any(d.call is security.require_api_key for d in route.dependant.dependencies)


def test_every_mutating_route_requires_api_key():
    """Guard against a new POST/PUT/PATCH/DELETE route shipping without auth."""
    unprotected = [
        f"{sorted(r.methods)} {r.path}"
        for r in app.routes
        if isinstance(r, APIRoute)
        and (r.methods - {"GET", "HEAD", "OPTIONS"})
        and not _route_requires_key(r)
    ]
    assert unprotected == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/trades/1/close",
        "/api/auth/groww/login",
        "/api/auth/groww/logout",
        "/api/market-brief/run",
        "/api/trading/halt",
        "/api/trading/resume",
        "/api/agent/trading/start",
        "/api/agent/trading/stop",
        "/api/system/nifty50/check",
        "/api/news/health/check",
    ],
)
@pytest.mark.asyncio
async def test_mutating_routes_reject_missing_or_wrong_key(client, keyed_settings, path):
    resp = await client.post(path, json={})
    assert resp.status_code == 403
    resp = await client.post(path, json={}, headers={"X-Api-Key": "wrong-key-wrong-key"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_market_brief_run_allowed_with_key(client, keyed_settings):
    with patch("api.routes.market_brief.run_research_agent", new=AsyncMock()) as run:
        resp = await client.post("/api/market-brief/run", headers={"X-Api-Key": GOOD_KEY})
    assert resp.status_code == 202
    run.assert_awaited_once()


@pytest.mark.asyncio
async def test_groww_logout_allowed_with_key(client, keyed_settings):
    gc = MagicMock()
    with patch("api.routes.auth.delete_value", new=AsyncMock()) as delete, \
         patch("api.routes.auth.get_groww_client", return_value=gc):
        resp = await client.post("/api/auth/groww/logout", headers={"X-Api-Key": GOOD_KEY})
    assert resp.status_code == 200
    delete.assert_awaited_once()
    gc.invalidate_token.assert_called_once()


@pytest.mark.asyncio
async def test_manual_close_without_key_never_touches_db_or_broker(client, keyed_settings):
    with patch("api.routes.trades.get_groww_client") as gc:
        resp = await client.post("/api/trades/1/close")
    assert resp.status_code == 403
    gc.assert_not_called()


@pytest.mark.asyncio
async def test_paper_mode_without_key_keeps_open_access(client):
    """Preserve dev/paper behaviour: no key configured → control routes open."""
    with patch.object(security, "get_settings", return_value=_settings(admin_api_key="")), \
         patch("api.routes.market_brief.run_research_agent", new=AsyncMock()):
        resp = await client.post("/api/market-brief/run")
    assert resp.status_code == 202


# ── WebSocket ─────────────────────────────────────────────────────────────────


@pytest.fixture
def ws_client():
    with patch("api.websocket.start_ws_background_tasks"), \
         patch("api.websocket.ltp_store.get_all", return_value={"INFY": 1500.0}):
        yield TestClient(app)  # no context manager → lifespan does not run


def _auth_msg(key: str) -> str:
    return json.dumps({"channel": "auth", "data": {"api_key": key}})


class TestWebSocketAuth:
    def test_rejects_wrong_key(self, ws_client, keyed_settings):
        with ws_client.websocket_connect("/ws/live") as ws:
            ws.send_text(_auth_msg("not-the-key-at-all"))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == 1008

    def test_rejects_non_auth_first_message(self, ws_client, keyed_settings):
        with ws_client.websocket_connect("/ws/live") as ws:
            ws.send_text(json.dumps({"channel": "ping", "data": {}}))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
        assert exc.value.code == 1008

    def test_rejects_silent_client_after_timeout(self, ws_client, keyed_settings):
        with patch("api.websocket.WS_AUTH_TIMEOUT_S", 0.05):
            with ws_client.websocket_connect("/ws/live") as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
        assert exc.value.code == 1008

    def test_rejects_key_in_query_string(self, ws_client, keyed_settings):
        """Query-string keys leak into access logs — deliberately unsupported."""
        with patch("api.websocket.WS_AUTH_TIMEOUT_S", 0.05):
            with ws_client.websocket_connect(f"/ws/live?api_key={GOOD_KEY}") as ws:
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
        assert exc.value.code == 1008

    def test_accepts_first_message_auth(self, ws_client, keyed_settings):
        with ws_client.websocket_connect("/ws/live") as ws:
            ws.send_text(_auth_msg(GOOD_KEY))
            msg = json.loads(ws.receive_text())
        assert msg["channel"] == "ltp_update"
        assert msg["data"] == {"INFY": 1500.0}

    def test_accepts_header_auth(self, ws_client, keyed_settings):
        with ws_client.websocket_connect("/ws/live", headers={"X-Api-Key": GOOD_KEY}) as ws:
            msg = json.loads(ws.receive_text())
        assert msg["channel"] == "ltp_update"

    def test_paper_mode_without_key_is_open(self, ws_client):
        with patch.object(security, "get_settings", return_value=_settings(admin_api_key="")):
            with ws_client.websocket_connect("/ws/live") as ws:
                msg = json.loads(ws.receive_text())
        assert msg["channel"] == "ltp_update"


# ── Secret redaction ──────────────────────────────────────────────────────────


BOT_TOKEN = "123456789:AAHsecretTelegramBotTokenValue"
TOTP_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"


@pytest.fixture
def secret_settings():
    s = _settings(
        admin_api_key=GOOD_KEY,
        telegram_bot_token=BOT_TOKEN,
        telegram_chat_id="42",
        groww_totp_secret=TOTP_SECRET,
        anthropic_api_key="sk-ant-api03-supersecretvalue",
    )
    with patch.object(security, "get_settings", return_value=s):
        yield s


class TestRedaction:
    def test_redact_secrets_masks_configured_values(self, secret_settings):
        text = (
            f"POST https://api.telegram.org/bot{BOT_TOKEN}/sendMessage failed; "
            f"totp={TOTP_SECRET} key={GOOD_KEY} anth=sk-ant-api03-supersecretvalue"
        )
        out = security.redact_secrets(text)
        for secret in (BOT_TOKEN, TOTP_SECRET, GOOD_KEY, "sk-ant-api03-supersecretvalue"):
            assert secret not in out
        assert "***REDACTED***" in out

    def test_redact_secrets_masks_extra_runtime_values(self, secret_settings):
        out = security.redact_secrets("bad token abcdef123456", extra=("abcdef123456", None))
        assert "abcdef123456" not in out

    def test_logging_filter_masks_args(self, secret_settings):
        record = logging.LogRecord(
            "x", logging.ERROR, __file__, 1, "auth failed: %s", (f"secret={TOTP_SECRET}",), None
        )
        assert security.SecretRedactingFilter().filter(record) is True
        assert TOTP_SECRET not in record.getMessage()

    def test_root_handler_has_redacting_filter(self):
        import main  # noqa: F401 — installs the filter at import
        handlers = logging.getLogger().handlers
        assert any(
            isinstance(f, security.SecretRedactingFilter) for h in handlers for f in h.filters
        )
        assert logging.getLogger("httpx").level >= logging.WARNING

    @pytest.mark.asyncio
    async def test_telegram_failure_log_hides_bot_token(self, secret_settings, caplog):
        from integrations import telegram_client
        http = MagicMock()
        http.post = AsyncMock(side_effect=RuntimeError(
            f"Client error '401 Unauthorized' for url 'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'"
        ))
        with patch.object(telegram_client, "get_settings", return_value=secret_settings), \
             patch.object(telegram_client, "_get_http_client", return_value=http), \
             caplog.at_level(logging.ERROR, logger="integrations.telegram_client"):
            ok = await telegram_client.send_telegram("hi")
        assert ok is False
        assert caplog.records
        assert all(BOT_TOKEN not in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_groww_login_error_hides_totp_secret(self, client, secret_settings):
        import api.routes.auth as auth_routes
        s = secret_settings.model_copy(update={"groww_client_id": "groww-client-id-123"})

        def _boom(**kwargs):
            raise RuntimeError(f"login rejected for {kwargs['api_key']} totp={kwargs['totp']}")

        fake_groww = MagicMock()
        fake_groww.GrowwAPI.get_access_token.side_effect = _boom
        with patch.object(auth_routes, "get_settings", return_value=s), \
             patch.object(security, "get_settings", return_value=s), \
             patch.dict("sys.modules", {"growwapi": fake_groww}):
            resp = await client.post(
                "/api/auth/groww/login",
                json={"totp": "123456"},
                headers={"X-Api-Key": GOOD_KEY},
            )
        assert resp.status_code == 500
        body = resp.text
        assert "groww-client-id-123" not in body
        assert "123456" not in body


# ── Redis AUTH ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_redis_password_passed_to_client():
    import core.redis_client as rc

    fake = MagicMock()
    fake.ping = AsyncMock(return_value=True)
    s = _settings(redis_password="r3dis-pass", redis_url="redis://redis:6379/0")
    with patch.object(rc, "_redis", None), \
         patch.object(rc, "get_settings", return_value=s), \
         patch.object(rc, "flush_fallback_buffer", new=AsyncMock()), \
         patch.object(rc.aioredis, "from_url", return_value=fake) as from_url:
        await rc.get_redis()
    assert from_url.call_args.kwargs["password"] == "r3dis-pass"


@pytest.mark.asyncio
async def test_redis_no_password_by_default():
    import core.redis_client as rc

    fake = MagicMock()
    fake.ping = AsyncMock(return_value=True)
    with patch.object(rc, "_redis", None), \
         patch.object(rc, "get_settings", return_value=_settings()), \
         patch.object(rc, "flush_fallback_buffer", new=AsyncMock()), \
         patch.object(rc.aioredis, "from_url", return_value=fake) as from_url:
        await rc.get_redis()
    assert from_url.call_args.kwargs["password"] is None
