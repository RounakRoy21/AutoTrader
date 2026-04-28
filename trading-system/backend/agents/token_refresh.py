"""
Token refresh — stub module (Groww TOTP tokens do not expire).

Groww session tokens are long-lived and persist until the user revokes them
via the Groww app or explicitly logs out via POST /api/auth/groww/logout.
There is no daily re-authentication requirement (unlike legacy Zerodha Kite OAuth).

This module is kept as a no-op stub to avoid breaking any external tooling
that may import from it. The APScheduler job that called check_groww_token
has been removed from main.py.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def check_groww_token() -> None:
    """No-op: Groww tokens do not expire. Nothing to check."""
    logger.debug("[TokenCheck] Groww TOTP tokens are persistent — no check required")
