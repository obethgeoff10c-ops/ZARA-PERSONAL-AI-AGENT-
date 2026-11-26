"""Model gateway: retries, budgets, structured output, usage and cost accounting."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from zara.config import Settings
from zara.llm.anthropic import AnthropicProvider
from zara.llm.base import LLMError, LLMMessage, LLMProvider, LLMResponse, LLMUsage

ModelT = TypeVar("ModelT", bound=BaseModel)


class ModelService:
    """Single gateway for every model call in a run."""

    def __init__(self, provider: LLMProvider | None, settings: Settings) -> None:
        self.provider = provider
        self.settings = settings
        self.usage = LLMUsage()
        self.calls = 0
        self.cost_usd = 0.0

    @property
    def available(self) -> bool:
        return self.provider is not None

    def _account(self, usage: LLMUsage) -> None:
        self.usage = self.usage + usage
        self.cost_usd += (
            usage.input_tokens / 1_000_000 * self.settings.llm_input_cost_per_mtok
            + usage.output_tokens / 1_000_000 * self.settings.llm_output_cost_per_mtok
        )

    def usage_snapshot(self) -> dict[str, Any]:
        return {
            "tokens_in": self.usage.input_tokens,
            "tokens_out": self.usage.output_tokens,
            "total_tokens": self.usage.total_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "model_calls": self.calls,
        }

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        max_tokens: int = 1200,
        temperature: float = 0.0,
        response_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        provider = self._require_provider()
        attempts = self.settings.llm_max_retries + 1
        last_error: LLMError | None = None
        for attempt in range(attempts):
            try:
                response = await provider.complete(
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    response_schema=response_schema,
                )
            except LLMError as exc:
                last_error = exc
                if not exc.retryable or attempt == attempts - 1:
                    raise
                await asyncio.sleep(min(4.0, 0.5 * (2**attempt)))
                continue
            self.calls += 1
            self._account(response.usage)
            return response
        raise last_error or LLMError("The model call failed.", code="llm_error")

    async def complete_json(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        model: type[ModelT],
        max_tokens: int = 1200,
        temperature: float = 0.0,
    ) -> ModelT:
        schema = model.model_json_schema()
        instruction = (
            "Return only one JSON object that validates against this JSON Schema. "
            "Do not wrap it in Markdown or add commentary.\n"
            f"{json.dumps(schema, separators=(',', ':'))}"
        )
        response = await self.complete(
            system=f"{system}\n\n{instruction}",
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_schema=schema,
        )
        payload = _extract_json(response.text)
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            raise LLMError(
                "The model returned output that did not match the required schema.",
                code="llm_invalid_output",
            ) from exc

    async def stream_text(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        max_tokens: int = 1200,
        temperature: float = 0.0,
    ) -> AsyncIterator[str]:
        provider = self._require_provider()
        async for delta in provider.stream(
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        ):
            if delta.usage is not None:
                self.calls += 1
                self._account(delta.usage)
            if delta.text:
                yield delta.text

    def _require_provider(self) -> LLMProvider:
        if self.provider is None:
            raise LLMError("No model provider is configured.", code="llm_not_configured")
        return self.provider


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.lower().startswith("json"):
            stripped = stripped[4:]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMError("The model did not return JSON.", code="llm_invalid_output")
    try:
        value = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError("The model returned invalid JSON.", code="llm_invalid_output") from exc
    if not isinstance(value, dict):
        raise LLMError("The model returned JSON that is not an object.", code="llm_invalid_output")
    return value


def build_provider(settings: Settings) -> LLMProvider | None:
    """Construct the configured provider. ``none`` is deterministic/offline."""
    if settings.llm_provider == "none":
        return None
    if settings.anthropic_api_key is None:
        raise LLMError("ANTHROPIC_API_KEY is not configured.", code="llm_not_configured")
    return AnthropicProvider(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=settings.anthropic_model,
        base_url=str(settings.anthropic_base_url),
        api_version=settings.anthropic_api_version,
        timeout=settings.llm_timeout_seconds,
    )


def build_service(settings: Settings, provider: LLMProvider | None = None) -> ModelService:
    return ModelService(provider if provider is not None else build_provider(settings), settings)
