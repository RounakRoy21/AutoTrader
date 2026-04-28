"""
Anthropic API wrapper.
  Research Agent  → claude-sonnet-4-6          (configured via ANTHROPIC_MODEL)
  Decision Engine → claude-haiku-4-5-20251001  (configured via ANTHROPIC_DECISION_MODEL)
Validates every LLM response against the provided Pydantic model.
Max 2 attempts per call — if both fail, the result is discarded.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional, Type, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from core.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MAX_LLM_RETRIES = 2


async def _increment_call_counter(model: str) -> None:
    """Atomically increment today's Anthropic API call counter in Redis.

    Uses two separate keys — research (claude-sonnet) and decision (claude-haiku)
    — so the dashboard can show both model's daily usage.  TTL is set to 24 h on
    the first call of the day so the counters reset automatically at midnight + TTL.
    Best-effort: any Redis error is silently ignored so the counter never blocks
    the main LLM call path.
    """
    try:
        from core.redis_client import get_redis
        from core.redis_keys import ANTHROPIC_CALLS_DECISION_KEY, ANTHROPIC_CALLS_RESEARCH_KEY
        settings = get_settings()
        key = (
            ANTHROPIC_CALLS_DECISION_KEY
            if model == settings.anthropic_decision_model
            else ANTHROPIC_CALLS_RESEARCH_KEY
        )
        r = await get_redis()
        new_val = await r.incr(key)
        if new_val == 1:
            await r.expire(key, 86400)  # 24-hour TTL on first call of the day
    except Exception:
        pass  # Counter is informational — never raise


class AnthropicClient:
    """Thin wrapper around the Anthropic Python SDK."""

    def __init__(self) -> None:
        settings = get_settings()
        # timeout=30.0: Claude Sonnet can take 15-25 s on complex prompts.
        # Without an explicit timeout the SDK uses httpx's default (5 s connect
        # + unlimited read), which causes silent hangs that block the scanner
        # queue indefinitely.  30 s gives enough headroom while still bounding
        # the worst-case delay to a predictable window.
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=30.0,
        )
        self._model = settings.anthropic_model

    async def generate_structured(
        self,
        system_prompt: str,
        user_content: str,
        response_model: Type[T],
        max_tokens: int = 4096,
        model: Optional[str] = None,
    ) -> Optional[T]:
        """
        Send a prompt to Claude and validate the response against *response_model*.
        Pass *model* to override the default (e.g. use Haiku for the Decision Engine).
        Returns the parsed Pydantic object or None if validation fails after retries.
        """
        active_model = model or self._model
        for attempt in range(1, MAX_LLM_RETRIES + 1):
            try:
                message = await self._client.messages.create(
                    model=active_model,
                    max_tokens=max_tokens,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_content}],
                )
                raw_text = message.content[0].text.strip()

                # Strip markdown code fences if present
                if raw_text.startswith("```"):
                    raw_text = raw_text.split("\n", 1)[1]
                    if raw_text.endswith("```"):
                        raw_text = raw_text[:-3].strip()

                data = json.loads(raw_text)
                parsed = response_model.model_validate(data)
                logger.info(
                    "LLM response validated (model=%s, attempt=%d)", active_model, attempt
                )
                # Fire-and-forget daily call counter increment (non-blocking)
                asyncio.ensure_future(_increment_call_counter(active_model))
                return parsed

            except (json.JSONDecodeError, ValidationError) as exc:
                logger.warning(
                    "LLM output validation failed (attempt %d/%d): %s",
                    attempt, MAX_LLM_RETRIES, exc,
                )
            except anthropic.APIError as exc:
                logger.error("Anthropic API error (attempt %d/%d): %s", attempt, MAX_LLM_RETRIES, exc)

        logger.error("LLM call failed after %d attempts — discarding signal", MAX_LLM_RETRIES)
        return None


# ── Module-level singleton ────────────────────────
_client: Optional[AnthropicClient] = None


def get_anthropic_client() -> AnthropicClient:
    """Return the singleton AnthropicClient."""
    global _client
    if _client is None:
        _client = AnthropicClient()
    return _client
