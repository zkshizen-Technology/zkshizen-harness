"""`zks config|pack|user|skills|init|serve` management commands."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path

from zkshizen import auth, db
from zkshizen.agent import skills as skill_store
from zkshizen.config import get_settings, machine_id
from zkshizen.llm import config as llm_config
from zkshizen.llm.client import LLMError, client_for
from zkshizen.packs import license as lic
from zkshizen.packs import loader


def _conn():
    conn = db.connect()
    db.init_core(conn)
    return conn


# ─── init / serve ─────────────────────────────────────────────────────

def cmd_init(_: argparse.Namespace) -> int:
    from zkshizen.server import init_database

    s = get_settings()
    loader.load_all()
    token = init_database()
    print(f"data dir : {s.data_dir}")
    print(f"database : {s.db_path}")
    print(f"machine  : {machine_id()}")
    if token:
        print(f"admin API token (shown once, store it now): {token}")
    print("next: zks config set <profile> ...  |  zks user passwd admin  |  zks pack install <zip>  |  zks serve")
    return 0


def cmd_serve(ns: argparse.Namespace) -> int:
    from zkshizen.server import serve

    serve(ns.host, ns.port)
    return 0


# ─── LLM config ───────────────────────────────────────────────────────

def _read_key(value: str | None) -> str | None:
    if value is None:
        return None
    if value == "-":
        key = getpass.getpass("API key (input hidden): ").strip()
        if not key:
            raise SystemExit("empty API key")
        return key
    return value


def cmd_config_show(_: argparse.Namespace) -> int:
    data = llm_config.load_file()
    print(f"config file: {llm_config.config_path()}")
    if not data["profiles"]:
        print("no profiles yet. Examples:\n"
              "  zks config set anthropic --provider anthropic --api-key - --default\n"
              "  zks config set openai --provider openai --model <model> --api-key -")
    for name, raw in data["profiles"].items():
        mark = "*" if name == data.get("default") else " "
        key = raw.get("api_key")
        src = llm_config.mask(key) if key else (f"env {raw['api_key_env']}" if raw.get("api_key_env") else
                                               "env " + llm_config.PROVIDER_DEFAULTS[raw["provider"]]["key_env"])
        print(f" {mark} {name:14} {raw['provider']:9} model={raw.get('model') or '(provider default)'}  "
              f"base_url={raw.get('base_url') or '(provider default)'}  auth={raw.get('auth', 'default')}  key={src}")
    try:
        p = llm_config.resolve()
        print(f"\neffective: profile={p.name} provider={p.provider} model={p.model} base_url={p.base_url} "
              f"key={llm_config.mask(p.api_key)}")
    except llm_config.LLMConfigError as exc:
        print(f"\neffective: not usable: {exc}")
    return 0


def cmd_config_set(ns: argparse.Namespace) -> int:
    fields = {"provider": ns.provider, "base_url": ns.base_url, "model": ns.model, "api_key": _read_key(ns.api_key),
              "api_key_env": ns.api_key_env, "timeout_s": ns.timeout, "token_param": ns.token_param, "auth": ns.auth}
    if llm_config.load_file()["profiles"].get(ns.name) is None and not ns.provider:
        if ns.name not in llm_config.PROVIDERS:
            raise SystemExit("new profile needs --provider anthropic|openai")
        fields["provider"] = ns.name
    try:
        saved = llm_config.upsert_profile(ns.name, fields, make_default=ns.default)
    except llm_config.LLMConfigError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"saved profile '{ns.name}' ({saved['provider']}) to {llm_config.config_path()}")
    return 0


def cmd_config_use(ns: argparse.Namespace) -> int:
    try:
        llm_config.set_default(ns.name)
    except llm_config.LLMConfigError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"default profile: {ns.name}")
    return 0


def cmd_config_remove(ns: argparse.Namespace) -> int:
    print("removed" if llm_config.remove_profile(ns.name) else "no such profile")
    return 0


def cmd_config_test(ns: argparse.Namespace) -> int:
    llm = client_for(ns.name, ns.model)
    print(f"testing {llm.profile_name} -> {llm.provider} {llm.base_url} model={llm.model}")
    try:
        reply = asyncio.run(llm.complete([{"role": "user", "content": "Reply with exactly: ZKS-OK"}],
                                         max_tokens=20, temperature=0))
    except LLMError as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"OK: {reply.strip()[:80]}")
    return 0


# ─── Packs ────────────────────────────────────────────────────────────

def cmd_pack_list(_: argparse.Namespace) -> int:
    reg = loader.load_all()
    if not reg.status:
        print("no packs installed")
    for s in reg.status:
        lic_txt = ""
        if s.license:
            lic_txt = f"  license={s.license['status']} customer={s.license.get('customer') or '-'} " \
                      f"expires={s.license.get('expires_at') or '-'}"
        print(f"{s.id:20} {s.version:10} {'LOADED' if s.loaded else 'OFF':7} web={s.web_mount or '-'}{lic_txt}")
        if s.reason != "loaded":
            print(f"    {s.reason}")
    return 0


def cmd_pack_install(ns: argparse.Namespace) -> int:
    try:
        m = loader.install_zip(Path(ns.zip), replace=ns.upgrade)
    except (loader.PackError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"installed pack '{m.id}' {m.version} -> {m.root}")
    if m.license.required:
        if ns.license:
            return _activate(m, ns.license, ns.server)
        print(f"this pack needs a license: zks pack activate {m.id} --key <LICENSE-KEY>")
    print("restart the server (or the service) to load it")
    return 0


def _activate(m, key: str, server: str | None) -> int:
    try:
        state = lic.activate(m, key, server)
    except lic.LicenseError as exc:
        print(f"activation FAILED: {exc}")
        return 1
    p = state.payload
    print(f"activated: {p.get('customer')} • product {p.get('product')} • expires {p.get('expires_at')} "
          f"• config v{p.get('config_version')}")
    print("restart the server (or the service) to load the licensed config")
    return 0


def cmd_pack_activate(ns: argparse.Namespace) -> int:
    try:
        m = loader.find(ns.id)
    except loader.PackError as exc:
        raise SystemExit(str(exc)) from exc
    key = ns.key if ns.key and ns.key != "-" else getpass.getpass("License key: ").strip()
    return _activate(m, key, ns.server)


def cmd_pack_sync(ns: argparse.Namespace) -> int:
    loader.load_all()
    from zkshizen.server import sync_licensed_packs

    results = sync_licensed_packs()
    if not results:
        print("no licensed packs loaded")
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    print("a running server picks up config changes within its check interval; restart it to apply now")
    return 0 if all(r.get("ok") for r in results) else 1


def cmd_pack_status(ns: argparse.Namespace) -> int:
    try:
        m = loader.find(ns.id)
    except loader.PackError as exc:
        raise SystemExit(str(exc)) from exc
    if not m.license.required:
        print(f"{m.id}: no license required")
        return 0
    print(json.dumps(lic.local_state(m).as_dict() | {"machine_id": machine_id()}, ensure_ascii=False, indent=1))
    return 0


def cmd_pack_remove(ns: argparse.Namespace) -> int:
    print("removed" if loader.uninstall(ns.id) else "no such pack")
    return 0


# ─── Users ────────────────────────────────────────────────────────────

def _password(value: str | None) -> str:
    if value and value != "-":
        return value
    p1 = getpass.getpass("New password (min 10 chars): ")
    if getpass.getpass("Repeat password: ") != p1:
        raise SystemExit("passwords do not match")
    return p1


def cmd_user_add(ns: argparse.Namespace) -> int:
    conn = _conn()
    try:
        u = auth.create_user(conn, ns.username, _password(ns.password), role=ns.role, display_name=ns.name or "")
    except auth.AuthError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"created {u.role} '{u.username}'")
    if u.role == "admin":
        print(f"admins need 2FA before web login: zks user totp {u.username}")
    return 0


def cmd_user_list(_: argparse.Namespace) -> int:
    for u in auth.list_users(_conn()):
        print(f"{u['username']:20} {u['role']:7} 2fa={'yes' if u['has_totp'] else 'no '} "
              f"{'DISABLED' if u['disabled'] else ''}")
    return 0


def _get(conn, username: str):
    u = auth.find_user(conn, username)
    if u is None:
        raise SystemExit(f"no such user '{username}'")
    return u


def cmd_user_passwd(ns: argparse.Namespace) -> int:
    conn = _conn()
    u = _get(conn, ns.username)
    try:
        auth.set_password(conn, u.id, _password(ns.password))
    except auth.AuthError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"password updated for '{u.username}'")
    return 0


def cmd_user_totp(ns: argparse.Namespace) -> int:
    conn = _conn()
    u = _get(conn, ns.username)
    if ns.disable:
        auth.set_totp_secret(conn, u.id, None)
        print(f"2FA disabled for '{u.username}'")
        return 0
    secret = auth.new_totp_secret()
    print("Add this account to your authenticator app (Google Authenticator, Authy, 1Password):")
    print(f"  secret : {secret}")
    print(f"  uri    : {auth.totp_uri(secret, u.username)}")
    code = input("Enter the 6-digit code from the app to confirm: ").strip()
    if not auth.verify_totp(secret, code):
        raise SystemExit("code did not match; 2FA not enabled")
    auth.set_totp_secret(conn, u.id, secret)
    print(f"2FA enabled for '{u.username}'")
    return 0


def cmd_user_token(ns: argparse.Namespace) -> int:
    conn = _conn()
    u = _get(conn, ns.username)
    print(auth.issue_token(conn, u.id, ns.label or "cli-issued", ttl_hours=ns.ttl_hours))
    return 0


def cmd_user_disable(ns: argparse.Namespace) -> int:
    conn = _conn()
    auth.disable_user(conn, _get(conn, ns.username).id)
    print(f"disabled '{ns.username}' and revoked its tokens")
    return 0


# ─── Skills ───────────────────────────────────────────────────────────

def cmd_skills(_: argparse.Namespace) -> int:
    loader.load_all()
    found = skill_store.discover()
    if not found:
        print("no skills found")
    for sk in sorted(found.values(), key=lambda s: s.name):
        print(f"{sk.name:36} [{','.join(sk.channels)}] {sk.source}{' +tools' if sk.has_tools else ''}\n"
              f"    {sk.description[:110]}")
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    s = get_settings()
    print(f"zkshizen data dir : {s.data_dir} ({'exists' if s.data_dir.exists() else 'missing, run zks init'})")
    print(f"python            : {os.sys.version.split()[0]}")
    try:
        p = llm_config.resolve()
        print(f"LLM               : {p.provider} {p.model} @ {p.base_url} key={llm_config.mask(p.api_key)}")
    except llm_config.LLMConfigError as exc:
        print(f"LLM               : not configured ({exc})")
    cmd_pack_list(argparse.Namespace())
    return 0
