"""Core harness: auth, 2FA, channels, packs, licenses, chat filters."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN_TOKEN, FakeLLM, Signer, build_pack

HOST_TOOLS = {"run_shell", "read_file", "write_file", "edit_file", "list_dir", "install_skill", "uninstall_skill"}


def _client(fake: FakeLLM | None = None) -> TestClient:
    from zkshizen.server import create_app
    return TestClient(create_app(llm_override=(lambda: fake.client()) if fake else None),
                      headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})


def _sse(text: str) -> list[dict]:
    return [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]


# ─── Auth ─────────────────────────────────────────────────────────────

def test_password_and_totp_primitives() -> None:
    from zkshizen import auth
    h = auth.hash_password("correct horse battery")
    assert auth.verify_password("correct horse battery", h) and not auth.verify_password("wrong", h)
    with pytest.raises(auth.AuthError):
        auth.hash_password("short")
    secret = auth.new_totp_secret()
    now = 1_800_000_000.0
    assert auth.verify_totp(secret, auth.totp_now(secret, now), at=now)
    assert auth.verify_totp(secret, auth.totp_now(secret, now - 30), at=now)  # one step of clock skew
    assert not auth.verify_totp(secret, auth.totp_now(secret, now - 120), at=now)
    # RFC 6238 test vector (SHA-1, T=59 -> 94287082, last 6 digits)
    rfc_secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert auth.totp_now(rfc_secret, 59) == "287082"


def test_login_flow_and_admin_requires_2fa() -> None:
    from zkshizen import auth, db
    c = _client()
    conn = db.connect()
    auth.create_user(conn, "supt", "field-password-1", role="user")
    boss = auth.create_user(conn, "boss", "admin-password-1", role="admin")
    anon = TestClient(c.app)
    assert anon.get("/api/me").status_code == 401
    r = anon.post("/api/auth/login", json={"username": "supt", "password": "nope"})
    assert r.status_code == 401
    r = anon.post("/api/auth/login", json={"username": "supt", "password": "field-password-1"})
    assert r.status_code == 200 and anon.get("/api/me").json()["username"] == "supt"
    # cookie session: unsafe requests need the CSRF header
    assert anon.post("/api/chat/web", json={"messages": [{"role": "user", "content": "x"}]}).status_code == 403
    assert anon.post("/api/auth/login", json={"username": "boss", "password": "admin-password-1"}).status_code == 403
    secret = auth.new_totp_secret()
    auth.set_totp_secret(conn, boss.id, secret)
    assert anon.post("/api/auth/login", json={"username": "boss", "password": "admin-password-1",
                                               "totp": "000000"}).status_code == 401
    r = anon.post("/api/auth/login", json={"username": "boss", "password": "admin-password-1",
                                           "totp": auth.totp_now(secret)})
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"


def test_login_throttle() -> None:
    from zkshizen import server
    server._FAILS.clear()
    anon = TestClient(_client().app)
    for _ in range(5):
        anon.post("/api/auth/login", json={"username": "x", "password": "y"})
    assert anon.post("/api/auth/login", json={"username": "x", "password": "y"}).status_code == 429
    server._FAILS.clear()


# ─── Channels ────────────────────────────────────────────────────────

def test_channel_isolation() -> None:
    from zkshizen.agent.channels import build_registry
    from zkshizen.packs.loader import load_all
    load_all()
    assert not set(build_registry("web").names) & HOST_TOOLS
    assert not set(build_registry("admin").names) & HOST_TOOLS
    assert HOST_TOOLS <= set(build_registry("cli").names)


def test_admin_channel_requires_admin_role(fake_llm: FakeLLM) -> None:
    from zkshizen import auth, db
    c = _client(fake_llm)
    conn = db.connect()
    u = auth.create_user(conn, "supt", "field-password-1")
    tok = auth.issue_token(conn, u.id, "t")
    r = c.post("/api/chat/admin", headers={"Authorization": f"Bearer {tok}"},
               json={"messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 403


def test_every_tool_has_required_args() -> None:
    """Through some proxies a tool_use with empty input {} returns an empty body."""
    from zkshizen.agent.channels import build_registry
    from zkshizen.packs.loader import load_all
    load_all()
    for ch in ("web", "admin", "cli"):
        for spec in build_registry(ch).specs():
            assert spec["function"]["parameters"].get("required"), spec["function"]["name"]


# ─── Packs + licenses ────────────────────────────────────────────────

def _install(tmp_path: Path, signer: Signer, **kw):
    from zkshizen.packs import loader
    return loader.install_zip(build_pack(tmp_path, public_key=signer.public_b64, **kw))


def test_licensed_pack_lifecycle(tmp_path: Path, signer: Signer, fake_llm: FakeLLM) -> None:
    from zkshizen.packs import license as lic
    from zkshizen.packs import loader
    m = _install(tmp_path, signer)
    reg = loader.load_all()
    assert not reg.status[0].loaded and "not activated" in reg.status[0].reason

    config = {"site_name": "Test Site"}
    envelope = signer.license_for(config)

    def server(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert req.url.path.endswith("/activate") and body["license_key"] == "ZKS-TEST"
        return httpx.Response(200, json={"license": envelope, "config": config})
    state = lic.activate(m, "ZKS-TEST", transport=httpx.MockTransport(server))
    assert state.valid and state.status == "active"

    c = _client(fake_llm)
    assert c.get("/api/packs/demo/hello").json() == {"site": "Test Site"}
    assert c.get("/demo/").status_code == 200
    assert any(p["id"] == "demo" and p["loaded"] for p in c.get("/api/packs").json()["packs"])

    # tampering with the licensed config breaks verification
    cfg_path = m.root / lic.CONFIG_FILE
    cfg_path.write_text(json.dumps({"site_name": "Hacked"}), encoding="utf-8")
    assert lic.local_state(m).status == "invalid"
    cfg_path.write_text(json.dumps(config), encoding="utf-8")
    assert lic.local_state(m).valid


def test_license_rejections(tmp_path: Path, signer: Signer) -> None:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from zkshizen.packs import license as lic
    m = _install(tmp_path, signer)
    cfg = {"site_name": "X"}
    other = Signer(Ed25519PrivateKey.generate())
    assert lic.evaluate(m, other.license_for(cfg), cfg).status == "invalid"                    # wrong key
    assert lic.evaluate(m, signer.license_for(cfg, product="other"), cfg).status == "invalid"  # wrong product
    assert lic.evaluate(m, signer.license_for(cfg, machine="m2"), cfg).status == "invalid"     # other machine
    expired = signer.license_for(cfg, days=-3)
    assert lic.evaluate(m, expired, cfg).status == "grace"
    assert lic.evaluate(m, expired, cfg, at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).status == "expired"
    other_kid = Signer(signer.key, kid="unknown")
    assert "unknown key" in lic.evaluate(m, other_kid.license_for(cfg), cfg).message


def test_license_check_pulls_new_config_and_revocation(tmp_path: Path, signer: Signer) -> None:
    from zkshizen.packs import license as lic
    m = _install(tmp_path, signer)
    v1 = {"site_name": "One"}
    lic.store_local(m, signer.license_for(v1), v1, "http://127.0.0.1:9")
    v2 = {"site_name": "Two"}

    def renew(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "active", "license": signer.license_for(v2, version=2), "config": v2})
    state, changed = lic.check(m, transport=httpx.MockTransport(renew))
    assert changed and state.config["site_name"] == "Two"

    def revoke(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "revoked"})
    state, _ = lic.check(m, transport=httpx.MockTransport(revoke))
    assert not state.valid and not (m.root / lic.LICENSE_FILE).exists()


def test_unlicensed_pack_and_skill_roots(tmp_path: Path, signer: Signer) -> None:
    from zkshizen.agent import skills
    from zkshizen.agent.channels import build_registry
    from zkshizen.packs import loader
    _install(tmp_path, signer, license_required=False)
    loader.load_all()
    assert "demo-skill" in {s.name for s in skills.for_channel("web")}
    assert "demo-skill" not in {s.name for s in skills.for_channel("admin")}
    assert "demo_site" in build_registry("web").names


def test_zip_slip_rejected(tmp_path: Path) -> None:
    from zkshizen.packs import loader
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("pack.toml", 'id = "evil"\nname = "e"\nversion = "1"\n')
        zf.writestr("../../escape.txt", "x")
    with pytest.raises(loader.PackError):
        loader.install_zip(bad)


def test_upgrade_keeps_license(tmp_path: Path, signer: Signer) -> None:
    from zkshizen.packs import license as lic
    from zkshizen.packs import loader
    m = _install(tmp_path, signer)
    cfg = {"site_name": "Keep"}
    lic.store_local(m, signer.license_for(cfg), cfg, "http://127.0.0.1:9")
    with pytest.raises(loader.PackError):
        loader.install_zip(build_pack(tmp_path / "again", public_key=signer.public_b64))
    m2 = loader.install_zip(build_pack(tmp_path / "up", public_key=signer.public_b64), replace=True)
    assert lic.local_state(m2).valid


# ─── Chat ─────────────────────────────────────────────────────────────

def test_web_chat_uses_pack_prompt_tool_and_filter(tmp_path: Path, signer: Signer, fake_llm: FakeLLM) -> None:
    _install(tmp_path, signer, license_required=False)
    fake_llm.script = [{"tool_calls": [("demo_site", {"x": "1"})]}, {"text": "The SECRET site. Done."}]
    c = _client(fake_llm)
    with c.stream("POST", "/api/chat/web", json={"messages": [{"role": "user", "content": "which site?"}]}) as r:
        events = _sse(r.read().decode())
    assert "DEMO PACK PROMPT" in fake_llm.requests[0]["messages"][0]["content"]
    tool_names = {t["function"]["name"] for t in fake_llm.requests[0]["tools"]}
    assert "demo_site" in tool_names and not tool_names & HOST_TOOLS
    text = "".join(e["content"] for e in events if e["type"] == "token")
    assert text == "The [removed] site. Done." and events[-1]["type"] == "done"


def test_chat_llm_down(fake_llm: FakeLLM) -> None:
    fake_llm.down = True
    with _client(fake_llm).stream("POST", "/api/chat/web", json={"messages": [{"role": "user", "content": "x"}]}) as r:
        events = _sse(r.read().decode())
    assert any(e["type"] == "error" and e.get("offline") for e in events) and events[-1]["type"] == "done"


def test_cli_approval_denies_without_tty(fake_llm: FakeLLM, monkeypatch, capsys) -> None:
    from zkshizen.cli import agent_cmds
    monkeypatch.setattr(agent_cmds, "get_llm", lambda *a, **k: fake_llm.client())
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    fake_llm.script = [{"tool_calls": [("run_shell", {"command": "echo hi"})]}, {"text": "done"}]
    assert agent_cmds.cmd_ask(type("NS", (), {"prompt": ["x"], "resume": None, "profile": None, "model": None,
                                             "verbose": False, "yolo": False})()) == 0
    assert "DENIED" in fake_llm.requests[1]["messages"][-1]["content"]
    fake_llm.script = [{"tool_calls": [("run_shell", {"command": "echo zks-yolo"})]}, {"text": "done"}]
    agent_cmds.cmd_ask(type("NS", (), {"prompt": ["x"], "resume": None, "profile": None, "model": None,
                                      "verbose": False, "yolo": True})())
    assert "zks-yolo" in fake_llm.requests[-1]["messages"][-1]["content"]


def test_llm_not_configured_message() -> None:
    from zkshizen.llm.client import LLMError, client_for
    llm = client_for()
    with pytest.raises(LLMError, match="no LLM configured"):
        asyncio.run(llm.complete([{"role": "user", "content": "x"}]))
