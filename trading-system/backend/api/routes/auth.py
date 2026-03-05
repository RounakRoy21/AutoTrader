"""
Kite Connect OAuth callback route.
Captures the request_token after user login and exchanges it for an access_token.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query
from kiteconnect import KiteConnect

from core.config import get_settings
from core.redis_client import get_value, set_value
from core.redis_keys import KITE_TOKEN_KEY
from integrations.kite_client import get_kite_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["Auth"])

KITE_TOKEN_TTL = 24 * 60 * 60  # 24 hours


@router.get("/kite/status")
async def kite_token_status():
    """Report whether a valid Kite access token is present in Redis."""
    token = await get_value(KITE_TOKEN_KEY)
    has_token = bool(token)
    return {
        "success": True,
        "data": {
            "authenticated": has_token,
            "message": "Token present" if has_token else "No token — please visit /api/auth/kite/login-url",
        },
        "error": None,
    }


@router.get("/kite/login-url")
async def get_kite_login_url():
    """Return the Kite login URL for the user to authenticate."""
    settings = get_settings()
    kite = KiteConnect(api_key=settings.kite_api_key)
    url = kite.login_url()
    return {"success": True, "data": {"login_url": url}, "error": None}


@router.get("/kite/callback")
async def kite_callback(request_token: str = Query(...)):
    """
    OAuth callback for Zerodha Kite.
    Exchanges the request_token for an access_token and stores it in Redis.
    """
    settings = get_settings()
    try:
        kite = KiteConnect(api_key=settings.kite_api_key)
        # generate_session() is a blocking HTTP call — run off the event loop
        data = await asyncio.to_thread(
            kite.generate_session,
            request_token,
            api_secret=settings.kite_api_secret,
        )
        access_token = data["access_token"]

        await set_value(KITE_TOKEN_KEY, access_token, ttl=KITE_TOKEN_TTL)
        # Invalidate the in-memory cache so kite_client re-reads the new token
        get_kite_client().invalidate_token()
        logger.info("Kite access_token stored in Redis (TTL=%ds)", KITE_TOKEN_TTL)

        return {
            "success": True,
            "data": {"message": "Kite authentication successful. You may close this window."},
            "error": None,
        }
    except Exception as exc:
        logger.error("Kite token exchange failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Kite auth failed: {exc}")

