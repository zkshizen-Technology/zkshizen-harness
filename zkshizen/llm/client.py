"""LLM client for two wire protocols: OpenAI Chat Completions and Anthropic Messages.

Callers always use the OpenAI message shape (system/user/assistant/tool roles,
image_url parts, tool_calls). With provider="anthropic" requests and responses
are translated to the Messages API, so the agent loop and every pack are
provider-agnostic. Which provider/endpoint/model/key is used comes from
zkshizen.llm.config (llm.json profiles + env overrides).
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from zkshizen.llm.config import LLMConfigError, LLMProfile, resolve

ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    """The LLM endpoint is unreachable, misconfigured or returned an error."""


def user_content(text: str, photos: list[str] | None = None) -> str | list[dict[str, Any]]:
    """User message content; photos become image_url parts (translated per provider)."""
    if not photos:
        return text
    return [{"type": "text", "text": text}] + [
        {"type": "image_url", "image_url": {"url": p}} for p in photos
    ]


def extract_json(text: str) -> dict[str, Any] | None:
    """First balanced JSON object in a model reply (handles ```json fences)."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i, ch in enumerate(text[start:], start):
            if in_str:
                esc = (ch == "\\") and not esc
                if ch == '"' and not esc:
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


ToolHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]

# ─── Anthropic translation ─────────────────────────────────────────────

_DATA_URL_RE = re.compile(r"^data:(image/[\w.+-]+);base64,(.*)$", re.S)


def _blocks(content: Any) -> list[dict[str, Any]]:
    """OpenAI content (str or parts) -> Anthropic content blocks."""
    if not content:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out: list[dict[str, Any]] = []
    for part in content:
        if part.get("type") == "text" and part.get("text"):
            out.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            m = _DATA_URL_RE.match(url)
            if m:
                media = "image/jpeg" if m.group(1) == "image/jpg" else m.group(1)
                out.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": m.group(2)}})
            elif url:
                out.append({"type": "image", "source": {"type": "url", "url": url}})
    return out


def to_anthropic(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> dict[str, Any]:
    system: list[str] = []
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        # Messages API requires alternating roles: merge consecutive same-role turns.
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m.get("role")
        if role == "system":
            system += [b["text"] for b in _blocks(m.get("content")) if b["type"] == "text"]
        elif role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                           "content": str(m.get("content") or "")}])
        elif role == "assistant":
            blocks = _blocks(m.get("content"))
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": fn.get("arguments")}
                blocks.append({"type": "tool_use", "id": call.get("id"), "name": fn.get("name"),
                               "input": args if isinstance(args, dict) else {"_value": args}})
            push("assistant", blocks or [{"type": "text", "text": "(no content)"}])
        else:
            push("user", _blocks(m.get("content")) or [{"type": "text", "text": "(empty)"}])
    body: dict[str, Any] = {"messages": out}
    if system:
        body["system"] = "\n\n".join(system)
    if tools:
        body["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                          "input_schema": t["function"].get("parameters") or {"type": "object", "properties": {}}}
                         for t in tools]
    return body


def from_anthropic(data: dict[str, Any]) -> dict[str, Any]:
    # Some Anthropic/OpenAI-compatible proxies answer non-streaming /v1/messages
    # with an OpenAI chat.completion body; accept that shape too.
    if "choices" in data and "content" not in data:
        return data["choices"][0]["message"]
    text: list[str] = []
    calls: list[dict[str, Any]] = []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({"id": block.get("id"), "type": "function", "function": {
                "name": block.get("name"), "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)}})
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
    if calls:
        msg["tool_calls"] = calls
    return msg


# ─── Client ─────────────────────────────────────────────────────────────

@dataclass
class LLMClient:
    base_url: str
    api_key: str
    model: str
    timeout_s: float = 60.0
    transport: httpx.AsyncBaseTransport | None = None
    provider: str = "openai"
    token_param: str = "max_tokens"
    profile_name: str = "custom"
    config_error: str | None = None
    auth: str = "x-api-key"

    @classmethod
    def from_profile(cls, p: LLMProfile, transport: httpx.AsyncBaseTransport | None = None) -> LLMClient:
        return cls(base_url=p.base_url, api_key=p.api_key, model=p.model, timeout_s=p.timeout_s,
                   transport=transport, provider=p.provider, token_param=p.token_param, profile_name=p.name,
                   auth=p.auth)

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"

    def _check(self) -> None:
        if self.config_error:
            raise LLMError(f"LLM not configured: {self.config_error}")
        if self.provider == "anthropic" and not self.api_key:
            raise LLMError("no Anthropic API key configured (zks config set <profile> --api-key -)")

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.provider == "anthropic":
            if self.auth == "bearer":
                h["Authorization"] = f"Bearer {self.api_key}"
            else:
                h["x-api-key"] = self.api_key
            h["anthropic-version"] = ANTHROPIC_VERSION
        elif self.api_key:  # local OpenAI-compatible proxies may need no key
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _path(self) -> str:
        if self.provider == "anthropic":
            return "/messages" if self.base_url.endswith("/v1") else "/v1/messages"
        return "/chat/completions"

    def _payload(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None,
                 temperature: float, max_tokens: int, stream: bool) -> dict[str, Any]:
        if self.provider == "anthropic":
            body = to_anthropic(messages, tools)
            body.update(model=self.model, max_tokens=max_tokens, temperature=temperature, stream=stream)
            return body
        body = {"model": self.model, "messages": messages, "stream": stream,
                "temperature": temperature, self.token_param: max_tokens}
        if tools:
            body["tools"] = tools
        return body

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, headers=self._headers(),
                                 timeout=httpx.Timeout(self.timeout_s, connect=5.0), transport=self.transport)

    def _unreachable(self, exc: Exception) -> LLMError:
        return LLMError(f"{self.provider} endpoint unreachable ({self.base_url}): {exc}")

    async def _post_once(self, client: httpx.AsyncClient, messages: list[dict[str, Any]],
                         tools: list[dict[str, Any]] | None, temperature: float, max_tokens: int) -> dict[str, Any]:
        try:
            resp = await client.post(self._path(), json=self._payload(messages, tools, temperature, max_tokens, False))
        except httpx.HTTPError as exc:
            raise self._unreachable(exc) from exc
        if resp.status_code >= 400:
            raise LLMError(f"{self.provider} HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            return from_anthropic(data) if self.provider == "anthropic" else data["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"{self.provider} returned an unexpected payload") from exc

    async def complete(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
                       tool_handler: ToolHandler | None = None, max_tool_rounds: int = 4,
                       temperature: float = 0.2, max_tokens: int = 2048) -> str:
        """Non-streaming completion with a tool-calling loop."""
        self._check()
        convo = list(messages)
        async with self._client() as client:
            for _ in range(max_tool_rounds + 1):
                msg = await self._post_once(client, convo, tools, temperature, max_tokens)
                calls = msg.get("tool_calls") or []
                if not calls or tool_handler is None:
                    return msg.get("content") or ""
                convo.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
                for call in calls:
                    fn = call.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = await tool_handler(fn.get("name", ""), args)
                    convo.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                  "content": json.dumps(result, ensure_ascii=False, default=str)})
        raise LLMError("tool-calling loop exceeded max rounds")

    async def stream_turn(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
                          temperature: float = 0.3, max_tokens: int = 4096) -> AsyncIterator[tuple[str, Any]]:
        """One streamed assistant turn: ("text", delta)* then one ("message", openai_message)."""
        self._check()
        content: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        parse = _parse_anthropic if self.provider == "anthropic" else _parse_openai
        async with self._client() as client:
            try:
                async with client.stream("POST", self._path(),
                                         json=self._payload(messages, tools, temperature, max_tokens, True)) as resp:
                    if resp.status_code >= 400:
                        body = (await resp.aread()).decode("utf-8", "replace")
                        raise LLMError(f"{self.provider} HTTP {resp.status_code}: {body[:300]}")
                    async for delta in parse(resp, calls):
                        content.append(delta)
                        yield ("text", delta)
                if not content and not calls:
                    # Some Anthropic/OpenAI-compatible proxies answer certain tool-enabled streams with an
                    # empty 200 body. The same request without streaming does answer.
                    try:
                        msg = await self._post_once(client, messages, tools, temperature, max_tokens)
                    except LLMError:
                        msg = {}  # leave the turn empty; the agent loop retries and then reports it
                    if msg.get("content"):
                        content.append(msg["content"])
                        yield ("text", msg["content"])
                    for i, call in enumerate(msg.get("tool_calls") or []):
                        calls[i] = {"id": call.get("id") or "", "type": "function",
                                    "function": {"name": call["function"]["name"],
                                                 "arguments": call["function"].get("arguments") or ""}}
            except httpx.HTTPError as exc:
                raise self._unreachable(exc) from exc
        message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
        if calls:
            message["tool_calls"] = [
                calls[i] | {"id": calls[i]["id"] or f"call_{i}",
                            "function": calls[i]["function"] | {"arguments": calls[i]["function"]["arguments"] or "{}"}}
                for i in sorted(calls)]
        yield ("message", message)

    async def stream(self, messages: list[dict[str, Any]], *, temperature: float = 0.3,
                     max_tokens: int = 2048) -> AsyncIterator[str]:
        async for kind, value in self.stream_turn(messages, temperature=temperature, max_tokens=max_tokens):
            if kind == "text":
                yield value


async def _sse_json(resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async for line in resp.aiter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            continue


async def _parse_openai(resp: httpx.Response, calls: dict[int, dict[str, Any]]) -> AsyncIterator[str]:
    async for chunk in _sse_json(resp):
        if chunk.get("error"):
            raise LLMError(f"openai stream error: {chunk['error']}")
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                yield delta["content"]
            for tc in delta.get("tool_calls") or []:
                slot = calls.setdefault(int(tc.get("index", 0)), {
                    "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""


async def _parse_anthropic(resp: httpx.Response, calls: dict[int, dict[str, Any]]) -> AsyncIterator[str]:
    async for ev in _sse_json(resp):
        kind = ev.get("type")
        if kind == "error":
            raise LLMError(f"anthropic stream error: {ev.get('error')}")
        if kind == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "tool_use":
                calls[int(ev.get("index", 0))] = {"id": block.get("id", ""), "type": "function",
                                                  "function": {"name": block.get("name", ""), "arguments": ""}}
        elif kind == "content_block_delta":
            delta = ev.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                yield delta["text"]
            elif delta.get("type") == "input_json_delta":
                slot = calls.get(int(ev.get("index", 0)))
                if slot is not None:
                    slot["function"]["arguments"] += delta.get("partial_json", "")
        elif kind == "message_stop":
            return


def client_for(profile: str | None = None, model: str | None = None) -> LLMClient:
    """Client for a profile (default when None). Config problems surface as LLMError on use."""
    try:
        return LLMClient.from_profile(resolve(profile, model))
    except LLMConfigError as exc:
        return LLMClient(base_url="", api_key="", model=model or "unconfigured", config_error=str(exc))


def get_llm() -> LLMClient:
    """FastAPI dependency: the default profile. Tests replace it via app.dependency_overrides."""
    return client_for()
