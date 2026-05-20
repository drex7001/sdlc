"""Anthropic Claude provider.

Uses prompt caching on the system prompt so repeated stages on the same model
amortise the cost of the (large) instructions. The same caching is applied to
single-shot ``complete()`` calls and to the per-turn ``complete_with_tools()``
calls that drive the codegen / repair tool loop.
"""

from __future__ import annotations

import os
import time
from typing import Any

from ..client import LLMResponse, ToolUse


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
            system=_cached_system(system),  # type: ignore[arg-type]
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
            usage=_usage(resp),
            raw={"id": resp.id, "stop_reason": resp.stop_reason},
            latency_ms=latency_ms,
            stop_reason=resp.stop_reason,
            assistant_content=_serialise_content(resp.content),
        )

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> LLMResponse:
        start = time.perf_counter()
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=_cached_system(system),  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
        )
        latency_ms = int((time.perf_counter() - start) * 1000)

        text_parts: list[str] = []
        tool_uses: list[ToolUse] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype == "tool_use":
                tool_uses.append(ToolUse(
                    id=getattr(block, "id", ""),
                    name=getattr(block, "name", ""),
                    input=dict(getattr(block, "input", {}) or {}),
                ))

        return LLMResponse(
            text="".join(text_parts),
            model=resp.model,
            provider=self.provider_name,
            usage=_usage(resp),
            raw={"id": resp.id, "stop_reason": resp.stop_reason},
            latency_ms=latency_ms,
            stop_reason=resp.stop_reason,
            tool_uses=tool_uses,
            assistant_content=_serialise_content(resp.content),
        )


def _cached_system(system: str) -> list[dict[str, Any]]:
    # Mark the system prompt as cacheable. The Anthropic API will reuse cached
    # tokens across calls within ~5 minutes, which is the typical span of one
    # pipeline run (incl. the multi-turn tool-use loop).
    return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]


def _usage(resp: Any) -> dict[str, int]:
    return {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_creation_input_tokens": getattr(resp.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0) or 0,
    }


def _serialise_content(blocks: list[Any]) -> list[dict[str, Any]]:
    """Convert SDK content blocks into plain dicts so they can be passed back."""
    out: list[dict[str, Any]] = []
    for b in blocks:
        btype = getattr(b, "type", None)
        if btype == "text":
            out.append({"type": "text", "text": getattr(b, "text", "")})
        elif btype == "tool_use":
            out.append({
                "type": "tool_use",
                "id": getattr(b, "id", ""),
                "name": getattr(b, "name", ""),
                "input": dict(getattr(b, "input", {}) or {}),
            })
    return out
