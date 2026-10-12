"""LLM provider profiles: which API, endpoint, key and default model to use.

Stored in <ZKS_DATA_DIR>/llm.json (mode 0600 on Linux/macOS), managed with
`zks config ...`. Read on every request, so changes apply without a restart.

    {
      "default": "anthropic",
      "profiles": {
        "anthropic": {"provider": "anthropic", "base_url": "https://api.anthropic.com",
                      "model": "claude-sonnet-5-5", "api_key": "sk-ant-..."},
        "openai":    {"provider": "openai", "base_url": "https://api.openai.com/v1",
                      "model": "<model>", "api_key_env": "OPENAI_API_KEY"}
      }
    }

Precedence (highest first): environment variables LLM_PROVIDER / LLM_BASE_URL /
LLM_MODEL / LLM_API_KEY, then the selected profile (LLM_PROFILE or "default"),
then built-in defaults. A profile key may be stored inline (api_key) or read
from another environment variable (api_key_env); with neither, the provider's
standard variable is used (ANTHROPIC_API_KEY / OPENAI_API_KEY).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from zkshizen.config import get_settings

Provider = Literal["openai", "anthropic"]
PROVIDERS: tuple[str, ...] = ("openai", "anthropic")
TOKEN_PARAMS = ("max_tokens", "max_completion_tokens")
AUTH_MODES = ("x-api-key", "bearer")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")

PROVIDER_DEFAULTS: dict[str, dict[str, str]] = {
    "anthropic": {"base_url": "https://api.anthropic.com", "model": "claude-sonnet-5-5",
                  "key_env": "ANTHROPIC_API_KEY"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "", "key_env": "OPENAI_API_KEY"},
}
NOT_CONFIGURED = ("no LLM configured. Run: zks config set anthropic --provider anthropic --api-key - --default "
                  "(or --provider openai --model <model>)")


class LLMConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LLMProfile:
    name: str
    provider: Provider
    base_url: str
    model: str
    api_key: str
    timeout_s: float = 60.0
    token_param: str = "max_tokens"  # OpenAI reasoning models need max_completion_tokens
    # Anthropic only: "x-api-key" (api.anthropic.com) or "bearer" (Authorization: Bearer,
    # what Anthropic SDK clients send for ANTHROPIC_AUTH_TOKEN; used by some Anthropic-compatible proxies).
    auth: str = "x-api-key"

    def masked(self) -> dict[str, Any]:
        d = asdict(self)
        d["api_key"] = mask(self.api_key)
        return d


def mask(key: str) -> str:
    if not key:
        return "(not set)"
    return key[:4] + "…" + key[-4:] if len(key) > 12 else "****"


def config_path() -> Path:
    return get_settings().llm_config_path


def load_file() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {"default": None, "profiles": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LLMConfigError(f"cannot read {path}: {exc}") from exc
    data.setdefault("profiles", {})
    data.setdefault("default", None)
    return data


def save_file(data: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    if os.name != "nt":
        os.chmod(tmp, 0o600)  # holds API keys
    tmp.replace(path)
    return path


def _validate_profile(name: str, raw: dict[str, Any]) -> None:
    if not _PROFILE_RE.match(name):
        raise LLMConfigError("profile name: letters, digits, . _ - (max 40)")
    if raw.get("provider") not in PROVIDERS:
        raise LLMConfigError(f"provider must be one of {', '.join(PROVIDERS)}")
    if raw.get("token_param", "max_tokens") not in TOKEN_PARAMS:
        raise LLMConfigError(f"token_param must be one of {', '.join(TOKEN_PARAMS)}")
    if raw.get("auth", "x-api-key") not in AUTH_MODES:
        raise LLMConfigError(f"auth must be one of {', '.join(AUTH_MODES)}")
    url = str(raw.get("base_url") or "")
    if url and not re.match(r"^https?://", url):
        raise LLMConfigError("base_url must start with http:// or https://")


def upsert_profile(name: str, fields: dict[str, Any], make_default: bool = False) -> dict[str, Any]:
    data = load_file()
    current = dict(data["profiles"].get(name) or {})
    current.update({k: v for k, v in fields.items() if v is not None})
    if "api_key" in fields and fields["api_key"] is not None:
        current.pop("api_key_env", None)  # an inline key replaces an env reference
    if "api_key_env" in fields and fields["api_key_env"] is not None:
        current.pop("api_key", None)
    _validate_profile(name, current)
    data["profiles"][name] = current
    if make_default or not data.get("default"):
        data["default"] = name
    save_file(data)
    return current


def set_default(name: str) -> None:
    data = load_file()
    if name not in data["profiles"]:
        raise LLMConfigError(f"profile '{name}' does not exist")
    data["default"] = name
    save_file(data)


def remove_profile(name: str) -> bool:
    data = load_file()
    if data["profiles"].pop(name, None) is None:
        return False
    if data.get("default") == name:
        data["default"] = next(iter(data["profiles"]), None)
    save_file(data)
    return True


def resolve(profile: str | None = None, model: str | None = None) -> LLMProfile:
    """Effective settings for one call. `profile`/`model` are per-call overrides."""
    data = load_file()
    env = os.environ
    name = profile or env.get("LLM_PROFILE") or data.get("default")
    raw: dict[str, Any]
    if name:
        if name not in data["profiles"]:
            raise LLMConfigError(f"LLM profile '{name}' not found in {config_path()}")
        raw = dict(data["profiles"][name])
    elif any(env.get(k) for k in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")):
        name, raw = "env", {"provider": env.get("LLM_PROVIDER", "openai")}
    else:
        raise LLMConfigError(NOT_CONFIGURED)

    provider = env.get("LLM_PROVIDER") or raw.get("provider") or "openai"
    if provider not in PROVIDERS:
        raise LLMConfigError(f"unknown provider '{provider}'")
    defaults = PROVIDER_DEFAULTS[provider]
    api_key = (env.get("LLM_API_KEY") or raw.get("api_key")
               or env.get(str(raw.get("api_key_env") or ""), "") or env.get(defaults["key_env"], ""))
    resolved_model = (model or env.get("LLM_MODEL") or raw.get("model") or defaults["model"])
    if not resolved_model:
        raise LLMConfigError(f"no model set for profile '{name}' (zks config set {name} --model ...)")
    return LLMProfile(
        name=name,
        provider=provider,  # type: ignore[arg-type]
        base_url=(env.get("LLM_BASE_URL") or raw.get("base_url") or defaults["base_url"]).rstrip("/"),
        model=resolved_model,
        api_key=api_key,
        timeout_s=float(env.get("LLM_TIMEOUT_S") or raw.get("timeout_s") or 60),
        token_param=str(raw.get("token_param") or "max_tokens"),
        auth=str(raw.get("auth") or "x-api-key"),
    )
