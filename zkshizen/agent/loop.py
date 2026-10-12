"""Agent loop: stream a model turn, run requested tools, repeat until the model stops."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from zkshizen.agent.registry import Registry
from zkshizen.llm.client import LLMClient

PREVIEW_CHARS = 600
EMPTY_TURN_RETRIES = 1


@dataclass(frozen=True, slots=True)
class AgentEvent:
    kind: str  # text | tool_start | tool_end | step_limit | empty | done
    data: dict[str, Any]


AuditFn = Callable[[str, dict[str, Any], bool, str], None]


def _parse_args(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


async def run_agent(
    llm: LLMClient,
    registry: Registry,
    messages: list[dict[str, Any]],
    *,
    max_steps: int,
    audit: AuditFn | None = None,
    temperature: float = 0.3,
) -> AsyncIterator[AgentEvent]:
    """Mutates `messages` in place so callers can keep the conversation.

    Raises LLMError if the model endpoint fails; tool errors are fed back to
    the model as tool results instead.
    """
    specs = registry.specs()
    for step in range(max_steps):
        assistant: dict[str, Any] | None = None
        for _attempt in range(EMPTY_TURN_RETRIES + 1):
            async for kind, value in llm.stream_turn(messages, tools=specs or None, temperature=temperature):
                if kind == "text":
                    yield AgentEvent("text", {"delta": value})
                else:
                    assistant = value
            assert assistant is not None
            if assistant.get("content") or assistant.get("tool_calls"):
                break
            # Proxies occasionally return an empty 200 stream; retry the same turn.
        if not (assistant.get("content") or assistant.get("tool_calls")):
            yield AgentEvent("empty", {"step": step + 1})
            return
        messages.append(assistant)
        calls = assistant.get("tool_calls") or []
        if not calls:
            yield AgentEvent("done", {"steps": step + 1})
            return
        for call in calls:
            name = call["function"]["name"]
            args = _parse_args(call["function"]["arguments"])
            yield AgentEvent("tool_start", {"name": name, "args": args})
            result = await registry.execute(name, args)
            if audit is not None:
                audit(name, args, result.ok, result.content[:2000])
            yield AgentEvent("tool_end", {"name": name, "ok": result.ok,
                                          "preview": result.content[:PREVIEW_CHARS]})
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result.content})
    yield AgentEvent("step_limit", {"max_steps": max_steps})
