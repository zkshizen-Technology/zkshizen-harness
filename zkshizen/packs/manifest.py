"""Pack manifest (pack.toml).

    id = "hello"                     # lowercase, digits, dashes
    name = "Hello Pack"
    version = "1.0.0"
    min_core = "0.1.0"
    entry = "hello_pack:register"    # module:function, imported from python_path
    python_path = "python"           # relative dir added to sys.path
    skills = "skills"                # relative dir of SKILL.md folders (optional)
    web_dir = "web"                  # static files served at web_mount (optional)
    web_mount = "/hello"

    [license]
    required = true
    product = "hello"
    server = "https://license.example.com"   # default activation server, overridable
    public_keys = { k2026a = "<base64 Ed25519 public key>" }   # several = key rotation
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_MOUNT_RE = re.compile(r"^/[A-Za-z0-9_\-/]{1,60}$")
RESERVED_MOUNTS = ("/api", "/docs", "/redoc", "/openapi.json")
MANIFEST = "pack.toml"


class ManifestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LicenseSpec:
    required: bool = False
    product: str = ""
    server: str = ""
    public_keys: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Manifest:
    id: str
    name: str
    version: str
    root: Path
    entry: str | None = None
    python_path: str | None = None
    skills: str | None = None
    web_dir: str | None = None
    web_mount: str | None = None
    min_core: str = "0.0.0"
    license: LicenseSpec = field(default_factory=LicenseSpec)

    @property
    def skills_path(self) -> Path | None:
        return self.root / self.skills if self.skills else None

    @property
    def web_path(self) -> Path | None:
        return self.root / self.web_dir if self.web_dir else None

    @property
    def python_dir(self) -> Path | None:
        return self.root / self.python_path if self.python_path else None


def _rel(root: Path, value: object, key: str) -> str | None:
    if value in (None, ""):
        return None
    rel = str(value)
    target = (root / rel).resolve()
    if root.resolve() not in (target, *target.parents):
        raise ManifestError(f"{key} must stay inside the pack")
    return rel


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3]) or (0,)


def load_manifest(root: Path) -> Manifest:
    path = root / MANIFEST
    if not path.is_file():
        raise ManifestError(f"{MANIFEST} not found in {root}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"invalid {MANIFEST}: {exc}") from exc
    pack_id = str(data.get("id", ""))
    if not _ID_RE.match(pack_id):
        raise ManifestError("id must be lowercase letters, digits and dashes (max 40)")
    mount = data.get("web_mount")
    if mount is not None:
        mount = str(mount).rstrip("/") or "/"
        if not _MOUNT_RE.match(mount) or any(mount == r or mount.startswith(r + "/") for r in RESERVED_MOUNTS):
            raise ManifestError(f"web_mount {mount!r} is invalid or reserved")
    entry = data.get("entry")
    if entry is not None and not re.match(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$", str(entry)):
        raise ManifestError("entry must look like 'module:function'")
    lic = data.get("license") or {}
    keys = lic.get("public_keys") or {}
    if not isinstance(keys, dict):
        raise ManifestError("license.public_keys must be a table of key_id = base64 key")
    spec = LicenseSpec(required=bool(lic.get("required", False)), product=str(lic.get("product", "")),
                       server=str(lic.get("server", "")).rstrip("/"), public_keys={str(k): str(v) for k, v in keys.items()})
    if spec.required and not spec.public_keys:
        raise ManifestError("license.required needs at least one public key")
    return Manifest(
        id=pack_id, name=str(data.get("name") or pack_id), version=str(data.get("version", "0.0.0")), root=root,
        entry=str(entry) if entry else None,
        python_path=_rel(root, data.get("python_path"), "python_path"),
        skills=_rel(root, data.get("skills"), "skills"),
        web_dir=_rel(root, data.get("web_dir"), "web_dir"),
        web_mount=mount, min_core=str(data.get("min_core", "0.0.0")), license=spec,
    )
