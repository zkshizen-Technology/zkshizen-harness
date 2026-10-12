"""Shared fixtures: isolated data dir per test, fake LLM, signed test packs."""

from __future__ import annotations

import base64
import datetime as dt
import json
import textwrap
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ADMIN_TOKEN = "test-admin-token"


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    d = tmp_path / "zks-data"
    monkeypatch.setenv("ZKS_DATA_DIR", str(d))
    monkeypatch.setenv("ZKS_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("ZKS_LICENSE_CHECK_S", "0")
    for var in ("ZKS_DB_PATH", "ZKS_CORS_ORIGINS", "LLM_PROFILE", "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL",
                "LLM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ZKS_YOLO"):
        monkeypatch.delenv(var, raising=False)
    from zkshizen.config import get_settings
    get_settings.cache_clear()
    yield d
    from zkshizen.agent import skills
    from zkshizen.packs import loader
    skills.clear_skill_roots()
    loader.registry().contexts.clear()
    loader.registry().status.clear()
    get_settings.cache_clear()


# ─── Fake OpenAI-compatible LLM ──────────────────────────────────────

@dataclass
class FakeLLM:
    """Each streamed turn pops a script entry: {"text": str} or {"tool_calls": [(name, args)]}."""
    script: list[dict[str, Any]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    down: bool = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("down", request=request)
        body = json.loads(request.content)
        self.requests.append(body)
        turn = self.script.pop(0) if self.script else {"text": "ok"}
        if not body.get("stream"):
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                                        "content": turn.get("text", "")}}]})
        chunks = []
        if turn.get("text"):
            chunks.append({"choices": [{"delta": {"content": turn["text"]}}]})
        for i, (name, args) in enumerate(turn.get("tool_calls") or []):
            chunks.append({"choices": [{"delta": {"tool_calls": [{"index": i, "id": f"call_{i}", "type": "function",
                                                                  "function": {"name": name,
                                                                               "arguments": json.dumps(args)}}]}}]})
        text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    def client(self):
        from zkshizen.llm.client import LLMClient
        return LLMClient(base_url="http://llm.test/v1", api_key="k", model="fake",
                         transport=httpx.MockTransport(self.handler))


@pytest.fixture()
def fake_llm() -> FakeLLM:
    return FakeLLM()


# ─── Signing helpers (what a license server does) ────────────────────

@dataclass
class Signer:
    key: Ed25519PrivateKey
    kid: str = "ktest"

    @property
    def public_b64(self) -> str:
        raw = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def envelope(self, payload: dict[str, Any]) -> dict[str, Any]:
        from zkshizen.packs.license import b64u_encode, canonical
        body = canonical(payload)
        return {"kid": self.kid, "payload": b64u_encode(body), "sig": b64u_encode(self.key.sign(body))}

    def license_for(self, config: dict[str, Any], *, product: str = "demo", machine: str | None = None,
                    days: int = 30, features: list[str] | None = None, version: int = 1) -> dict[str, Any]:
        from zkshizen.config import machine_id
        from zkshizen.packs.license import config_digest
        now = dt.datetime.now(dt.timezone.utc)
        return self.envelope({
            "license_id": "lic-1", "product": product, "customer": "Test Customer",
            "machine_id": machine or machine_id(), "issued_at": now.isoformat(),
            "expires_at": (now + dt.timedelta(days=days)).isoformat(), "grace_days": 7,
            "features": features or ["chat"], "config_version": version, "config_sha256": config_digest(config),
        })


@pytest.fixture()
def signer() -> Signer:
    return Signer(Ed25519PrivateKey.generate())


DEMO_ENTRY = '''
from zkshizen.agent.registry import Tool
from fastapi import APIRouter

def register(ctx):
    ctx.add_tools("web", [Tool("demo_site", "Return the licensed site name.",
                               {"type": "object", "properties": {"x": {"type": "string"}}},
                               lambda a: {"site": ctx.config.get("site_name")})])
    ctx.add_system_prompt("web", "DEMO PACK PROMPT")
    ctx.add_output_filter("web", lambda t: t.replace("SECRET", "[removed]"))
    r = APIRouter()

    @r.get("/hello")
    def hello():
        return {"site": ctx.config.get("site_name")}
    ctx.add_router(r)
    ctx.add_migration("001", "CREATE TABLE IF NOT EXISTS demo_items (id INTEGER PRIMARY KEY);")
'''


def build_pack(tmp: Path, *, pack_id: str = "demo", license_required: bool = True, public_key: str = "",
               entry_code: str = DEMO_ENTRY, extra_files: dict[str, str] | None = None) -> Path:
    root = tmp / f"src-{pack_id}"
    (root / "python" / "demo_pack").mkdir(parents=True)
    (root / "python" / "demo_pack" / "__init__.py").write_text(textwrap.dedent(entry_code), encoding="utf-8")
    (root / "skills" / "demo-skill").mkdir(parents=True)
    (root / "skills" / "demo-skill" / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill for web.\nchannels: [web]\n---\nBody.", encoding="utf-8")
    (root / "web").mkdir()
    (root / "web" / "index.html").write_text("<!doctype html><title>demo</title>demo web", encoding="utf-8")
    lic = (f'[license]\nrequired = {"true" if license_required else "false"}\nproduct = "demo"\n'
           f'server = "http://127.0.0.1:9"\npublic_keys = {{ ktest = "{public_key}" }}\n') if license_required else ""
    (root / "pack.toml").write_text(
        f'id = "{pack_id}"\nname = "Demo"\nversion = "1.0.0"\nentry = "demo_pack:register"\n'
        f'python_path = "python"\nskills = "skills"\nweb_dir = "web"\nweb_mount = "/demo"\n{lic}', encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content, encoding="utf-8")
    zpath = tmp / f"{pack_id}.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in root.rglob("*"):
            if p.is_file():
                zf.write(p, f"{pack_id}/{p.relative_to(root).as_posix()}")
    return zpath
