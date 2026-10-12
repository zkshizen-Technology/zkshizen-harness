"""Channel chat over Server-Sent Events, shared by every pack.

Event stream (one JSON object per `data:` line):
  {"type":"meta", ...}                         session info + pack context meta
  {"type":"token","content":"...","choices":[{"delta":{"content":"..."}}]}
  {"type":"tool","name":"...","status":"running|ok|error"}
  {"type":"notice","message":"..."}
  {"type":"error","message":"...","error":"...","offline":true}
  {"type":"done","session_id":"..."}

`choices` mirrors the OpenAI delta shape so simple OpenAI-style stream parsers
in existing web apps keep working.

Pack output filters (e.g. a redaction gate) run on text released at
sentence/line boundaries, so a phrase split across model chunks is filtered
as a whole and never reaches the client half-redacted.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

from zkshizen import db
from zkshizen.agent.channels import Channel, build_registry, output_filters, system_prompt
from zkshizen.agent.loop import AgentEvent, run_agent
from zkshizen.config import get_settings
from zkshizen.documents import render_for_prompt
from zkshizen.llm.client import LLMClient, LLMError, user_content
from zkshizen.packs.loader import registry as pack_registry

log = logging.getLogger("zkshizen.chat")

_BOUNDARY_RE = re.compile(r"(?:[.!?:;](?=\s)|\n)")
_FORCE_FLUSH_CHARS = 400
MAX_PHOTOS = 3


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def token_event(text: str) -> str:
    return sse({"type": "token", "content": text, "choices": [{"delta": {"content": text}}]})


class FilteredBuffer:
    def __init__(self, filters: list[Callable[[str], str]]) -> None:
        self._filters = filters
        self._pending = ""
        self.emitted: list[str] = []

    def feed(self, delta: str) -> str | None:
        self._pending += delta
        cut = None
        for m in _BOUNDARY_RE.finditer(self._pending):
            cut = m.end()
        if cut is None and len(self._pending) > _FORCE_FLUSH_CHARS:
            ws = self._pending.rfind(" ", 0, len(self._pending) - 80)
            cut = ws if ws > 0 else None
        if cut is None:
            return None
        chunk, self._pending = self._pending[:cut], self._pending[cut:]
        return self._emit(chunk)

    def flush(self) -> str | None:
        chunk, self._pending = self._pending, ""
        return self._emit(chunk) if chunk else None

    def _emit(self, chunk: str) -> str:
        for f in self._filters:
            chunk = f(chunk)
        self.emitted.append(chunk)
        return chunk

    @property
    def text(self) -> str:
        return "".join(self.emitted)


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return str(content or "")


def build_messages(channel: Channel, history: list[dict[str, Any]], photos: list[str],
                   documents: list[dict[str, Any]], extra_system: list[str]) -> list[dict[str, Any]]:
    system = "\n\n".join([system_prompt(channel), *[s for s in extra_system if s]])
    msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for m in history[-20:-1]:
        msgs.append({"role": m["role"], "content": _message_text(m["content"])})
    last = _message_text(history[-1]["content"])
    if documents:
        last += "\n\n" + render_for_prompt(documents)
    msgs.append({"role": "user", "content": user_content(last, photos[:MAX_PHOTOS])})
    return msgs


async def stream_chat(llm: LLMClient, channel: Channel, *, session_id: str, user: dict[str, Any],
                      history: list[dict[str, Any]], photos: list[str], documents: list[dict[str, Any]],
                      request_context: dict[str, Any]) -> AsyncIterator[str]:
    """Owns its own DB connection: the body runs after request-scoped dependencies are closed."""
    conn = db.connect()
    try:
        extra_system: list[str] = []
        meta: dict[str, Any] = {}
        ctx_photos: list[str] = []
        provider_ctx = dict(request_context) | {"session_id": session_id,
                                                "last_user_text": _message_text(history[-1]["content"])}
        for provider in pack_registry().context_providers(channel):
            try:
                out = provider(provider_ctx, user) or {}
            except Exception as exc:  # a pack bug must not break chat
                log.warning("context provider failed: %s", exc)
                continue
            if out.get("system"):
                extra_system.append(str(out["system"]))
            ctx_photos += [p for p in out.get("photos") or [] if isinstance(p, str)]
            meta |= out.get("meta") or {}
        all_photos = (list(photos) + ctx_photos)[:MAX_PHOTOS]
        db.add_chat_message(conn, session_id, channel, user.get("id"), "user", _message_text(history[-1]["content"]))
        yield sse({"type": "meta", "session_id": session_id, "vision": bool(all_photos), **meta})

        buf = FilteredBuffer(output_filters(channel))
        settings = get_settings()
        max_steps = settings.max_steps_admin if channel == "admin" else settings.max_steps_web
        actor = str(user.get("username") or user.get("id") or "anonymous")

        def audit(tool: str, args: dict[str, Any], ok: bool, preview: str) -> None:
            db.add_audit(conn, channel, session_id, actor, tool, args, ok, preview)

        async def events() -> AsyncIterator[AgentEvent]:
            emitted = False
            try:
                async for ev in run_agent(llm, build_registry(channel),
                                          build_messages(channel, history, all_photos, documents, extra_system),
                                          max_steps=max_steps, audit=audit):
                    emitted = True
                    yield ev
            except LLMError as exc:
                # An image the model cannot decode fails the whole request; retry once without photos.
                if emitted or not all_photos or "image" not in str(exc).lower():
                    raise
                yield AgentEvent("photo_dropped", {})
                async for ev in run_agent(llm, build_registry(channel),
                                          build_messages(channel, history, [], documents, extra_system),
                                          max_steps=max_steps, audit=audit):
                    yield ev

        try:
            async for ev in events():
                if ev.kind == "text":
                    out_text = buf.feed(ev.data["delta"])
                    if out_text:
                        yield token_event(out_text)
                elif ev.kind == "tool_start":
                    tail = buf.flush()
                    if tail:
                        yield token_event(tail)
                    yield sse({"type": "tool", "name": ev.data["name"], "status": "running"})
                elif ev.kind == "tool_end":
                    yield sse({"type": "tool", "name": ev.data["name"], "status": "ok" if ev.data["ok"] else "error"})
                elif ev.kind == "photo_dropped":
                    yield sse({"type": "notice", "message": "Photo could not be read by the model; answering without it."})
                elif ev.kind == "step_limit":
                    yield sse({"type": "notice", "message": "Tool step limit reached."})
                elif ev.kind == "empty":
                    yield sse({"type": "notice", "message": "The model returned an empty reply; please resend."})
            tail = buf.flush()
            if tail:
                yield token_event(tail)
        except LLMError as exc:
            log.warning("chat failed: %s", exc)
            tail = buf.flush()
            if tail:
                yield token_event(tail)
            msg = "AI is unavailable right now. Try again shortly."
            yield sse({"type": "error", "message": msg, "error": msg, "offline": True})
        if buf.text:
            db.add_chat_message(conn, session_id, channel, user.get("id"), "assistant", buf.text)
        yield sse({"type": "done", "session_id": session_id})
    finally:
        conn.close()
