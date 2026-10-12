"""Tools for the CLI channel: full host access, no approval prompts.

These tools are only ever registered inside the `zks` CLI process, which an
operator starts from a shell on the host. The HTTP daemon never imports this
module, so nothing reachable from the web can run commands or touch files.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
from pathlib import Path
from typing import Any

from zkshizen.agent import skills as skill_store
from zkshizen.agent.registry import Tool

MAX_FILE_READ = 200_000
DEFAULT_CMD_TIMEOUT = 120
MAX_CMD_TIMEOUT = 1800
IS_WINDOWS = platform.system() == "Windows"


def _shell_argv(command: str) -> list[str]:
    if IS_WINDOWS:
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    return [shutil.which("bash") or "/bin/sh", "-lc", command]


async def run_shell(args: dict[str, Any]) -> dict[str, Any]:
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command is required")
    timeout = min(max(int(args.get("timeout_s", DEFAULT_CMD_TIMEOUT)), 1), MAX_CMD_TIMEOUT)
    cwd = Path(os.path.expanduser(str(args.get("cwd") or os.getcwd())))
    proc = await asyncio.create_subprocess_exec(
        *_shell_argv(command), cwd=str(cwd),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"exit_code": None, "timed_out": True, "timeout_s": timeout}
    return {
        "exit_code": proc.returncode,
        "stdout": out.decode("utf-8", "replace"),
        "stderr": err.decode("utf-8", "replace"),
    }


def _path(args: dict[str, Any], key: str = "path") -> Path:
    raw = str(args.get(key, "")).strip()
    if not raw:
        raise ValueError(f"{key} is required")
    return Path(os.path.expanduser(raw)).resolve()


def read_file(args: dict[str, Any]) -> str:
    p = _path(args)
    data = p.read_bytes()
    if b"\x00" in data[:4096]:
        return f"[binary file, {len(data)} bytes]"
    text = data.decode("utf-8", "replace")
    offset = max(int(args.get("offset_line", 1)), 1) - 1
    limit = int(args.get("max_lines", 2000))
    lines = text.splitlines()
    chunk = lines[offset:offset + limit]
    body = "\n".join(f"{i + offset + 1:>6}\t{line}" for i, line in enumerate(chunk))
    tail = f"\n[{len(lines)} lines total]" if len(lines) > offset + limit or offset else ""
    return body[:MAX_FILE_READ] + tail


def write_file(args: dict[str, Any]) -> str:
    p = _path(args)
    content = str(args.get("content", ""))
    p.parent.mkdir(parents=True, exist_ok=True)
    if args.get("append"):
        with p.open("a", encoding="utf-8") as fh:
            fh.write(content)
    else:
        p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {p}"


def edit_file(args: dict[str, Any]) -> str:
    p = _path(args)
    old, new = str(args.get("old", "")), str(args.get("new", ""))
    if not old:
        raise ValueError("old must be non-empty")
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise ValueError("old text not found")
    if count > 1 and not args.get("replace_all"):
        raise ValueError(f"old text occurs {count} times; pass replace_all or a longer snippet")
    p.write_text(text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1), encoding="utf-8")
    return f"replaced {count if args.get('replace_all') else 1} occurrence(s) in {p}"


def list_dir(args: dict[str, Any]) -> list[str]:
    p = _path(args)
    entries = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))[:500]:
        entries.append(child.name + ("/" if child.is_dir() else f"  ({child.stat().st_size} B)"))
    return entries


def install_skill(args: dict[str, Any]) -> dict[str, Any]:
    skill = skill_store.install(
        str(args.get("name", "")), str(args.get("skill_md", "")),
        tools_py=args.get("tools_py"), extra_files=args.get("extra_files"),
    )
    return {"installed": skill.name, "path": str(skill.path), "channels": list(skill.channels),
            "has_tools": skill.has_tools,
            "note": "The web daemon picks up new skills on its next request; no restart needed."}


def uninstall_skill(args: dict[str, Any]) -> dict[str, Any]:
    return {"removed": skill_store.uninstall(str(args.get("name", "")))}


CLI_TOOLS: list[Tool] = [
    Tool("run_shell", f"Run a shell command on this host ({'PowerShell' if IS_WINDOWS else 'bash'}). "
         "Returns exit code, stdout, stderr. Non-interactive: never use commands that wait for input.",
         {"type": "object", "properties": {
             "command": {"type": "string"},
             "cwd": {"type": "string"},
             "timeout_s": {"type": "integer", "minimum": 1, "maximum": MAX_CMD_TIMEOUT}},
          "required": ["command"]}, run_shell, timeout_s=MAX_CMD_TIMEOUT + 10),
    Tool("read_file", "Read a text file with line numbers.",
         {"type": "object", "properties": {
             "path": {"type": "string"}, "offset_line": {"type": "integer"}, "max_lines": {"type": "integer"}},
          "required": ["path"]}, read_file, timeout_s=30),
    Tool("write_file", "Create or overwrite a text file (or append with append=true).",
         {"type": "object", "properties": {
             "path": {"type": "string"}, "content": {"type": "string"}, "append": {"type": "boolean"}},
          "required": ["path", "content"]}, write_file, timeout_s=30),
    Tool("edit_file", "Replace an exact text snippet in a file. `old` must be unique unless replace_all.",
         {"type": "object", "properties": {
             "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"},
             "replace_all": {"type": "boolean"}},
          "required": ["path", "old", "new"]}, edit_file, timeout_s=30),
    Tool("list_dir", "List a directory.",
         {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
         list_dir, timeout_s=15),
    Tool("install_skill", "Install or replace a skill (SKILL.md, optional harness_tools.py exporting TOOLS, "
         "optional extra files). Frontmatter `channels: [web, cli]` controls who can use it. "
         "Use this to give the web chat new abilities such as internet search.",
         {"type": "object", "properties": {
             "name": {"type": "string", "description": "lowercase-dash name"},
             "skill_md": {"type": "string"},
             "tools_py": {"type": "string"},
             "extra_files": {"type": "object", "additionalProperties": {"type": "string"}}},
          "required": ["name", "skill_md"]}, install_skill, timeout_s=30),
    Tool("uninstall_skill", "Remove an installed skill.",
         {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
         uninstall_skill, timeout_s=15),
]
