"""
Anthropic API wrapper.
  Research Agent  → claude-sonnet-4-6          (configured via ANTHROPIC_MODEL)
  Decision Engine → claude-haiku-4-5-20251001  (configured via ANTHROPIC_DECISION_MODEL)
Validates every LLM response against the provided Pydantic model.
Max 2 attempts per call — if both fail, the result is discarded.
"""

from __future__ import annotations

import asyncio
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
        # timeout=90.0: Claude Sonnet on the market-brief prompt (~8 k input tokens)
        # regularly takes 25–50 s.  The previous 30 s limit caused the client to
        # disconnect mid-response (HTTP 499) before Anthropic finished, which was
        # then silently retried — producing 6 identical calls that all failed.
        # Haiku is fast (<5 s) so 90 s gives both models ample headroom.
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=90.0,
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
                # Increment daily call counter immediately after a successful HTTP
                # response — before validation — so we count every billed API call
                # regardless of whether Pydantic parsing succeeds.
                await _increment_call_counter(active_model)

                # Log token usage from the response — no extra API call needed;
                # message.usage is always populated by the Anthropic SDK.
                logger.info(
                    "LLM usage: model=%s input_tokens=%d output_tokens=%d",
                    active_model,
                    message.usage.input_tokens,
                    message.usage.output_tokens,
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
                return parsed

            except (json.JSONDecodeError, ValidationError) as exc:
                logger.warning(
                    "LLM output validation failed (attempt %d/%d): %s",
                    attempt, MAX_LLM_RETRIES, exc,
                )
            except anthropic.APITimeoutError as exc:
                logger.error(
                    "Anthropic request timed out (attempt %d/%d) — "
                    "consider raising timeout or reducing prompt size: %s",
                    attempt, MAX_LLM_RETRIES, exc,
                )
                if attempt < MAX_LLM_RETRIES:
                    await asyncio.sleep(5)
            except anthropic.APIError as exc:
                logger.error("Anthropic API error (attempt %d/%d): %s", attempt, MAX_LLM_RETRIES, exc)
                if attempt < MAX_LLM_RETRIES:
                    await asyncio.sleep(2)

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

