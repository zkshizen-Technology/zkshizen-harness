"""License verification, activation and periodic check for licensed packs.

A license is a signed envelope issued by a zkshizen license server:

    {"kid": "k2026a", "payload": "<base64url JSON>", "sig": "<base64url Ed25519 signature>"}

The payload binds the license to one product and one machine and carries the
SHA-256 of the config bundle delivered with it, so the config cannot be
edited locally without breaking verification:

    {"license_id", "product", "customer", "machine_id", "issued_at", "expires_at",
     "grace_days", "features", "config_version", "config_sha256"}

The private signing key never leaves the license server. Packs ship the
matching public keys (several allowed, for key rotation).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from zkshizen import __version__
from zkshizen.config import machine_id
from zkshizen.packs.manifest import Manifest

LICENSE_FILE = "license.json"
CONFIG_FILE = "config.json"
API_PREFIX = "/api/license/v1"


class LicenseError(ValueError):
    pass


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def config_digest(config: Any) -> str:
    return hashlib.sha256(canonical(config)).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_ts(value: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


@dataclass(frozen=True, slots=True)
class LicenseState:
    valid: bool
    status: str  # active | grace | expired | invalid | missing
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        p = self.payload
        return {"valid": self.valid, "status": self.status, "message": self.message,
                "license_id": p.get("license_id"), "customer": p.get("customer"), "product": p.get("product"),
                "expires_at": p.get("expires_at"), "config_version": p.get("config_version"),
                "features": p.get("features", [])}


def verify_envelope(envelope: dict[str, Any], public_keys: dict[str, str]) -> dict[str, Any]:
    """Return the payload if the signature is valid for one of the pack's keys."""
    kid = str(envelope.get("kid", ""))
    if kid not in public_keys:
        raise LicenseError(f"license signed with unknown key '{kid}'")
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_keys[kid]))
        payload_bytes = b64u_decode(str(envelope["payload"]))
        key.verify(b64u_decode(str(envelope["sig"])), payload_bytes)
    except (KeyError, ValueError, InvalidSignature) as exc:
        raise LicenseError("license signature is invalid") from exc
    return json.loads(payload_bytes)


def evaluate(manifest: Manifest, envelope: dict[str, Any] | None, config: dict[str, Any] | None,
             at: dt.datetime | None = None) -> LicenseState:
    if not envelope:
        return LicenseState(False, "missing", "pack is not activated (zks pack activate <id> --key ...)")
    try:
        payload = verify_envelope(envelope, manifest.license.public_keys)
    except LicenseError as exc:
        return LicenseState(False, "invalid", str(exc))
    if payload.get("product") != manifest.license.product:
        return LicenseState(False, "invalid", "license is for a different product", payload)
    if payload.get("machine_id") != machine_id():
        return LicenseState(False, "invalid", "license is bound to another machine", payload)
    if payload.get("config_sha256") and config_digest(config or {}) != payload["config_sha256"]:
        return LicenseState(False, "invalid", "licensed config was modified locally", payload)
    now = at or _now()
    expires = _parse_ts(str(payload["expires_at"]))
    grace = dt.timedelta(days=int(payload.get("grace_days", 14)))
    if now <= expires:
        return LicenseState(True, "active", "license active", payload, config or {})
    if now <= expires + grace:
        return LicenseState(True, "grace", f"license expired {expires:%Y-%m-%d}; grace period until "
                                           f"{expires + grace:%Y-%m-%d}. Renew or run `zks pack sync`.", payload, config or {})
    return LicenseState(False, "expired", f"license expired on {expires:%Y-%m-%d}", payload)


# ─── Local storage ────────────────────────────────────────────────────

def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def load_local(manifest: Manifest) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    return _read_json(manifest.root / LICENSE_FILE), _read_json(manifest.root / CONFIG_FILE)


def store_local(manifest: Manifest, envelope: dict[str, Any], config: dict[str, Any], server: str) -> None:
    for name, data in ((CONFIG_FILE, config), (LICENSE_FILE, envelope | {"server": server})):
        tmp = manifest.root / (name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(manifest.root / name)


def local_state(manifest: Manifest) -> LicenseState:
    envelope, config = load_local(manifest)
    if envelope:
        envelope = {k: envelope[k] for k in ("kid", "payload", "sig") if k in envelope}
    return evaluate(manifest, envelope, config)


# ─── Server calls ─────────────────────────────────────────────────────

def _post(server: str, path: str, body: dict[str, Any], transport: httpx.BaseTransport | None) -> dict[str, Any]:
    if not server.startswith("https://") and not server.startswith(("http://127.0.0.1", "http://localhost")):
        raise LicenseError("license server must use https:// (http only for localhost testing)")
    try:
        with httpx.Client(timeout=30, transport=transport) as client:
            resp = client.post(server.rstrip("/") + API_PREFIX + path, json=body)
    except httpx.HTTPError as exc:
        raise LicenseError(f"license server unreachable: {exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        raise LicenseError(str(data.get("detail") or f"license server HTTP {resp.status_code}"))
    return data


def _accept(manifest: Manifest, data: dict[str, Any], server: str) -> LicenseState:
    envelope, config = data.get("license"), data.get("config")
    if not isinstance(envelope, dict) or not isinstance(config, dict):
        raise LicenseError("license server returned an unexpected response")
    state = evaluate(manifest, envelope, config)
    if not state.valid:
        raise LicenseError(f"server license rejected locally: {state.message}")
    store_local(manifest, envelope, config, server)
    return state


def activate(manifest: Manifest, key: str, server: str | None = None,
             transport: httpx.BaseTransport | None = None) -> LicenseState:
    server = (server or manifest.license.server).rstrip("/")
    if not server:
        raise LicenseError("no license server configured; pass --server")
    data = _post(server, "/activate", {"license_key": key.strip(), "product": manifest.license.product,
                                       "machine_id": machine_id(), "pack_version": manifest.version,
                                       "core_version": __version__}, transport)
    return _accept(manifest, data, server)


def check(manifest: Manifest, transport: httpx.BaseTransport | None = None) -> tuple[LicenseState, bool]:
    """Renew and pull config changes. Returns (state, config_changed)."""
    envelope, _config = load_local(manifest)
    if not envelope:
        raise LicenseError("pack is not activated")
    server = str(envelope.get("server") or manifest.license.server)
    payload = json.loads(b64u_decode(str(envelope["payload"])))
    data = _post(server, "/check", {"license_id": payload.get("license_id"), "machine_id": machine_id(),
                                    "config_version": payload.get("config_version"),
                                    "pack_version": manifest.version}, transport)
    if data.get("status") == "revoked":
        (manifest.root / LICENSE_FILE).unlink(missing_ok=True)
        return LicenseState(False, "invalid", "license was revoked by the vendor"), False
    state = _accept(manifest, data, server)
    return state, state.payload.get("config_version") != payload.get("config_version")
