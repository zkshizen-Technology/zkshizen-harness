"""HTTP server: auth, channel chat, pack routers and pack web apps.

Routes
  GET  /api/health
  POST /api/auth/login        {username, password, totp?} -> session cookie + bearer token
  POST /api/auth/logout
  GET  /api/me
  GET  /api/packs             installed packs and license status (admin)
  POST /api/chat/{web|admin}  SSE agent chat
  /api/packs/<id>/...         routers registered by packs
  <web_mount>/...             static web apps shipped by packs

Auth: `Authorization: Bearer <token>` or the HttpOnly `zks_session` cookie.
Cookie-authenticated unsafe requests must send `X-ZKS-CSRF: 1` (cross-site
pages cannot set custom headers without CORS approval).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from zkshizen import __version__, auth, db
from zkshizen.chat import stream_chat
from zkshizen.config import get_settings
from zkshizen.documents import validate as validate_documents
from zkshizen.llm.client import LLMClient, get_llm
from zkshizen.packs import license as lic
from zkshizen.packs.loader import PackContext, load_all, registry

log = logging.getLogger("zkshizen.server")
COOKIE = "zks_session"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


# ─── Dependencies ─────────────────────────────────────────────────────

def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def current_user(request: Request, conn: Conn) -> dict[str, Any]:
    header = request.headers.get("authorization", "")
    token, via_cookie = None, False
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    elif request.cookies.get(COOKIE):
        token, via_cookie = request.cookies[COOKIE], True
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in", headers={"WWW-Authenticate": "Bearer"})
    if via_cookie and request.method not in _SAFE_METHODS and request.headers.get("x-zks-csrf") != "1":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing X-ZKS-CSRF header")
    user = auth.user_for_token(conn, token)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session",
                            headers={"WWW-Authenticate": "Bearer"})
    return user.as_dict()


User = Annotated[dict[str, Any], Depends(current_user)]


def require_writer(user: User) -> dict[str, Any]:
    if user["role"] == "viewer":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Viewer role is read-only")
    return user


def require_admin(user: User) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


Writer = Annotated[dict[str, Any], Depends(require_writer)]
Admin = Annotated[dict[str, Any], Depends(require_admin)]
LLM = Annotated[LLMClient, Depends(get_llm)]


# ─── Login throttling (per client IP, in memory) ──────────────────────

_FAILS: dict[str, list[float]] = {}
MAX_FAILS, FAIL_WINDOW_S = 5, 300


def _throttle(ip: str) -> None:
    now = time.time()
    recent = [t for t in _FAILS.get(ip, []) if now - t < FAIL_WINDOW_S]
    _FAILS[ip] = recent
    if len(recent) >= MAX_FAILS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed logins; wait a few minutes")


# ─── Schemas ──────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    totp: str | None = Field(default=None, max_length=10)


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    session_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_.:-]{1,80}$")
    messages: list[ChatMessage] = Field(min_length=1, max_length=60)
    photos: list[str] = Field(default_factory=list, max_length=3)
    documents: list[dict[str, Any]] = Field(default_factory=list)
    # Free-form per-request context for pack context providers (e.g. {"pinned_case_id": ...}).
    context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("photos")
    @classmethod
    def _photos(cls, v: list[str]) -> list[str]:
        for p in v:
            if not p.startswith("data:image/") or ";base64," not in p:
                raise ValueError("photos must be base64 image data URLs")
            if len(p) > 9_000_000:
                raise ValueError("photo too large (max ~6 MB)")
        return v

    @field_validator("documents")
    @classmethod
    def _docs(cls, v: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return validate_documents(v)


# ─── Core router ──────────────────────────────────────────────────────

core = APIRouter(prefix="/api")


@core.get("/health")
def health() -> dict[str, Any]:
    reg = registry()
    return {"status": "online", "service": "zkshizen-harness", "version": __version__,
            "packs": [{"id": s.id, "loaded": s.loaded} for s in reg.status]}


@core.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response, conn: Conn) -> dict[str, Any]:
    ip = request.client.host if request.client else "unknown"
    _throttle(ip)
    try:
        user = auth.login(conn, body.username, body.password, body.totp)
    except auth.AuthError as exc:
        _FAILS.setdefault(ip, []).append(time.time())
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    if user.role == "admin" and not user.has_totp:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"Admins must enroll 2FA first. On the server run: zks user totp {user.username}")
    settings = get_settings()
    token = auth.issue_token(conn, user.id, f"web-login {ip}", ttl_hours=settings.token_ttl_hours)
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                        max_age=settings.token_ttl_hours * 3600, path="/")
    return {"user": user.as_dict(), "token": token}


@core.post("/auth/logout")
def logout(request: Request, response: Response, conn: Conn) -> dict[str, bool]:
    token = request.cookies.get(COOKIE) or request.headers.get("authorization", "")[7:].strip()
    if token:
        auth.revoke_token(conn, token)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@core.get("/me")
def me(user: User) -> dict[str, Any]:
    return user


@core.get("/packs")
def packs(user: Admin) -> dict[str, Any]:
    return {"packs": [s.as_dict() for s in registry().status]}


@core.post("/chat/{channel}")
async def chat(channel: Literal["web", "admin"], body: ChatRequest, user: Writer, llm: LLM) -> StreamingResponse:
    if channel == "admin" and user["role"] != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin channel requires an admin account")
    stream = stream_chat(llm, channel, session_id=body.session_id, user=user,
                         history=[m.model_dump() for m in body.messages], photos=body.photos,
                         documents=body.documents, request_context=body.context)
    return StreamingResponse(stream, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── Startup, packs and background license sync ───────────────────────

def init_database() -> str | None:
    """Core schema + every loaded pack's migrations. Returns a generated admin token (first run only)."""
    conn = db.connect()
    try:
        db.init_core(conn)
        token = auth.ensure_admin(conn, get_settings().admin_token)
        for ctx in registry().contexts:
            for migration_id, sql in ctx.migrations:
                db.apply_migration(conn, migration_id, sql)
        return token
    finally:
        conn.close()


def run_pack_startup() -> None:
    for ctx in registry().contexts:
        for fn in ctx.startup:
            try:
                fn()
            except Exception as exc:
                log.error("pack %s startup failed: %s", ctx.pack_id, exc)


def sync_licensed_packs() -> list[dict[str, Any]]:
    """One license check per licensed pack. Notifies config listeners when the config changed."""
    results = []
    for ctx in list(registry().contexts):
        if not ctx.manifest.license.required:
            continue
        try:
            state, changed = lic.check(ctx.manifest)
        except lic.LicenseError as exc:
            results.append({"pack": ctx.pack_id, "ok": False, "message": str(exc)})
            continue
        ctx.license, ctx.config = state, state.config
        if changed:
            for fn in ctx.config_listeners:
                try:
                    fn(state.config)
                except Exception as exc:
                    log.error("pack %s config listener failed: %s", ctx.pack_id, exc)
        results.append({"pack": ctx.pack_id, "ok": state.valid, "status": state.status, "config_changed": changed})
    return results


async def _license_loop(interval_s: int) -> None:
    while True:
        await asyncio.sleep(interval_s)
        try:
            for r in await asyncio.to_thread(sync_licensed_packs):
                log.info("license check: %s", r)
        except Exception as exc:
            log.warning("license check failed: %s", exc)


def _mount_pack(app: FastAPI, ctx: PackContext) -> None:
    for router, prefix in ctx.routers:
        app.include_router(router, prefix=prefix)
    web_dir, mount = ctx.web_dir, ctx.manifest.web_mount
    if web_dir and mount and web_dir.is_dir():
        if mount == "/":
            app.mount("/", StaticFiles(directory=str(web_dir), html=True), name=f"pack-{ctx.pack_id}-web")
        else:
            app.mount(mount, StaticFiles(directory=str(web_dir), html=True), name=f"pack-{ctx.pack_id}-web")


_LANDING = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>zkshizen harness</title>
<style>body{font-family:system-ui,sans-serif;max-width:640px;margin:48px auto;padding:0 16px;line-height:1.5;
color:#1f2937;background:#f9fafb}code{background:#e5e7eb;padding:2px 5px;border-radius:4px}
@media (prefers-color-scheme:dark){body{color:#e5e7eb;background:#111827}code{background:#374151}}</style>
</head><body><h1>zkshizen harness is running</h1>
<p>No pack provides a web app at this address yet.</p>
<ul><li>API health: <code>/api/health</code></li><li>Install a pack: <code>zks pack install &lt;file.zip&gt;</code></li>
<li>Talk to the agent in a terminal: <code>zks chat</code></li></ul>
<p>Pack web apps:</p><ul>%LINKS%</ul></body></html>"""


def create_app(llm_override: Any = None) -> FastAPI:
    """Build the app. Packs are (re)loaded here so routers and static mounts exist before serving."""
    load_all()
    generated = init_database()
    if generated:
        print(f"[zkshizen] bootstrap admin API token (shown once): {generated}", flush=True)
    run_pack_startup()
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = None
        if any(c.manifest.license.required for c in registry().contexts) and settings.license_check_interval_s > 0:
            task = asyncio.create_task(_license_loop(settings.license_check_interval_s))
        yield
        if task:
            task.cancel()

    app = FastAPI(title="zkshizen harness", version=__version__, lifespan=lifespan)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_credentials=True,
                           allow_methods=["GET", "POST", "PATCH", "DELETE"],
                           allow_headers=["Authorization", "Content-Type", "Accept", "X-ZKS-CSRF"])

    @app.exception_handler(sqlite3.IntegrityError)
    async def _integrity(_req: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
        return JSONResponse({"detail": f"conflict: {exc}"}, status_code=409)

    app.include_router(core)
    root_taken = False
    for ctx in registry().contexts:
        _mount_pack(app, ctx)
        root_taken = root_taken or ctx.manifest.web_mount == "/"
    if not root_taken:
        links = "".join(f'<li><a href="{c.manifest.web_mount}/">{c.manifest.name}</a></li>'
                        for c in registry().contexts if c.manifest.web_mount) or "<li>none</li>"

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        def landing() -> str:
            return _LANDING.replace("%LINKS%", links)
    if llm_override is not None:
        app.dependency_overrides[get_llm] = llm_override
    return app


def serve(host: str | None = None, port: int | None = None) -> None:
    import uvicorn

    s = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host=host or s.host, port=port or s.port, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1")
