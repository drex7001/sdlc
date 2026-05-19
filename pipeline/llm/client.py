"""Provider-pluggable LLM client.

The interface intentionally exposes a single ``complete()`` call. Providers
translate it to whatever their native SDK requires. All requests/responses are
returned in a normalised LLMResponse so the audit layer can persist them
identically regardless of provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0


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
