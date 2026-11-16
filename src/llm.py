"""Provider-agnostic LLM client.

OpenRouter and Groq both speak the OpenAI chat-completions dialect, so a single
``AsyncOpenAI`` client covers both; only ``base_url``, the key and the model
name differ. That is the whole point of putting the provider behind
``Settings``.

The client normalises the response into a small dict so the rest of the code
never touches SDK objects:

    {"content": str, "tool_calls": [{"id", "name", "arguments", "parse_error"}],
     "finish_reason": str, "usage": dict}

Free tiers are rate limited (OpenRouter: ~20 req/min, 50 req/day without
credits), so retries with exponential backoff and ``Retry-After`` support are
built in rather than bolted on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from openai import AsyncOpenAI
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from src.config import Settings

log = logging.getLogger("agent.llm")


class LLMError(RuntimeError):
    """The model could not be reached or returned something unusable."""


def _strip_code_fence(text: str) -> str:
    """Models love wrapping JSON in ```json fences even when told not to."""
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    return text


def extract_json(text: str) -> dict[str, Any]:
    """Best-effort JSON object extraction from a model reply.

    Tries, in order: the whole string, a de-fenced version, then the outermost
    ``{...}`` span. Raises ``LLMError`` with the offending text if all fail, so
    the failure is visible in the evidence log instead of silently ignored.
    """
    candidates = [text.strip(), _strip_code_fence(text)]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    raise LLMError(f"Model did not return a JSON object. Raw reply:\n{text[:800]}")


class LLMClient:
    """Thin async wrapper around the chat-completions endpoint."""

    def __init__(self, settings: Settings, max_attempts: int = 4) -> None:
        self.settings = settings
        self.max_attempts = max_attempts
        headers: dict[str, str] = {}
        if settings.provider == "sarvam":
                    # Sarvam wants api-subscription-key; v2 also accepts Bearer.
                    headers = {"api-subscription-key": settings.api_key}
        if settings.provider == "openrouter":
            # OpenRouter uses these for attribution/leaderboards. Optional, but
            # it keeps the account in good standing.
            headers = {
                "HTTP-Referer": "https://github.com/",
                "X-Title": "Autonomous UI Test Agent",
            }
        self._client = AsyncOpenAI(
            base_url=settings.base_url,
            api_key=settings.api_key,
            timeout=settings.request_timeout,
            max_retries=0,  # we do our own, so the backoff is visible in the log
            default_headers=headers or None,
        )
        self.call_count = 0
        self.total_tokens = 0

    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """One chat completion, with retries on transient failures."""
        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "max_tokens": self.settings.max_tokens if max_tokens is None else max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                self.call_count += 1
                response = await self._client.chat.completions.create(**payload)
                return self._normalise(response)
            except (RateLimitError, APITimeoutError, APIConnectionError) as exc:
                last_error = exc
                delay = self._retry_delay(exc, attempt)
                log.warning(
                    "LLM call failed (%s), attempt %d/%d, retrying in %.1fs",
                    type(exc).__name__,
                    attempt,
                    self.max_attempts,
                    delay,
                )
                if attempt == self.max_attempts:
                    break
                await asyncio.sleep(delay)
            except APIStatusError as exc:
                # 4xx other than rate limiting will not get better by retrying.
                raise LLMError(
                    f"LLM request rejected with HTTP {exc.status_code}: {exc.message}. "
                    f"If this is a 402/404, the model id {self.settings.model!r} is "
                    f"probably not available on your {self.settings.provider} plan."
                ) from exc

        raise LLMError(
            f"LLM unreachable after {self.max_attempts} attempts: {last_error}. "
            f"Free tiers are rate limited — wait a minute, or switch provider "
            f"with LLM_PROVIDER=groq."
        )

    # ------------------------------------------------------------------
    async def json_completion(
        self,
        messages: list[dict[str, Any]],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """A completion that is expected to be a JSON object."""
        reply = await self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        return extract_json(reply["content"])

    # ------------------------------------------------------------------
    @staticmethod
    def _retry_delay(exc: Exception, attempt: int) -> float:
        """Honour Retry-After when the provider sends one, else back off."""
        response = getattr(exc, "response", None)
        if response is not None:
            raw = response.headers.get("retry-after") if hasattr(response, "headers") else None
            if raw:
                try:
                    return min(float(raw), 60.0)
                except ValueError:
                    pass
        return min(2.0 ** attempt, 30.0)

    def _normalise(self, response: Any) -> dict[str, Any]:
        choice = response.choices[0]
        message = choice.message
        tool_calls: list[dict[str, Any]] = []

        for call in message.tool_calls or []:
            raw_args = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw_args)
                parse_error = ""
            except json.JSONDecodeError as exc:
                arguments = {}
                parse_error = f"invalid JSON arguments ({exc.msg})"
            tool_calls.append(
                {
                    "id": call.id,
                    "name": call.function.name,
                    "arguments": arguments,
                    "parse_error": parse_error,
                }
            )

        usage = {}
        if getattr(response, "usage", None) is not None:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
            self.total_tokens += usage.get("total_tokens", 0) or 0

        return {
            "content": (message.content or "").strip(),
            "tool_calls": tool_calls,
            "finish_reason": choice.finish_reason,
            "usage": usage,
        }

    async def aclose(self) -> None:
        await self._client.close()
