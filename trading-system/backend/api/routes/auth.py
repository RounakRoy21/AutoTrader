"""
Groww API authentication routes.

Groww uses TOTP-based login (no OAuth redirect, no daily re-auth).
The session token returned after login persists until explicitly revoked
via the Groww app — no TTL is set in Redis.

Endpoints:
  GET  /api/auth/groww/status   — check if a session token is present
  POST /api/auth/groww/login    — perform TOTP login, store token in Redis
  POST /api/auth/groww/logout   — remove token from Redis and invalidate cache
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.config import get_settings
from core.redis_client import delete_value, get_value, set_value
from core.redis_keys import GROWW_TOKEN_KEY
from integrations.groww_client import get_groww_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["Auth"])


class LoginRequest(BaseModel):
    """Body for POST /api/auth/groww/login."""
    client_id: str
    password: str
    totp: Optional[str] = None  # 6-digit TOTP; auto-generated if GROWW_TOTP_SECRET is set


@router.get("/groww/status")
async def groww_token_status():
    """Report whether a valid Groww session token is present in Redis."""
    token = await get_value(GROWW_TOKEN_KEY)
    has_token = bool(token)
    return {
        "success": True,
        "data": {
            "authenticated": has_token,
            "message": (
                "Session token present"
                if has_token
                else "No token — POST /api/auth/groww/login to authenticate"
            ),
        },
        "error": None,
    }


@router.post("/groww/login")
async def groww_login(body: LoginRequest):
    """
    Perform TOTP-based Groww login and store the session token in Redis.

    If ``totp`` is omitted in the request body, the server generates it
    automatically from GROWW_TOTP_SECRET (if configured in .env).
    Tokens have no expiry — no TTL is applied.
    """
    settings = get_settings()

    # Resolve TOTP
    totp_code = body.totp
    if not totp_code:
        if not settings.groww_totp_secret:
            raise HTTPException(
                status_code=400,
                detail="TOTP code required — either provide it in the request body "
                       "or set GROWW_TOTP_SECRET in .env for auto-generation.",
            )
        try:
            import pyotp
            totp_code = pyotp.TOTP(settings.groww_totp_secret).now()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"TOTP generation failed: {exc}")

    try:
        from growwapi import GrowwAPI

        def _do_login():
            gc = GrowwAPI()
            gc.login(
                client_id=body.client_id or settings.groww_client_id,
                password=body.password or settings.groww_password,
                totp=totp_code,
            )
            return gc.access_token

        access_token = await asyncio.to_thread(_do_login)
        if not access_token:
            raise ValueError("Login succeeded but no access_token was returned")

        # No TTL — Groww session tokens do not expire on their own
        await set_value(GROWW_TOKEN_KEY, access_token)
        # Invalidate the in-memory cache so groww_client picks up the new token
        get_groww_client().invalidate_token()
        logger.info("Groww access_token stored in Redis (no TTL)")

        return {
            "success": True,
            "data": {"message": "Groww authentication successful."},
            "error": None,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Groww login failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Groww login failed: {exc}")


@router.post("/groww/logout")
async def groww_logout():
    """Remove the Groww session token from Redis and invalidate the in-memory cache."""
    await delete_value(GROWW_TOKEN_KEY)
    get_groww_client().invalidate_token()
    logger.info("Groww session token removed from Redis")
    return {
        "success": True,
        "data": {"message": "Logged out. POST /api/auth/groww/login to re-authenticate."},
        "error": None,
    }

