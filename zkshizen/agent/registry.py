"""Tool registry for the agent loop.

A registry is built per channel. The web channel's registry is assembled from
web-safe tools only, so shell/file tools are absent rather than merely forbidden
by the prompt.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

MAX_TOOL_OUTPUT_CHARS = 30_000
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Through some Anthropic/OpenAI-compatible proxies, a tool_use whose input is {} makes the whole
# response come back as an empty 200 body. Tools without required arguments
# therefore get a required `reason` argument, stripped again before the handler.
REASON_ARG = "reason"
_REASON_SCHEMA = {"type": "string", "description": "One short phrase: why you are calling this tool."}

Handler = Callable[[dict[str, Any]], Any | Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    # Sync handlers (skill tools, file IO) run in a worker thread with this timeout.
    timeout_s: float = 120.0

    @property
    def injects_reason(self) -> bool:
        return not self.parameters.get("required") and REASON_ARG not in (self.parameters.get("properties") or {})

    def spec(self) -> dict[str, Any]:
        params = self.parameters
        if self.injects_reason:
            params = {**params, "type": "object",
                      "properties": {**(params.get("properties") or {}), REASON_ARG: _REASON_SCHEMA},
                      "required": [REASON_ARG]}
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": params}}


@dataclass(frozen=True, slots=True)
class ToolResult:
    ok: bool
    content: str


def _to_text(value: Any) -> str:
    # Compact JSON on purpose: through some Anthropic/OpenAI-compatible proxies, some indented multi-line
    # tool results never reach the model (it reported "clean - nothing to commit"
    # instead). The same data sent compact arrived intact. Cause not confirmed.
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str,
                                                           separators=(",", ":"))
    if len(text) > MAX_TOOL_OUTPUT_CHARS:
        cut = len(text) - MAX_TOOL_OUTPUT_CHARS
        text = text[:MAX_TOOL_OUTPUT_CHARS] + f"\n...[truncated {cut} chars]"
    return text


class Registry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            if not _NAME_RE.match(tool.name):
                raise ValueError(f"invalid tool name {tool.name!r}")
            self._tools[tool.name] = tool  # later definitions (installed skills) override

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self._tools.values()]

    async def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, f"ERROR: tool '{name}' is not available in this channel.")
        if tool.injects_reason:
            args = {k: v for k, v in args.items() if k != REASON_ARG}
        try:
            if inspect.iscoroutinefunction(tool.handler):
                result = await tool.handler(args)
            else:
                result = await asyncio.wait_for(asyncio.to_thread(tool.handler, args), tool.timeout_s)
                if inspect.isawaitable(result):
                    result = await result
        except asyncio.TimeoutError:
            return ToolResult(False, f"ERROR: tool '{name}' timed out after {tool.timeout_s:.0f}s.")
        except Exception as exc:  # tool failures are reported to the model, not raised
            return ToolResult(False, f"ERROR: {type(exc).__name__}: {exc}")
        return ToolResult(True, _to_text(result))
