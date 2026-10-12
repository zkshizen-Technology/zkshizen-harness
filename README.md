# zkshizen harness

A self-hosted AI agent harness. One Python process (FastAPI) gives you:

- a web chat for end users,
- an admin chat for operators,
- a terminal agent (`zks chat` / `zks ask`) that can work on the host,

plus installable **packs** that add tools, prompts, HTTP routes, static web apps
and skills, optionally gated by an Ed25519-signed license. Works with Anthropic
or any OpenAI-compatible endpoint. Runs on Linux, macOS and Windows.

MIT licensed.

## Channels and the security boundary

| Channel | Entry point | Who | Tools |
|---|---|---|---|
| `web` | `POST /api/chat/web` (SSE) | any signed-in user | pack web tools and skills only |
| `admin` | `POST /api/chat/admin` (SSE) | users with role `admin` (2FA required) | pack admin tools and skills only |
| `cli` | `zks chat`, `zks ask` | whoever runs the command on the host | host tools: shell, read/write/edit files, list dirs, install skills |

The web and admin channels never get shell or file tools. Each channel builds
its own tool registry, so host tools are absent from web/admin requests rather
than forbidden by a prompt. The CLI channel asks `y/N` before every shell or
file-writing tool call; without a TTY the answer is "no". `--yolo` (or
`ZKS_YOLO=1`) skips the question, so only use it in a sandbox you are willing
to lose. Every tool call is written to the audit table.

Accounts use scrypt password hashes and SHA-256-hashed bearer tokens. Browser
sessions use a cookie plus a CSRF header for unsafe requests. Admin accounts
must enroll TOTP before they can sign in. Failed logins are throttled.

## Install

Python 3.11 or newer is required (3.12 recommended).

Installer scripts (they create a private venv, or use pipx if available):

```sh
# Linux / macOS, from a checkout
./install/install.sh
# or from a git URL
./install/install.sh https://example.com/you/zkshizen-harness.git
```

```powershell
# Windows
powershell -ExecutionPolicy Bypass -File install\install.ps1
```

Manual install with pipx:

```sh
pipx install .
```

Manual install with a venv:

```sh
python3 -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install .
```

For development: `pip install -e ".[test]"` and `python -m pytest tests -q`.

## Quickstart

```sh
zks init                                   # data dir, database, admin user, one-time admin API token
zks config set anthropic --provider anthropic --api-key - --default
# or an OpenAI-compatible endpoint:
zks config set openai --provider openai --model gpt-4o-mini --api-key - --default
zks config test                            # sends one short request
zks user passwd admin                      # set the admin password
zks user totp admin                        # enroll 2FA (scan the otpauth URI)
zks serve                                  # http://127.0.0.1:8787
zks service install                        # run at boot (systemd / launchd / Task Scheduler)
```

`--api-key -` prompts with hidden input so the key does not land in shell
history. Use `--api-key-env VAR` to read it from the environment instead, and
`--base-url` for a self-hosted or proxied endpoint. Some Anthropic-compatible
proxies expect `Authorization: Bearer`; add `--auth bearer` for those.

Other useful commands: `zks doctor`, `zks skills`, `zks user add NAME --role user`,
`zks user token NAME`, `zks pack list`, `zks sessions`, `zks ask "..." --resume ID`.

## Data directory

| OS | Default |
|---|---|
| Linux | `$XDG_DATA_HOME/zkshizen` or `~/.local/share/zkshizen` |
| macOS | `~/Library/Application Support/zkshizen` |
| Windows | `%LOCALAPPDATA%\zkshizen` |

Override with `ZKS_DATA_DIR`. It holds `zkshizen.db` (SQLite, WAL), `llm.json`
(LLM profiles, may contain API keys), `packs/`, `skills/`, `cli-sessions/` and
`machine_id`. Back it up and keep it private.

Other settings: `ZKS_HOST` (default `127.0.0.1`), `ZKS_PORT` (`8787`),
`ZKS_ADMIN_TOKEN`, `ZKS_CORS_ORIGINS` (comma-separated), `ZKS_DB_PATH`,
`ZKS_MAX_STEPS_WEB|ADMIN|CLI`, `ZKS_LICENSE_CHECK_S` (default 300, `0` disables),
`ZKS_TOKEN_TTL_HOURS`.

## Skills

A skill is a folder with a `SKILL.md`. Its frontmatter decides which channels
see it:

```markdown
---
name: hello-skill
description: Greets the user and explains what the assistant can do.
channels: [web, cli]
---
Instructions for the model...
```

Skills live in `<data_dir>/skills/` or inside a pack. See
`examples/skills/hello-skill/`.

## Packs

A pack is a zip with a `pack.toml` at its root (or in one top-level folder).
It is unpacked into `<data_dir>/packs/<id>/`.

```toml
id = "hello"                     # lowercase, digits, dashes
name = "Hello Pack"
version = "1.0.0"
min_core = "0.1.0"
entry = "hello_pack:register"    # module:function, imported from python_path
python_path = "python"           # relative dir added to sys.path
skills = "skills"                # relative dir of SKILL.md folders (optional)
web_dir = "web"                  # static files served at web_mount (optional)
web_mount = "/hello"             # must not be /api, /docs, /redoc, /openapi.json

[license]                        # optional; omit for a free pack
required = true
product = "hello"
server = "https://license.example.com"   # default activation server, overridable
public_keys = { k2026a = "<base64 Ed25519 public key>" }   # several = key rotation
```

The entry function receives a `PackContext`:

```python
def register(ctx):
    ctx.add_tools(channel, [Tool, ...])          # channel: "web" | "admin" | "cli"
    ctx.add_router(router, prefix=None)          # default /api/packs/<id>; or a custom /api/<name>
    ctx.add_system_prompt(channel, text)         # appended to the channel prompt
    ctx.add_output_filter(channel, fn)           # str -> str, applied to streamed replies
    ctx.add_context_provider(channel, fn)        # fn(request_context, user) -> {"system", "photos", "meta"} | None
    ctx.add_migration(migration_id, sql)         # run once on startup
    ctx.add_startup(fn)                          # called once the database is ready
    ctx.add_config_listener(fn)                  # fn(new_config) after a license sync changed it
    ctx.set_web_dir(path)                        # serve static files from another directory
    ctx.feature(name) -> bool                    # licensed feature flag
    ctx.config, ctx.license, ctx.pack_id, ctx.root
```

Notes for pack authors:

- `Tool(name, description, parameters, handler, timeout_s=120)` lives in
  `zkshizen.agent.registry`. Give every tool at least one required argument;
  tools without one get a required `reason` argument injected, because some
  Anthropic/OpenAI-compatible proxies return an empty body for a tool call
  with empty input.
- Custom router prefixes must start with `/api/` and must not be or sit under
  `/api/auth`, `/api/chat`, `/api/health`, `/api/me` or `/api/packs`. A
  violation raises `PackError` and the pack is not loaded.
- Context providers receive the request's free-form `context` object plus
  `session_id` and `last_user_text`. A returned `system` string is appended
  to the system message; `meta` is merged into the first SSE `meta` event.
- A pack whose entry function raises is listed as not loaded; the harness keeps
  running.

See `examples/packs/hello-pack/` for a working pack. Zip the folder and run
`zks pack install hello-pack.zip`.

### Licensed packs

```sh
zks pack install mypack.zip --license KEY --server https://license.example.com
zks pack activate mypack --key -        # activate later instead
zks pack sync                           # renew now and pull config changes
zks pack status mypack
zks pack install mypack-1.1.zip --upgrade   # keeps license.json and config.json
```

Activation sends the license key and a random per-installation machine id to
the license server, which returns a signed envelope plus the licensed config.
The payload binds product, machine, expiry, grace days, features and the
SHA-256 of the config, so the config cannot be edited locally. A running
server re-checks every `ZKS_LICENSE_CHECK_S` seconds; a revoked license
unloads the pack, an expired one keeps working through its grace period.
License servers must use HTTPS (plain HTTP is accepted only for localhost).

## Reverse proxy

`zks serve` binds to `127.0.0.1` by default. Put it behind a reverse proxy that
terminates HTTPS, and turn response buffering off so chat tokens stream.

Caddy:

```caddy
chat.example.com {
    reverse_proxy 127.0.0.1:8787 {
        flush_interval -1
    }
}
```

Nginx:

```nginx
server {
    listen 443 ssl http2;
    server_name chat.example.com;
    # ssl_certificate / ssl_certificate_key ...
    client_max_body_size 25m;

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location /api/chat/ {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
    }
}
```

If web apps on another origin call the API, list them in `ZKS_CORS_ORIGINS`.

## License

MIT. See `LICENSE`.
