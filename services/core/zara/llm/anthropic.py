"""Anthropic Claude provider (the only module that knows the Claude wire API)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from zara.llm.base import LLMDelta, LLMError, LLMMessage, LLMResponse, LLMUsage


class AnthropicProvider:
    """Claude Messages API client with honest error/usage reporting."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        api_version: str = "2023-06-01",
        timeout: float = 45.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Anthropic API key is required")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._api_version = api_version
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    @property
    def model(self) -> str:
        return self._model

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": self._api_version,
            "content-type": "application/json",
            "accept": "application/json",
        }

    def _body(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        max_tokens: int,
        temperature: float,
        stream: bool,
    ) -> dict[str, Any]:
        return {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": stream,
        }

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        max_tokens: int,
        temperature: float = 0.0,
        response_schema: dict[str, Any] | None = None,
    ) -> LLMResponse:
        body = self._body(
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
        )
        try:
            response = await self._http().post(
                f"{self._base_url}/v1/messages", headers=self._headers(), json=body
            )
        except httpx.HTTPError as exc:
            raise LLMError(
                "The model provider is unreachable.", code="llm_unavailable", retryable=True
            ) from exc
        self._raise_for_status(response)
        payload: dict[str, Any] = response.json()
        text = "".join(
            block.get("text", "")
            for block in payload.get("content", [])
            if block.get("type") == "text"
        )
        if not text:
            raise LLMError("The model returned an empty response.", code="llm_empty")
        usage = payload.get("usage") or {}
        return LLMResponse(
            text=text,
            usage=LLMUsage(
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
            ),
            model=str(payload.get("model") or self._model),
            stop_reason=payload.get("stop_reason"),
            raw=payload,
        )

    async def stream(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        max_tokens: int,
        temperature: float = 0.0,
    ) -> AsyncIterator[LLMDelta]:
        body = self._body(
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=True,
        )
        headers = {**self._headers(), "accept": "text/event-stream"}
        try:
            async with self._http().stream(
                "POST", f"{self._base_url}/v1/messages", headers=headers, json=body
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    self._raise_for_status(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line.removeprefix("data:").strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield LLMDelta(text=str(delta["text"]))
                    elif event.get("type") == "message_delta":
                        usage = event.get("usage") or {}
                        yield LLMDelta(
                            text="",
                            usage=LLMUsage(
                                input_tokens=int(usage.get("input_tokens") or 0),
                                output_tokens=int(usage.get("output_tokens") or 0),
                            ),
                            stop_reason=(event.get("delta") or {}).get("stop_reason"),
                        )
        except httpx.HTTPError as exc:
            raise LLMError(
                "The model provider stream failed.", code="llm_unavailable", retryable=True
            ) from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        retryable = response.status_code in {408, 409, 429} or response.status_code >= 500
        code = "llm_rate_limited" if response.status_code == 429 else "llm_http_error"
        raise LLMError(
            f"Model provider returned HTTP {response.status_code}.",
            code=code,
            retryable=retryable,
            status_code=response.status_code,
        )
