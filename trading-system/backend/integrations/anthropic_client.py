"""
Anthropic API wrapper for Claude claude-sonnet-4-20250514 calls.
Validates every LLM response against the provided Pydantic model.
Max 2 attempts per signal — if both fail, the signal is discarded.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Type, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from core.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

MAX_LLM_RETRIES = 2


class AnthropicClient:
    """Thin wrapper around the Anthropic Python SDK."""

    def __init__(self) -> None:
        settings = get_settings()
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.anthropic_model

    async def generate_structured(
        self,
        system_prompt: str,
        user_content: str,
        response_model: Type[T],
        max_tokens: int = 4096,
    ) -> Optional[T]:
        """
        Send a prompt to Claude and validate the response against *response_model*.
        Returns the parsed Pydantic object or None if validation fails after retries.
        """
        for attempt in range(1, MAX_LLM_RETRIES + 1):
            try:
                message = await self._client.messages.create(
                    model=self._model,
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
                    "LLM response validated (model=%s, attempt=%d)", self._model, attempt
                )
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
