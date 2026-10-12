"""Minimal example pack: one web tool, one prompt, one HTTP route.

Install with:  zip the hello-pack folder, then `zks pack install hello-pack.zip`.
The route is served at GET /api/packs/hello/hello.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from zkshizen.agent.registry import Tool


def _greet(args: dict[str, Any]) -> dict[str, str]:
    name = str(args.get("name") or "there").strip()[:80]
    return {"greeting": f"Hello, {name}!"}


def register(ctx) -> None:
    # Give every tool at least one required argument (see README, "Tool schemas").
    ctx.add_tools("web", [Tool(
        "hello_greet", "Greet a person by name.",
        {"type": "object",
         "properties": {"name": {"type": "string", "description": "Who to greet."}},
         "required": ["name"]},
        _greet,
    )])
    ctx.add_system_prompt("web", "When the user asks to be greeted, call hello_greet with their name.")

    router = APIRouter()

    @router.get("/hello")
    def hello() -> dict[str, str]:
        return {"message": "hello from hello-pack"}

    ctx.add_router(router)
