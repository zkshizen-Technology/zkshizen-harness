"""Skill discovery and loading.

A skill is a directory holding SKILL.md (frontmatter: name, description, and
optionally `channels: [web, admin, cli]`). Skills are read on demand by the
agent via list_skills / read_skill, so instructions do not bloat every prompt.

A skill may also ship `harness_tools.py` exposing:

    TOOLS = [{"name": ..., "description": ..., "parameters": {...},
              "handler": callable(args: dict) -> Any, "channels": ["web", "cli"]}]

Skill roots, lowest priority first: skills/ of every active pack (registered by
the pack loader), then <data_dir>/skills (written by `install_skill` from the
CLI channel or by hand). Tool modules are trusted code: those locations are
writable only by the operator on the host.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zkshizen.agent.registry import Tool
from zkshizen.config import get_settings

CHANNELS = ("web", "admin", "cli")
SKILL_FILE = "SKILL.md"
# (directory, source label) registered by active packs, e.g. (<pack>/skills, "pack:hello")
_PACK_ROOTS: list[tuple[Path, str]] = []


def register_skill_root(path: Path, source: str) -> None:
    if (path, source) not in _PACK_ROOTS:
        _PACK_ROOTS.append((path, source))


def clear_skill_roots() -> None:
    _PACK_ROOTS.clear()


TOOLS_FILE = "harness_tools.py"
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MAX_SKILL_READ = 60_000


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    path: Path
    channels: tuple[str, ...]
    source: str  # "bundle" | "installed"
    has_tools: bool = False
    files: tuple[str, ...] = field(default_factory=tuple)


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Minimal YAML subset: `key: value`, quoted strings, `>` folded blocks, [a, b] lists."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    meta: dict[str, Any] = {}
    key: str | None = None
    folded: list[str] = []
    for raw in text[3:end].splitlines():
        if key and (raw.startswith(" ") or raw.startswith("\t")):
            folded.append(raw.strip())
            continue
        if key:
            meta[key] = " ".join(folded).strip()
            key, folded = None, []
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", raw)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if v in (">", "|", ">-", "|-"):
            key = k
            continue
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [p.strip().strip("'\"") for p in v[1:-1].split(",") if p.strip()]
        else:
            meta[k] = v.strip("'\"")
    if key:
        meta[key] = " ".join(folded).strip()
    return meta


def _skill_from_dir(path: Path, source: str) -> Skill | None:
    skill_md = path / SKILL_FILE
    if not skill_md.is_file():
        return None
    try:
        meta = _parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    channels = meta.get("channels") or list(CHANNELS)
    if isinstance(channels, str):
        channels = [c.strip() for c in channels.split(",")]
    files = tuple(sorted(
        str(p.relative_to(path)).replace("\\", "/") for p in path.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix not in (".pyc",)
    )[:200])
    return Skill(
        name=str(meta.get("name") or path.name),
        description=str(meta.get("description") or "")[:500],
        path=path,
        channels=tuple(c for c in channels if c in CHANNELS),
        source=source,
        has_tools=(path / TOOLS_FILE).is_file(),
        files=files,
    )


def discover() -> dict[str, Skill]:
    """Pack skills first; locally installed skills with the same name override them."""
    found: dict[str, Skill] = {}
    for root, source in (*_PACK_ROOTS, (get_settings().skills_dir, "installed")):
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if child.is_dir():
                skill = _skill_from_dir(child, source)
                if skill:
                    found[skill.name] = skill
    return found


def for_channel(channel: str) -> list[Skill]:
    return [sk for sk in discover().values() if channel in sk.channels]


def _safe_join(base: Path, rel: str) -> Path:
    target = (base / rel).resolve()
    if base.resolve() not in (target, *target.parents):
        raise ValueError("path escapes the skill directory")
    return target


def skill_tools(channel: str) -> list[Tool]:
    """list_skills / read_skill, plus tools exported by skills for this channel."""
    def list_skills(_: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"name": sk.name, "description": sk.description, "source": sk.source,
                 "has_tools": sk.has_tools} for sk in for_channel(channel)]

    def read_skill(args: dict[str, Any]) -> str:
        name = str(args.get("name", ""))
        skill = next((sk for sk in for_channel(channel) if sk.name == name), None)
        if skill is None:
            raise ValueError(f"skill '{name}' not available in channel '{channel}'")
        rel = str(args.get("file") or SKILL_FILE)
        text = _safe_join(skill.path, rel).read_text(encoding="utf-8", errors="replace")
        listing = "" if args.get("file") else "\n\nFiles in skill: " + ", ".join(skill.files)
        return text[:_MAX_SKILL_READ] + listing

    tools = [
        Tool("list_skills", "List the skills available to you (name + one-line description).",
             {"type": "object", "properties": {}}, list_skills, timeout_s=10),
        Tool("read_skill", "Read a skill's SKILL.md instructions (or another file inside the skill). "
             "Read a skill before following it.",
             {"type": "object", "properties": {
                 "name": {"type": "string"},
                 "file": {"type": "string", "description": "optional relative path inside the skill"}},
              "required": ["name"]}, read_skill, timeout_s=10),
    ]
    for skill in for_channel(channel):
        if skill.has_tools:
            tools.extend(_load_skill_module_tools(skill, channel))
    return tools


def _load_skill_module_tools(skill: Skill, channel: str) -> list[Tool]:
    mod_name = f"zks_skill_{re.sub(r'[^a-zA-Z0-9_]', '_', skill.name)}"
    spec = importlib.util.spec_from_file_location(mod_name, skill.path / TOOLS_FILE)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # a broken skill must not take the agent down
        sys.modules.pop(mod_name, None)
        return [Tool(f"{skill.name.replace('-', '_')}_load_error"[:64],
                     f"Skill '{skill.name}' failed to load: {exc}",
                     {"type": "object", "properties": {}}, lambda _a, e=exc: f"load error: {e}")]
    out: list[Tool] = []
    for entry in getattr(module, "TOOLS", []) or []:
        if channel not in (entry.get("channels") or skill.channels):
            continue
        out.append(Tool(
            name=str(entry["name"]), description=str(entry.get("description", ""))[:1000],
            parameters=entry.get("parameters") or {"type": "object", "properties": {}},
            handler=entry["handler"], timeout_s=float(entry.get("timeout_s", 60)),
        ))
    return out


def install(name: str, skill_md: str, tools_py: str | None = None,
            extra_files: dict[str, str] | None = None) -> Skill:
    """Write (or replace) an installed skill. Used by the CLI agent only."""
    if not _SKILL_NAME_RE.match(name):
        raise ValueError("skill name must be lowercase letters, digits and dashes")
    if not skill_md.lstrip().startswith("---"):
        raise ValueError("SKILL.md must start with a frontmatter block (--- name/description ---)")
    root = get_settings().skills_dir
    target = root / name
    staging = root / f".{name}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    (staging / SKILL_FILE).write_text(skill_md, encoding="utf-8")
    if tools_py:
        compile(tools_py, TOOLS_FILE, "exec")  # syntax check before activating
        (staging / TOOLS_FILE).write_text(tools_py, encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        dest = _safe_join(staging, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    skill = _skill_from_dir(target, "installed")
    assert skill is not None
    return skill


def uninstall(name: str) -> bool:
    if not _SKILL_NAME_RE.match(name):
        raise ValueError("invalid skill name")
    target = get_settings().skills_dir / name
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True
