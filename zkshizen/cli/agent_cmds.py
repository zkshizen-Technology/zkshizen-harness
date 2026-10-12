"""`zks chat` / `zks ask`: the operator agent on this host (CLI channel).

Full host access. By default every shell command and file write asks for
confirmation (y/N). `--yolo` (or ZKS_YOLO=1) runs them without asking.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import getpass
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from zkshizen import db
from zkshizen.agent.channels import build_registry, cli_prompt
from zkshizen.agent.loop import run_agent
from zkshizen.agent.registry import Registry, ToolResult
from zkshizen.config import get_settings
from zkshizen.llm.client import LLMClient, LLMError, client_for
from zkshizen.packs.loader import load_all

DIM, BOLD, CYAN, RED, GREEN, YELLOW, RESET = ("\033[2m", "\033[1m", "\033[36m", "\033[31m", "\033[32m",
                                              "\033[33m", "\033[0m")
# Tools that change the host; confirmed interactively unless --yolo.
MUTATING = {"run_shell", "write_file", "edit_file", "install_skill", "uninstall_skill"}


def get_llm(profile: str | None = None, model: str | None = None) -> LLMClient:
    return client_for(profile, model)


class Out:
    def __init__(self) -> None:
        self.color = bool(sys.stdout.isatty() and not os.environ.get("NO_COLOR"))
        if self.color and os.name == "nt":
            os.system("")  # enable VT100 escapes on Windows 10+

    def c(self, code: str, text: str) -> str:
        return f"{code}{text}{RESET}" if self.color else text

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()


# ─── Sessions ─────────────────────────────────────────────────────────

def _session_path(session_id: str) -> Path:
    if not session_id.replace("-", "").isalnum():
        raise SystemExit("invalid session id")
    return get_settings().sessions_dir / f"{session_id}.json"


def load_session(session_id: str) -> list[dict[str, Any]]:
    path = _session_path(session_id)
    if not path.exists():
        raise SystemExit(f"session {session_id} not found")
    return json.loads(path.read_text(encoding="utf-8"))["messages"]


def save_session(session_id: str, messages: list[dict[str, Any]]) -> None:
    path = _session_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"id": session_id, "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
                               "messages": messages}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _new_session_id() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def _start(resume: str | None) -> tuple[str, list[dict[str, Any]]]:
    if resume:
        msgs = load_session(resume)
        msgs[0] = {"role": "system", "content": cli_prompt()}
        return resume, msgs
    return _new_session_id(), [{"role": "system", "content": cli_prompt()}]


# ─── Approval wrapper ─────────────────────────────────────────────────

def _fmt_args(args: dict[str, Any]) -> str:
    if "command" in args:
        return str(args["command"])
    if "path" in args:
        return str(args["path"])
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= 160 else text[:157] + "..."


class ApprovingRegistry(Registry):
    """Asks before mutating tools unless yolo. Non-interactive stdin means 'no'."""

    def __init__(self, inner: Registry, out: Out, yolo: bool) -> None:
        self._inner, self._out, self._yolo = inner, out, yolo
        self._always: set[str] = set()

    @property
    def names(self) -> list[str]:
        return self._inner.names

    def specs(self) -> list[dict[str, Any]]:
        return self._inner.specs()

    async def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name in MUTATING and not self._yolo and name not in self._always:
            if not sys.stdin.isatty():
                return ToolResult(False, "DENIED: no interactive terminal to approve this action "
                                         "(re-run with --yolo to allow).")
            self._out.write(self._out.c(YELLOW, f"  ? allow {name}: {_fmt_args(args)}  [y/N/a=always] "))
            answer = (await asyncio.to_thread(sys.stdin.readline)).strip().lower()
            if answer == "a":
                self._always.add(name)
            elif answer not in ("y", "yes"):
                return ToolResult(False, "DENIED by the operator.")
        return await self._inner.execute(name, args)


def _audit(session_id: str):
    try:
        conn = db.connect()
        db.init_core(conn)
    except Exception:
        return None
    actor = f"cli:{getpass.getuser()}"

    def audit(tool: str, args: dict[str, Any], ok: bool, preview: str) -> None:
        try:
            db.add_audit(conn, "cli", session_id, actor, tool, args, ok, preview)
        except Exception:
            pass
    return audit


async def run_turn(llm: LLMClient, messages: list[dict[str, Any]], out: Out, session_id: str,
                   *, verbose: bool, yolo: bool) -> bool:
    registry = ApprovingRegistry(build_registry("cli"), out, yolo)
    at_line_start = True
    try:
        async for ev in run_agent(llm, registry, messages, max_steps=get_settings().max_steps_cli,
                                  audit=_audit(session_id)):
            if ev.kind == "text":
                out.write(ev.data["delta"])
                at_line_start = ev.data["delta"].endswith("\n")
            elif ev.kind == "tool_start":
                if not at_line_start:
                    out.write("\n")
                out.write(out.c(CYAN, f"● {ev.data['name']}") + out.c(DIM, f"  {_fmt_args(ev.data['args'])}") + "\n")
                at_line_start = True
            elif ev.kind == "tool_end":
                mark = out.c(GREEN, "  ✓") if ev.data["ok"] else out.c(RED, "  ✗")
                lines = ev.data["preview"].strip().splitlines()
                out.write(mark + out.c(DIM, " " + "\n    ".join(lines if verbose else lines[:4])[:800]) + "\n")
            elif ev.kind == "step_limit":
                out.write(out.c(RED, f"\n[stopped: {ev.data['max_steps']} tool steps reached]\n"))
            elif ev.kind == "empty":
                out.write(out.c(RED, "\n[model returned an empty reply twice; resend the prompt]\n"))
    except LLMError as exc:
        out.write(out.c(RED, f"\n[LLM error] {exc}\n"))
        return False
    finally:
        save_session(session_id, messages)
    if not at_line_start:
        out.write("\n")
    return True


def _yolo(ns: argparse.Namespace) -> bool:
    return bool(ns.yolo or os.environ.get("ZKS_YOLO") == "1")


def cmd_ask(ns: argparse.Namespace) -> int:
    load_all()
    out = Out()
    prompt = " ".join(ns.prompt) if ns.prompt else sys.stdin.read()
    if not prompt.strip():
        raise SystemExit("empty prompt")
    session_id, messages = _start(ns.resume)
    messages.append({"role": "user", "content": prompt})
    ok = asyncio.run(run_turn(get_llm(ns.profile, ns.model), messages, out, session_id,
                              verbose=ns.verbose, yolo=_yolo(ns)))
    out.write(out.c(DIM, f"[session {session_id}]\n"))
    return 0 if ok else 1


def cmd_chat(ns: argparse.Namespace) -> int:
    load_all()
    out = Out()
    session_id, messages = _start(ns.resume)
    llm = get_llm(ns.profile, ns.model)
    mode = "FULL ACCESS, no approval prompts" if _yolo(ns) else "asks before shell/file changes"
    out.write(out.c(BOLD, "zkshizen agent") + out.c(DIM, f"  session {session_id} • {llm.profile_name} ({llm.label}) "
                                                         f"• {mode}\n  /exit to quit, /new for a fresh session\n"))
    while True:
        try:
            line = input(out.c(GREEN, "\n› "))
        except (EOFError, KeyboardInterrupt):
            out.write("\n")
            return 0
        text = line.strip()
        if not text:
            continue
        if text in ("/exit", "/quit"):
            return 0
        if text == "/new":
            session_id, messages = _start(None)
            out.write(out.c(DIM, f"[new session {session_id}]\n"))
            continue
        messages.append({"role": "user", "content": text})
        try:
            asyncio.run(run_turn(llm, messages, out, session_id, verbose=ns.verbose, yolo=_yolo(ns)))
        except KeyboardInterrupt:
            out.write(out.c(RED, "\n[interrupted]\n"))
            save_session(session_id, messages)


def cmd_sessions(_: argparse.Namespace) -> int:
    root = get_settings().sessions_dir
    files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if root.is_dir() else []
    for p in files[:30]:
        data = json.loads(p.read_text(encoding="utf-8"))
        first = next((m["content"] for m in data["messages"] if m["role"] == "user"), "")
        print(f"{data['id']}  {data.get('updated_at', '')}  {str(first)[:70]}")
    if not files:
        print("no sessions yet")
    return 0
