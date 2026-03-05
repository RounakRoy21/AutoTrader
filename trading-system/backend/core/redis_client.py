"""
Redis connection singleton with Pub/Sub helpers for inter-agent communication.
Falls back to in-memory queues if Redis is unreachable (graceful degradation).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict, Optional

import redis.asyncio as aioredis

from core.config import get_settings

logger = logging.getLogger(__name__)

_redis: Optional[aioredis.Redis] = None
_pubsub_tasks: Dict[str, asyncio.Task] = {}

# ── In-memory fallback when Redis is down ──────────
_fallback_mode = False
_fallback_channels: Dict[str, list] = {}


async def get_redis() -> aioredis.Redis:
    """Return the singleton async Redis client, creating it if necessary."""
    global _redis, _fallback_mode
    if _redis is None:
        settings = get_settings()
        try:
            _redis = aioredis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                retry_on_timeout=True,
            )
            await _redis.ping()
            _fallback_mode = False
            logger.info("Redis connection established")
        except Exception as exc:
            logger.error("Redis connection failed: %s — running in fallback mode", exc)
            _fallback_mode = True
            _redis = None
            raise
    return _redis


async def check_redis_health() -> bool:
    """Return True if Redis is reachable."""
    try:
        r = await get_redis()
        return await r.ping()
    except Exception as exc:
        logger.error("Redis health check failed: %s", exc)
        return False


async def publish(channel: str, data: Any) -> None:
    """Publish a JSON-serialised message to a Redis channel."""
    payload = json.dumps(data) if not isinstance(data, str) else data
    try:
        r = await get_redis()
        await r.publish(channel, payload)
        logger.debug("Published to %s: %s", channel, payload[:200])
    except Exception as exc:
        logger.warning("Redis publish failed (%s), buffering in-memory: %s", channel, exc)
        _fallback_channels.setdefault(channel, []).append(payload)


async def subscribe(channel: str, handler: Callable[[str], Any]) -> None:
    """Subscribe to a Redis channel and invoke *handler* for each message."""
    async def _listener() -> None:
        while True:
            try:
                r = await get_redis()
                pubsub = r.pubsub()
                await pubsub.subscribe(channel)
                logger.info("Subscribed to Redis channel: %s", channel)
                async for message in pubsub.listen():
                    if message["type"] == "message":
                        await handler(message["data"])
            except asyncio.CancelledError:
                logger.info("Subscription to %s cancelled", channel)
                break
            except Exception as exc:
                logger.error("Redis subscription error on %s: %s — retrying in 5s", channel, exc)
                await asyncio.sleep(5)

    task = asyncio.create_task(_listener())
    _pubsub_tasks[channel] = task


async def get_value(key: str) -> Optional[str]:
    """Get a value from Redis by key."""
    try:
        r = await get_redis()
        return await r.get(key)
    except Exception as exc:
        logger.error("Redis GET failed for %s: %s", key, exc)
        return None


async def set_value(key: str, value: str, ttl: Optional[int] = None) -> None:
    """Set a value in Redis, optionally with a TTL in seconds."""
    try:
        r = await get_redis()
        if ttl:
            await r.setex(key, ttl, value)
        else:
            await r.set(key, value)
    except Exception as exc:
        logger.error("Redis SET failed for %s: %s", key, exc)


async def increment(key: str) -> int:
    """Atomically increment a Redis key and return the new value."""
    try:
        r = await get_redis()
        return await r.incr(key)
    except Exception as exc:
        logger.error("Redis INCR failed for %s: %s", key, exc)
        return -1


async def flush_fallback_buffer() -> None:
    """Attempt to flush any messages queued during Redis downtime."""
    global _fallback_channels
    if not _fallback_channels:
        return
    try:
        r = await get_redis()
        for channel, messages in list(_fallback_channels.items()):
            for msg in messages:
                await r.publish(channel, msg)
            logger.info("Flushed %d buffered messages to %s", len(messages), channel)
        _fallback_channels = {}
    except Exception as exc:
        logger.warning("Failed to flush fallback buffer: %s", exc)


async def close_redis() -> None:
    """Gracefully close all subscriptions and the Redis connection."""
    for channel, task in _pubsub_tasks.items():
        task.cancel()
        logger.info("Cancelled subscription task for %s", channel)
    _pubsub_tasks.clear()

    global _redis
    if _redis is not None:
        await _redis.close()
        _redis = None
        logger.info("Redis connection closed")
