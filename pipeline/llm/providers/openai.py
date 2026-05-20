"""OpenAI provider.

Implements both single-shot ``complete()`` and the multi-turn
``complete_with_tools()`` via OpenAI's function-calling. The tool-loop driver
hands us messages in Anthropic content-block shape; we translate them to
OpenAI's role-based shape (assistant.tool_calls + role=tool) before each call.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any

from ..client import LLMResponse, ToolUse


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
            stop_reason=resp.choices[0].finish_reason,
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
        oai_messages = [{"role": "system", "content": system}, *_to_openai_messages(messages)]
        oai_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]
        resp = self._client.chat.completions.create(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=oai_messages,  # type: ignore[arg-type]
            tools=oai_tools,  # type: ignore[arg-type]
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or ""

        tool_uses: list[ToolUse] = []
        assistant_blocks: list[dict[str, Any]] = []
        if text:
            assistant_blocks.append({"type": "text", "text": text})
        for tc in msg.tool_calls or []:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", "") if fn else ""
            arguments = getattr(fn, "arguments", "") if fn else ""
            try:
                input_obj = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                input_obj = {"_raw": arguments}
            tool_uses.append(ToolUse(id=tc.id, name=name, input=input_obj))
            assistant_blocks.append({
                "type": "tool_use",
                "id": tc.id,
                "name": name,
                "input": input_obj,
            })

        usage = resp.usage or None
        return LLMResponse(
            text=text,
            model=resp.model,
            provider=self.provider_name,
            usage={
                "input_tokens": (usage.prompt_tokens if usage else 0),
                "output_tokens": (usage.completion_tokens if usage else 0),
            },
            raw={"id": resp.id, "finish_reason": choice.finish_reason},
            latency_ms=latency_ms,
            stop_reason=choice.finish_reason,
            tool_uses=tool_uses,
            assistant_content=assistant_blocks,
        )


def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic-shape conversation into OpenAI chat shape."""
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for b in content:
                if b.get("type") == "text":
                    text_parts.append(b["text"])
                elif b.get("type") == "tool_use":
                    tool_calls.append({
                        "id": b["id"],
                        "type": "function",
                        "function": {
                            "name": b["name"],
                            "arguments": json.dumps(b.get("input") or {}),
                        },
                    })
            entry: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                entry["content"] = "".join(text_parts)
            if tool_calls:
                entry["tool_calls"] = tool_calls
            out.append(entry)
        else:  # user
            user_text_parts: list[str] = []
            for b in content:
                btype = b.get("type")
                if btype == "tool_result":
                    out.append({
                        "role": "tool",
                        "tool_call_id": b["tool_use_id"],
                        "content": _coerce_result_content(b.get("content")),
                    })
                elif btype == "text":
                    user_text_parts.append(b["text"])
            if user_text_parts:
                out.append({"role": "user", "content": "".join(user_text_parts)})
    return out


def _coerce_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content)
