"""
WebSocket endpoint /ws/live — broadcasts real-time events to connected Angular clients.
Subscribes to Redis channels and forwards events as typed JSON messages.
Also pushes live LTP snapshots from the in-memory ltp_store every 2 seconds.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from core.redis_client import get_redis
from core.security import API_KEY_HEADER_NAME, api_key_matches
from integrations import ltp_store

logger = logging.getLogger(__name__)
router = APIRouter()

# Active WebSocket connections
_connections: Set[WebSocket] = set()

# Redis channels to forward to frontend
CHANNELS = ["trade_events", "eod_report", "market_brief", "system_alerts", "decision_feed"]

# How often to push a fresh LTP snapshot (seconds)
LTP_BROADCAST_INTERVAL = 2


async def broadcast(message: dict) -> None:
    """Send a JSON message to all connected WebSocket clients."""
    dead: list[WebSocket] = []
    payload = json.dumps(message)
    for ws in list(_connections):  # snapshot — safe if set mutates during await
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _connections.discard(ws)


async def _redis_listener() -> None:
    """Background loop: subscribe to Redis channels and broadcast to WebSocket clients."""
    while True:
        try:
            r = await get_redis()
            pubsub = r.pubsub()
            await pubsub.subscribe(*CHANNELS)
            logger.info("WebSocket relay subscribed to Redis channels: %s", CHANNELS)
            async for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        data = json.loads(message["data"])
                    except json.JSONDecodeError:
                        data = {"raw": message["data"]}
                    await broadcast({
                        "channel": message["channel"],
                        "data": data,
                    })
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Redis listener error: %s — retrying in 5s", exc)
            await asyncio.sleep(5)


async def _ltp_broadcaster() -> None:
    """
    Background loop: read the in-memory LTP store every LTP_BROADCAST_INTERVAL seconds
    and push a snapshot to all connected clients.
    Only broadcasts when at least one client is connected and the store is non-empty.
    """
    while True:
        try:
            await asyncio.sleep(LTP_BROADCAST_INTERVAL)
            if not _connections:
                continue
            prices = ltp_store.get_all()
            if prices:
                await broadcast({"channel": "ltp_update", "data": prices})
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("LTP broadcaster error: %s", exc)


_listener_task: asyncio.Task | None = None
_ltp_task: asyncio.Task | None = None


def ensure_listener_started() -> None:
    """Ensure the background Redis → WebSocket relay task is running."""
    global _listener_task
    if _listener_task is None or _listener_task.done():
        _listener_task = asyncio.create_task(_redis_listener())


def ensure_ltp_broadcaster_started() -> None:
    """Ensure the LTP broadcaster task is running."""
    global _ltp_task
    if _ltp_task is None or _ltp_task.done():
        _ltp_task = asyncio.create_task(_ltp_broadcaster())


def start_ws_background_tasks() -> None:
    """Start both background tasks. Called from the FastAPI lifespan."""
    ensure_listener_started()
    ensure_ltp_broadcaster_started()
    logger.info("WebSocket background tasks started (Redis relay + LTP broadcaster)")


# Seconds a client has to send its auth message after the handshake.
WS_AUTH_TIMEOUT_S = 5.0


async def _authenticate(ws: WebSocket) -> bool:
    """Return True when the client presented a valid admin key.

    Accepts an ``X-Api-Key`` handshake header (also covers the open,
    no-key-configured paper mode) or a first message
    ``{"channel": "auth", "data": {"api_key": "..."}}``.
    """
    if api_key_matches(ws.headers.get(API_KEY_HEADER_NAME)):
        return True
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=WS_AUTH_TIMEOUT_S)
        msg = json.loads(raw)
    except Exception:
        return False
    if not isinstance(msg, dict) or msg.get("channel") != "auth":
        return False
    data = msg.get("data")
    provided = data.get("api_key") if isinstance(data, dict) else None
    return api_key_matches(provided if isinstance(provided, str) else None)


@router.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    """
    WebSocket endpoint for the Angular frontend.
    Streams real-time events: TRADE_OPENED, TRADE_CLOSED, PNL_UPDATE,
    AGENT_STATUS, SYSTEM_ALERT, and ltp_update snapshots.

    Auth (audit R1): the stream carries positions, P&L and LLM rationale, so the
    admin key is required.  Browsers cannot set headers on a WebSocket
    handshake and a query-string key would leak into uvicorn / nginx access
    logs, so the dashboard sends it as the FIRST message:
    ``{"channel": "auth", "data": {"api_key": "..."}}``.  Non-browser clients
    may instead send an ``X-Api-Key`` header on the handshake.  When no key is
    configured (paper mode only — live mode refuses to boot) no auth message is
    needed.  A failed / missing auth closes with 1008 (policy violation) before
    the client is added to the broadcast set.
    """
    await ws.accept()
    if not await _authenticate(ws):
        logger.warning(
            "WebSocket connection rejected: invalid or missing api_key (client=%s)",
            ws.client.host if ws.client else "?",
        )
        try:
            await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        except Exception:
            pass
        return
    _connections.add(ws)
    # Ensure background tasks are running (idempotent — safe to call every connect)
    start_ws_background_tasks()
    logger.info("WebSocket client connected (total=%d)", len(_connections))

    # Send an immediate LTP snapshot so the client has data before the 2s tick
    prices = ltp_store.get_all()
    if prices:
        try:
            await ws.send_text(json.dumps({"channel": "ltp_update", "data": prices}))
        except Exception:
            pass

    try:
        while True:
            # Keep the connection alive; client may send pings
            _ = await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _connections.discard(ws)
        logger.info("WebSocket client disconnected (total=%d)", len(_connections))

