"""Anthropic Claude provider.

Uses prompt caching on the system prompt so repeated stages on the same model
amortise the cost of the (large) instructions.
"""

from __future__ import annotations

import os
import time

from ..client import LLMResponse


class AnthropicClient:
    provider_name = "anthropic"

    def __init__(self) -> None:
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError(
                "anthropic SDK not installed. pip install 'sdlc-pipeline[dev]' or pip install anthropic."
            ) from e
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> LLMResponse:
        start = time.perf_counter()
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=[
                {
                    "type": "text",
                    "text": system,
                    # Mark the system prompt as cacheable. The Anthropic API
                    # will reuse cached tokens across calls within ~5 minutes,
                    # which is the typical span of one pipeline run.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": prompt}],
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        text = "".join(
            getattr(block, "text", "")
            for block in resp.content
            if getattr(block, "type", None) == "text"
        )
        return LLMResponse(
            text=text,
            model=resp.model,
            provider=self.provider_name,
            usage={
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "cache_creation_input_tokens": getattr(
                    resp.usage, "cache_creation_input_tokens", 0
                ) or 0,
                "cache_read_input_tokens": getattr(
                    resp.usage, "cache_read_input_tokens", 0
                ) or 0,
            },
            raw={"id": resp.id, "stop_reason": resp.stop_reason},
            latency_ms=latency_ms,
        )
