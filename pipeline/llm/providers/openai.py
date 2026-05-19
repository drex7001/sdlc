"""OpenAI provider."""

from __future__ import annotations

import os
import time

from ..client import LLMResponse


class OpenAIClient:
    provider_name = "openai"

    def __init__(self) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError(
                "openai SDK not installed. pip install 'sdlc-pipeline[dev]' or pip install openai."
            ) from e
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")
        self._client = OpenAI(api_key=api_key)

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
        resp = self._client.chat.completions.create(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        text = resp.choices[0].message.content or ""
        usage = resp.usage or None
        return LLMResponse(
            text=text,
            model=resp.model,
            provider=self.provider_name,
            usage={
                "input_tokens": (usage.prompt_tokens if usage else 0),
                "output_tokens": (usage.completion_tokens if usage else 0),
            },
            raw={"id": resp.id, "finish_reason": resp.choices[0].finish_reason},
            latency_ms=latency_ms,
        )
