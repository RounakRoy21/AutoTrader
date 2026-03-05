"""
Token refresh job — runs at 08:50 IST every trading day.

Checks whether a valid Kite access token is present in Redis.
If absent (expired overnight), sends a Telegram alert with the login URL
so the operator can authenticate before market open at 09:15.

In paper trading mode the check is skipped (no token required).
"""

from __future__ import annotations

import logging

from kiteconnect import KiteConnect

from core.config import get_settings
from core.redis_client import get_value
from core.redis_keys import KITE_TOKEN_KEY
from integrations.telegram_client import send_telegram

logger = logging.getLogger(__name__)

KITE_TOKEN_KEY = "kite_access_token"


async def check_kite_token() -> None:
    """
    Pre-market token health check.
    Sends a Telegram alert if no valid token is found.
    """
    settings = get_settings()

    if settings.paper_trading:
        logger.info("[TokenCheck] Paper trading mode — skipping Kite token check")
        return

    token = await get_value(KITE_TOKEN_KEY)
    if token:
        logger.info("[TokenCheck] Kite access token is present ✅")
        return

    # Token missing — build login URL and alert operator
    try:
        kite = KiteConnect(api_key=settings.kite_api_key)
        login_url = kite.login_url()
    except Exception as exc:
        login_url = f"http://localhost:8000/api/auth/kite/login-url (error: {exc})"

    message = (
        "⚠️ <b>AutoTrader — Action Required</b>\n\n"
        "Kite access token has expired or is missing.\n"
        "Please authenticate before market opens at 09:15 IST.\n\n"
        f"🔗 <a href=\"{login_url}\">Click here to login</a>"
    )
    logger.warning("[TokenCheck] No Kite token found — sending Telegram alert")
    await send_telegram(message)
