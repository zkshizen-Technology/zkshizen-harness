"""Pack discovery, installation and the PackContext extension API.

A pack is a directory under <data_dir>/packs/<id>/ containing pack.toml.
Its entry function receives a PackContext and registers:

  ctx.add_tools(channel, [Tool, ...])          tools for web / admin / cli
  ctx.add_router(fastapi.APIRouter)            mounted under /api/packs/<id>
  ctx.add_system_prompt(channel, text)         appended to the channel prompt
  ctx.add_output_filter(channel, fn)           text -> text, applied to streamed replies
  ctx.add_migration(id, sql)                   run once on startup
  ctx.add_startup(fn)                          called once the database is ready
  ctx.config / ctx.license                     licensed config and license state

Licensed packs are loaded only while their license is valid (active or in
grace). An invalid pack is listed with its reason and contributes nothing.
"""

from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zkshizen import __version__
from zkshizen.agent import skills as skill_store
from zkshizen.agent.registry import Tool
from zkshizen.config import get_settings
from zkshizen.packs import license as lic
from zkshizen.packs.manifest import MANIFEST, Manifest, ManifestError, load_manifest, version_tuple

CHANNELS = ("web", "admin", "cli")
MAX_ZIP_BYTES = 200 * 1024 * 1024
CORE_API_PREFIXES = ("/api/auth", "/api/chat", "/api/health", "/api/me", "/api/packs")
OutputFilter = Callable[[str], str]
ContextProvider = Callable[[dict[str, Any], dict[str, Any]], "dict[str, Any] | None"]


class PackError(ValueError):
    pass


@dataclass
class PackContext:
    manifest: Manifest
    config: dict[str, Any]
    license: lic.LicenseState | None
    tools: dict[str, list[Tool]] = field(default_factory=lambda: {c: [] for c in CHANNELS})
    routers: list[Any] = field(default_factory=list)
    prompts: dict[str, list[str]] = field(default_factory=lambda: {c: [] for c in CHANNELS})
    filters: dict[str, list[OutputFilter]] = field(default_factory=lambda: {c: [] for c in CHANNELS})
    migrations: list[tuple[str, str]] = field(default_factory=list)
    startup: list[Callable[[], None]] = field(default_factory=list)
    context_providers: dict[str, list[ContextProvider]] = field(default_factory=lambda: {c: [] for c in CHANNELS})
    config_listeners: list[Callable[[dict[str, Any]], None]] = field(default_factory=list)
    web_dir_override: Path | None = None

    @property
    def web_dir(self) -> Path | None:
        return self.web_dir_override or self.manifest.web_path

    @property
    def pack_id(self) -> str:
        return self.manifest.id

    @property
    def root(self) -> Path:
        return self.manifest.root

    def _channel(self, channel: str) -> str:
        if channel not in CHANNELS:
            raise PackError(f"unknown channel '{channel}'")
        return channel

    def add_tools(self, channel: str, tools: list[Tool]) -> None:
        self.tools[self._channel(channel)].extend(tools)

    def add_router(self, router: Any, prefix: str | None = None) -> None:
        """Mount under /api/packs/<id> by default, or under a custom /api/<name> prefix
        (e.g. a license server's public /api/license). Core prefixes are reserved."""
        if prefix is not None:
            prefix = prefix.rstrip("/")
            if not prefix.startswith("/api/") or any(prefix == r or prefix.startswith(r + "/")
                                                     for r in CORE_API_PREFIXES):
                raise PackError(f"router prefix {prefix!r} must be under /api/ and not a core prefix")
        self.routers.append((router, prefix or f"/api/packs/{self.pack_id}"))

    def add_system_prompt(self, channel: str, text: str) -> None:
        self.prompts[self._channel(channel)].append(text.strip())

    def add_output_filter(self, channel: str, fn: OutputFilter) -> None:
        self.filters[self._channel(channel)].append(fn)

    def add_migration(self, migration_id: str, sql: str) -> None:
        self.migrations.append((f"{self.pack_id}:{migration_id}", sql))

    def add_startup(self, fn: Callable[[], None]) -> None:
        self.startup.append(fn)

    def add_context_provider(self, channel: str, fn: ContextProvider) -> None:
        """fn(request_context: dict, user: dict) -> {"system": str, "photos": [...], "meta": {...}} | None.
        Lets a pack inject per-request context (e.g. a pinned case) into chat."""
        self.context_providers[self._channel(channel)].append(fn)

    def add_config_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Called with the new licensed config after `zks pack sync` / periodic check changed it."""
        self.config_listeners.append(fn)

    def set_web_dir(self, path: Path) -> None:
        """Serve static files from `path` instead of the manifest web_dir (e.g. a rendered copy)."""
        self.web_dir_override = path

    def feature(self, name: str) -> bool:
        """Licensed feature flag. Unlicensed packs have every feature."""
        if not self.manifest.license.required:
            return True
        return name in (self.license.payload.get("features", []) if self.license else [])


@dataclass(frozen=True, slots=True)
class PackStatus:
    id: str
    name: str
    version: str
    loaded: bool
    reason: str
    license: dict[str, Any] | None
    web_mount: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "version": self.version, "loaded": self.loaded,
                "reason": self.reason, "license": self.license, "web_mount": self.web_mount}


@dataclass
class PackRegistry:
    contexts: list[PackContext] = field(default_factory=list)
    status: list[PackStatus] = field(default_factory=list)

    def tools(self, channel: str) -> list[Tool]:
        return [t for c in self.contexts for t in c.tools.get(channel, [])]

    def prompts(self, channel: str) -> list[str]:
        return [p for c in self.contexts for p in c.prompts.get(channel, [])]

    def filters(self, channel: str) -> list[OutputFilter]:
        return [f for c in self.contexts for f in c.filters.get(channel, [])]

    def context_providers(self, channel: str) -> list[ContextProvider]:
        return [p for c in self.contexts for p in c.context_providers.get(channel, [])]

    def get(self, pack_id: str) -> PackContext | None:
        return next((c for c in self.contexts if c.pack_id == pack_id), None)


_REGISTRY = PackRegistry()


def registry() -> PackRegistry:
    return _REGISTRY


def installed_manifests() -> list[Manifest]:
    root = get_settings().packs_dir
    out: list[Manifest] = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / MANIFEST).is_file() and not child.name.startswith("."):
            try:
                out.append(load_manifest(child))
            except ManifestError:
                continue
    return out


def _import_entry(manifest: Manifest) -> Callable[[PackContext], None] | None:
    if not manifest.entry:
        return None
    if manifest.python_dir and str(manifest.python_dir) not in sys.path:
        sys.path.insert(0, str(manifest.python_dir))
    module_name, func = manifest.entry.split(":")
    for name in list(sys.modules):  # fresh import after a pack upgrade
        if name == module_name or name.startswith(module_name + "."):
            del sys.modules[name]
    return getattr(importlib.import_module(module_name), func)


def load_all() -> PackRegistry:
    """(Re)load every installed pack into the process-wide registry."""
    reg = PackRegistry()
    skill_store.clear_skill_roots()
    for manifest in installed_manifests():
        state: lic.LicenseState | None = None
        config: dict[str, Any] = {}
        reason = "loaded"
        if version_tuple(__version__) < version_tuple(manifest.min_core):
            reason = f"needs zkshizen >= {manifest.min_core}"
        elif manifest.license.required:
            state = lic.local_state(manifest)
            config = state.config
            if not state.valid:
                reason = state.message
            elif state.status == "grace":
                reason = state.message
        else:
            config = lic.load_local(manifest)[1] or {}
        ok = reason == "loaded" or (state is not None and state.valid)
        if ok:
            ctx = PackContext(manifest, config, state)
            try:
                entry = _import_entry(manifest)
                if entry:
                    entry(ctx)
            except Exception as exc:  # a broken pack must not take the harness down
                ok, reason = False, f"entry failed: {type(exc).__name__}: {exc}"
            else:
                reg.contexts.append(ctx)
                if manifest.skills_path and manifest.skills_path.is_dir():
                    skill_store.register_skill_root(manifest.skills_path, f"pack:{manifest.id}")
        reg.status.append(PackStatus(manifest.id, manifest.name, manifest.version, ok, reason,
                                     state.as_dict() if state else None, manifest.web_mount if ok else None))
    _REGISTRY.contexts, _REGISTRY.status = reg.contexts, reg.status
    return _REGISTRY


def install_zip(zip_path: Path, *, replace: bool = False) -> Manifest:
    """Unpack a pack zip into <data_dir>/packs/<id>. Keeps license.json/config.json on upgrade."""
    if not zipfile.is_zipfile(zip_path):
        raise PackError(f"{zip_path} is not a zip file")
    packs_dir = get_settings().packs_dir
    packs_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf, tempfile.TemporaryDirectory(dir=packs_dir) as tmp:
        total = 0
        tmp_root = Path(tmp).resolve()
        for info in zf.infolist():
            total += info.file_size
            target = (tmp_root / info.filename).resolve()
            if total > MAX_ZIP_BYTES or tmp_root not in (target, *target.parents):
                raise PackError("zip is too large or contains unsafe paths")
        zf.extractall(tmp_root)
        candidates = [tmp_root] + [p for p in tmp_root.iterdir() if p.is_dir()]
        src = next((p for p in candidates if (p / MANIFEST).is_file()), None)
        if src is None:
            raise PackError(f"{MANIFEST} not found in zip")
        manifest = load_manifest(src)
        dest = packs_dir / manifest.id
        if dest.exists():
            if not replace:
                raise PackError(f"pack '{manifest.id}' is already installed (use --upgrade)")
            for keep in (lic.LICENSE_FILE, lic.CONFIG_FILE):
                if (dest / keep).is_file():
                    shutil.copy2(dest / keep, src / keep)
            shutil.rmtree(dest)
        shutil.move(str(src), str(dest))
    return load_manifest(dest)


def uninstall(pack_id: str) -> bool:
    dest = get_settings().packs_dir / pack_id
    if not (dest / MANIFEST).is_file():
        return False
    shutil.rmtree(dest)
    return True


def find(pack_id: str) -> Manifest:
    for m in installed_manifests():
        if m.id == pack_id:
            return m
    raise PackError(f"pack '{pack_id}' is not installed")
