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

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from core.redis_client import get_redis
from integrations import ltp_store

logger = logging.getLogger(__name__)
router = APIRouter()

# Active WebSocket connections
_connections: Set[WebSocket] = set()

# Redis channels to forward to frontend
CHANNELS = ["trade_events", "eod_report", "market_brief", "system_alerts"]

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


@router.websocket("/ws/live")
async def websocket_live(ws: WebSocket):
    """
    WebSocket endpoint for the Angular frontend.
    Streams real-time events: TRADE_OPENED, TRADE_CLOSED, PNL_UPDATE,
    AGENT_STATUS, SYSTEM_ALERT, and ltp_update snapshots.
    """
    await ws.accept()
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

