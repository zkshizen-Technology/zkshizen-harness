"""Runtime settings, resolved from environment variables with per-OS defaults.

Data directory (database, packs, skills, LLM profiles, sessions):
  Linux   $XDG_DATA_HOME/zkshizen or ~/.local/share/zkshizen
  macOS   ~/Library/Application Support/zkshizen
  Windows %LOCALAPPDATA%\\zkshizen
Override with ZKS_DATA_DIR. Tests call get_settings.cache_clear() after
changing the environment.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

APP_NAME = "zkshizen"


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    host: str
    port: int
    # Bootstrap admin bearer token; when unset, `zks init` / first start generates one.
    admin_token: str | None
    cors_origins: tuple[str, ...]
    max_steps_web: int
    max_steps_admin: int
    max_steps_cli: int
    # Seconds between license checks for installed packs (config sync).
    license_check_interval_s: int
    token_ttl_hours: int

    @property
    def db_path(self) -> Path:
        return Path(os.environ.get("ZKS_DB_PATH") or self.data_dir / "zkshizen.db")

    @property
    def packs_dir(self) -> Path:
        return self.data_dir / "packs"

    @property
    def skills_dir(self) -> Path:
        """Skills installed with `install_skill` (CLI agent) or by hand."""
        return self.data_dir / "skills"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "cli-sessions"

    @property
    def llm_config_path(self) -> Path:
        return self.data_dir / "llm.json"

    @property
    def machine_id_path(self) -> Path:
        return self.data_dir / "machine_id"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.path.expanduser(os.environ.get("ZKS_DATA_DIR") or default_data_dir()))
    origins = os.environ.get("ZKS_CORS_ORIGINS", "")
    return Settings(
        data_dir=data_dir,
        host=os.environ.get("ZKS_HOST", "127.0.0.1"),
        port=_int("ZKS_PORT", 8787),
        admin_token=os.environ.get("ZKS_ADMIN_TOKEN") or None,
        cors_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
        max_steps_web=_int("ZKS_MAX_STEPS_WEB", 8),
        max_steps_admin=_int("ZKS_MAX_STEPS_ADMIN", 20),
        max_steps_cli=_int("ZKS_MAX_STEPS_CLI", 40),
        license_check_interval_s=_int("ZKS_LICENSE_CHECK_S", 300),
        token_ttl_hours=_int("ZKS_TOKEN_TTL_HOURS", 24 * 30),
    )


def machine_id() -> str:
    """Random per-installation id, created once. Used for license activation."""
    import uuid

    path = get_settings().machine_id_path
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    path.parent.mkdir(parents=True, exist_ok=True)
    value = str(uuid.uuid4())
    path.write_text(value, encoding="utf-8")
    return value
