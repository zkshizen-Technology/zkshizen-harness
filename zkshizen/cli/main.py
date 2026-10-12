"""`zks` command line entry point."""

from __future__ import annotations

import argparse
import sys

from zkshizen import __version__
from zkshizen.cli import agent_cmds as a
from zkshizen.cli import manage_cmds as m
from zkshizen.cli import service as svc
from zkshizen.llm import config as llm_config


def _llm_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--profile", help="LLM profile (default: the default profile)")
    p.add_argument("--model", help="override the profile's model for this run")
    p.add_argument("--yolo", action="store_true", help="run shell/file tools without asking")
    p.add_argument("-v", "--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zks", description="zkshizen harness: self-hosted agent with packs")
    parser.add_argument("--version", action="version", version=f"zkshizen {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create data dir, database and admin user").set_defaults(func=m.cmd_init)
    p = sub.add_parser("serve", help="run the HTTP server")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.set_defaults(func=m.cmd_serve)
    sub.add_parser("doctor", help="show configuration and pack status").set_defaults(func=m.cmd_doctor)

    p = sub.add_parser("ask", help="one-shot agent task on this host")
    p.add_argument("prompt", nargs="*")
    p.add_argument("--resume", metavar="SESSION")
    _llm_flags(p)
    p.set_defaults(func=a.cmd_ask)
    p = sub.add_parser("chat", help="interactive agent session on this host")
    p.add_argument("--resume", metavar="SESSION")
    _llm_flags(p)
    p.set_defaults(func=a.cmd_chat)
    sub.add_parser("sessions", help="list saved CLI sessions").set_defaults(func=a.cmd_sessions)
    sub.add_parser("skills", help="list skills per channel").set_defaults(func=m.cmd_skills)

    cfg = sub.add_parser("config", help="LLM providers, API keys, endpoints, models").add_subparsers(
        dest="sub", required=True)
    cfg.add_parser("show").set_defaults(func=m.cmd_config_show)
    p = cfg.add_parser("set", help="create or update a profile")
    p.add_argument("name")
    p.add_argument("--provider", choices=llm_config.PROVIDERS)
    p.add_argument("--base-url", dest="base_url")
    p.add_argument("--model")
    p.add_argument("--api-key", dest="api_key", metavar="KEY|-", help="'-' prompts with hidden input")
    p.add_argument("--api-key-env", dest="api_key_env", metavar="VAR")
    p.add_argument("--auth", choices=llm_config.AUTH_MODES, help="anthropic: bearer for Authorization-header proxies")
    p.add_argument("--timeout", type=float)
    p.add_argument("--token-param", dest="token_param", choices=llm_config.TOKEN_PARAMS)
    p.add_argument("--default", action="store_true")
    p.set_defaults(func=m.cmd_config_set)
    p = cfg.add_parser("use")
    p.add_argument("name")
    p.set_defaults(func=m.cmd_config_use)
    p = cfg.add_parser("remove")
    p.add_argument("name")
    p.set_defaults(func=m.cmd_config_remove)
    p = cfg.add_parser("test")
    p.add_argument("name", nargs="?")
    p.add_argument("--model")
    p.set_defaults(func=m.cmd_config_test)

    pk = sub.add_parser("pack", help="install, activate and sync packs").add_subparsers(dest="sub", required=True)
    pk.add_parser("list").set_defaults(func=m.cmd_pack_list)
    p = pk.add_parser("install", help="install a pack zip")
    p.add_argument("zip")
    p.add_argument("--license", metavar="KEY", help="activate right after installing")
    p.add_argument("--server", help="license server URL (default: from the pack)")
    p.add_argument("--upgrade", action="store_true", help="replace an installed pack, keeping its license")
    p.set_defaults(func=m.cmd_pack_install)
    p = pk.add_parser("activate", help="activate a licensed pack")
    p.add_argument("id")
    p.add_argument("--key", help="license key ('-' or omitted prompts)")
    p.add_argument("--server")
    p.set_defaults(func=m.cmd_pack_activate)
    pk.add_parser("sync", help="renew licenses and pull config changes now").set_defaults(func=m.cmd_pack_sync)
    p = pk.add_parser("status")
    p.add_argument("id")
    p.set_defaults(func=m.cmd_pack_status)
    p = pk.add_parser("remove")
    p.add_argument("id")
    p.set_defaults(func=m.cmd_pack_remove)

    us = sub.add_parser("user", help="web accounts and 2FA").add_subparsers(dest="sub", required=True)
    p = us.add_parser("add")
    p.add_argument("username")
    p.add_argument("--role", choices=("admin", "user", "viewer"), default="user")
    p.add_argument("--name")
    p.add_argument("--password", help="omit to be prompted")
    p.set_defaults(func=m.cmd_user_add)
    us.add_parser("list").set_defaults(func=m.cmd_user_list)
    p = us.add_parser("passwd")
    p.add_argument("username")
    p.add_argument("--password")
    p.set_defaults(func=m.cmd_user_passwd)
    p = us.add_parser("totp", help="enroll (or --disable) 2FA")
    p.add_argument("username")
    p.add_argument("--disable", action="store_true")
    p.set_defaults(func=m.cmd_user_totp)
    p = us.add_parser("token", help="issue an API token")
    p.add_argument("username")
    p.add_argument("--label")
    p.add_argument("--ttl-hours", type=int)
    p.set_defaults(func=m.cmd_user_token)
    p = us.add_parser("disable")
    p.add_argument("username")
    p.set_defaults(func=m.cmd_user_disable)

    sv = sub.add_parser("service", help="run the server at boot").add_subparsers(dest="sub", required=True)
    p = sv.add_parser("install")
    p.add_argument("--user", help="Linux: account the service runs as")
    p.add_argument("--print", action="store_true", help="only print what would be installed")
    p.set_defaults(func=svc.cmd_install)
    sv.add_parser("uninstall").set_defaults(func=svc.cmd_uninstall)
    sv.add_parser("status").set_defaults(func=svc.cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    return int(ns.func(ns) or 0)


if __name__ == "__main__":
    sys.exit(main())
