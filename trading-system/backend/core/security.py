"""
API security helpers: admin API-key enforcement, live-mode boot guard and
secret redaction for logs / error messages (audit R1).

Auth model
──────────
* Every mutating / money-spending route (trade close, broker login/logout,
  market-brief run, halt/resume, trading start/stop, on-demand checks) and the
  ``/ws/live`` WebSocket (first-message auth, see api/websocket.py) check the
  key via :func:`require_api_key` / :func:`api_key_matches`.
* Read-only GET dashboard routes stay open; they are expected to be reachable
  on localhost / behind nginx only (see docker-compose.prod.yml).
* An EMPTY ``ADMIN_API_KEY`` disables the check — allowed only in paper mode.
  The backend refuses to boot with ``PAPER_TRADING=false`` and an empty or
  placeholder key (:func:`enforce_live_mode_api_key`), and the dependency
  itself fails closed if it ever sees live mode without a key.
* Keys are compared with :func:`secrets.compare_digest` (constant time).
"""

from __future__ import annotations

import logging
import secrets
from typing import Iterable, Optional

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from core.config import Settings, get_settings

logger = logging.getLogger(__name__)

API_KEY_HEADER_NAME = "X-Api-Key"

# Minimum length for a live-mode admin key.  ``secrets.token_urlsafe(32)``
# produces 43 characters; 16 is a deliberately low floor that still rejects
# trivial values like "admin" or "1234".
MIN_LIVE_API_KEY_LENGTH = 16

# Values that appear in docs / .env.example / tutorials and must never be
# accepted as a real live-mode key.  Compared case-insensitively.
_PLACEHOLDER_KEYS = frozenset({
    "change_me",
    "changeme",
    "change-me",
    "change_me_admin_api_key",
    "changeme_admin_api_key",
    "your_admin_api_key",
    "your-admin-api-key",
    "your_api_key",
    "admin",
    "admin_api_key",
    "password",
    "secret",
    "test",
    "dev",
    "xxx",
})

_api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)


# ── Key validation ──────────────────────────────────────────────────────────────

def is_placeholder_key(key: Optional[str]) -> bool:
    """True when *key* is empty, a known placeholder, or obviously weak."""
    if not key or not key.strip():
        return True
    normalised = key.strip().lower()
    if normalised in _PLACEHOLDER_KEYS or "change_me" in normalised or "changeme" in normalised:
        return True
    return len(key.strip()) < MIN_LIVE_API_KEY_LENGTH


def enforce_live_mode_api_key(settings: Optional[Settings] = None) -> None:
    """Refuse to start in live mode without a real admin API key.

    Raises ``RuntimeError`` (which aborts the FastAPI lifespan, so uvicorn
    exits) when ``paper_trading`` is False and ``admin_api_key`` is empty or a
    known placeholder.  Paper mode only logs a warning when the key is empty.
    """
    settings = settings or get_settings()
    key = settings.admin_api_key
    if settings.paper_trading:
        if not key:
            logger.warning(
                "ADMIN_API_KEY is empty — control endpoints and /ws/live are OPEN "
                "(acceptable for local paper trading only)"
            )
        return
    if is_placeholder_key(key):
        logger.critical(
            "REFUSING TO START IN LIVE MODE: ADMIN_API_KEY is empty or a placeholder. "
            "Anyone who can reach the API could close positions or log the broker out. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\" "
            "and set ADMIN_API_KEY in .env (and the dashboard's adminApiKey)."
        )
        raise RuntimeError(
            "Live mode (PAPER_TRADING=false) requires a strong ADMIN_API_KEY "
            f"(>= {MIN_LIVE_API_KEY_LENGTH} chars, not a placeholder)"
        )


def api_key_matches(provided: Optional[str], settings: Optional[Settings] = None) -> bool:
    """Constant-time check of *provided* against the configured admin key.

    Returns True when no key is configured AND the system is in paper mode
    (development open access).  Fails closed in live mode with no key.
    """
    settings = settings or get_settings()
    expected = settings.admin_api_key or ""
    if not expected:
        return bool(settings.paper_trading)
    if not provided:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


async def require_api_key(api_key: Optional[str] = Security(_api_key_header)) -> None:
    """FastAPI dependency enforcing ``X-Api-Key`` on mutating / control routes."""
    if not api_key_matches(api_key):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Api-Key")


# ── Secret redaction ───────────────────────────────────────────────────────────

_REDACTED = "***REDACTED***"


def _secret_values(settings: Settings) -> Iterable[str]:
    for value in (
        settings.admin_api_key,
        settings.groww_totp_secret,
        settings.groww_password,
        settings.groww_client_id,
        settings.anthropic_api_key,
        settings.alpha_vantage_api_key,
        settings.telegram_bot_token,
        settings.redis_password,
        settings.postgres_password,
    ):
        # Very short values (e.g. "1") would redact random substrings.
        if value and len(value) >= 6:
            yield value


def redact_secrets(text: object, extra: Iterable[Optional[str]] = ()) -> str:
    """Return ``str(text)`` with every configured secret value masked.

    *extra* lets callers mask runtime secrets (e.g. a freshly minted broker
    access token) that are not part of Settings.
    """
    out = str(text)
    try:
        values = list(_secret_values(get_settings()))
    except Exception:  # settings unavailable — still mask the extras
        values = []
    values.extend(v for v in extra if v and len(v) >= 6)
    # Longest first so a secret containing another secret is fully masked.
    for value in sorted(set(values), key=len, reverse=True):
        if value in out:
            out = out.replace(value, _REDACTED)
    return out


class SecretRedactingFilter(logging.Filter):
    """Logging filter that masks configured secret values in every record.

    Installed on the root handler in main.py as a catch-all, so an exception
    message that embeds a URL with a bot token (Telegram), a TOTP secret, or
    an API key never reaches stdout / container logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact_secrets(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True
