"""Pack router prefixes, context providers and the shipped examples."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN_TOKEN, FakeLLM, build_pack

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

PREFIX_ENTRY = '''
from fastapi import APIRouter

def register(ctx):
    r = APIRouter()

    @r.get("/ping")
    def ping():
        return {"pong": True}
    ctx.add_router(r, prefix="/api/license/v1/")
'''

RESERVED_ENTRY = '''
from fastapi import APIRouter

def register(ctx):
    ctx.add_router(APIRouter(), prefix="/api/auth")
'''

PROVIDER_ENTRY = '''
SEEN = []

def register(ctx):
    def provide(request_context, user):
        SEEN.append(dict(request_context))
        return {"system": "PINNED CONTEXT " + str(request_context.get("pin")), "meta": {"pinned": True}}
    ctx.add_context_provider("web", provide)
'''


def _client(fake: FakeLLM | None = None) -> TestClient:
    from zkshizen.server import create_app
    return TestClient(create_app(llm_override=(lambda: fake.client()) if fake else None),
                      headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})


def _sse(text: str) -> list[dict]:
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]


def test_custom_router_prefix(tmp_path: Path) -> None:
    from zkshizen.packs import loader
    loader.install_zip(build_pack(tmp_path, license_required=False, entry_code=PREFIX_ENTRY))
    c = _client()
    assert c.get("/api/license/v1/ping").json() == {"pong": True}
    assert c.get("/api/packs/demo/ping").status_code == 404


@pytest.mark.parametrize("prefix", ["/api/auth", "/api/auth/x", "/api/chat", "/api/packs/other", "/license", "/api"])
def test_reserved_or_invalid_prefix_raises(tmp_path: Path, prefix: str) -> None:
    from fastapi import APIRouter

    from zkshizen.packs import loader
    from zkshizen.packs.manifest import load_manifest
    m = loader.install_zip(build_pack(tmp_path, license_required=False))
    ctx = loader.PackContext(load_manifest(m.root), {}, None)
    with pytest.raises(loader.PackError):
        ctx.add_router(APIRouter(), prefix=prefix)


def test_reserved_prefix_fails_pack_load(tmp_path: Path) -> None:
    from zkshizen.packs import loader
    loader.install_zip(build_pack(tmp_path, license_required=False, entry_code=RESERVED_ENTRY))
    status = loader.load_all().status[0]
    assert not status.loaded and "PackError" in status.reason


def test_context_provider_gets_session_and_text(tmp_path: Path, fake_llm: FakeLLM) -> None:
    from zkshizen.packs import loader
    loader.install_zip(build_pack(tmp_path, license_required=False, entry_code=PROVIDER_ENTRY))
    fake_llm.script = [{"text": "Fine."}]
    c = _client(fake_llm)
    body = {"messages": [{"role": "user", "content": "what is pinned?"}], "context": {"pin": "A-7"}}
    with c.stream("POST", "/api/chat/web", json=body) as r:
        events = _sse(r.read().decode())
    seen = sys.modules["demo_pack"].SEEN
    assert len(seen) == 1 and seen[0]["pin"] == "A-7"
    assert seen[0]["last_user_text"] == "what is pinned?"
    assert seen[0]["session_id"] and seen[0]["session_id"] == events[-1]["session_id"]
    assert "PINNED CONTEXT A-7" in fake_llm.requests[0]["messages"][0]["content"]
    assert events[0]["type"] == "meta" and events[0]["pinned"] is True


def test_example_pack_installs_and_serves(tmp_path: Path, fake_llm: FakeLLM) -> None:
    from zkshizen.agent.channels import build_registry
    from zkshizen.packs import loader
    src = EXAMPLES / "packs" / "hello-pack"
    zpath = tmp_path / "hello-pack.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        for p in src.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts:
                zf.write(p, f"hello-pack/{p.relative_to(src).as_posix()}")
    m = loader.install_zip(zpath)
    assert m.id == "hello" and not m.license.required
    c = _client(fake_llm)
    assert c.get("/api/packs/hello/hello").json() == {"message": "hello from hello-pack"}
    assert "hello_greet" in build_registry("web").names
    assert "hello_greet" not in build_registry("admin").names


def test_example_skill_parses() -> None:
    from zkshizen.agent import skills
    skill = skills._skill_from_dir(EXAMPLES / "skills" / "hello-skill", "example")
    assert skill is not None and skill.name == "hello-skill"
    assert set(skill.channels) == {"web", "cli"}
