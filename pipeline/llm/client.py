"""Provider-pluggable LLM client.

Two call shapes are exposed:

* ``complete()`` — single-shot, returns one text response. Used by planning and
  testgen, which still produce a single JSON document.
* ``complete_with_tools()`` — multi-turn tool-use loop driver. The caller owns
  the conversation; this method just executes one turn of "send messages, get
  next assistant block (which may include tool_use blocks)". Used by codegen
  and the repair loop via the ``ToolLoop`` driver in
  ``pipeline.implementation.agent_tools``.

All requests/responses return a normalised :class:`LLMResponse` so the audit
layer persists them identically regardless of provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ToolUse:
    """One tool invocation the model has emitted."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    stop_reason: str | None = None
    # Tool calls emitted by the model in this turn (empty list when none).
    tool_uses: list[ToolUse] = field(default_factory=list)
    # Provider-shape assistant content blocks (text + tool_use), used by the
    # tool-loop driver to feed the message back into the next turn verbatim.
    assistant_content: list[dict[str, Any]] = field(default_factory=list)


class LLMClient(Protocol):
    """Minimal interface every provider implements."""

    provider_name: str

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> LLMResponse: ...

    def complete_with_tools(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 8192,
    ) -> LLMResponse: ...


def build_client(provider: str) -> LLMClient:
    """Factory. Imports lazily so optional SDKs are not required at import time."""
    if provider == "mock":
        from .providers.mock import MockClient

        return MockClient()
    if provider == "anthropic":
        from .providers.anthropic import AnthropicClient

        return AnthropicClient()
    if provider == "openai":
        from .providers.openai import OpenAIClient

        return OpenAIClient()
    raise ValueError(f"unknown LLM provider: {provider!r}")


def load_prompt(prompts_dir: Path, version: str, name: str) -> str:
    """Load a versioned prompt template. The version is recorded in audit."""
    path = prompts_dir / version / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"prompt template not found: {path}")
    return path.read_text(encoding="utf-8")
