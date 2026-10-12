"""SQLite (WAL) storage for the core: users, tokens, audit, chat history, migrations.

Packs add their own tables through `PackContext.add_migration`; every
migration id is applied once and recorded in `schema_migrations`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from zkshizen.config import get_settings

CORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('admin', 'user', 'viewer')),
    password_hash TEXT,
    totp_secret TEXT,
    disabled INTEGER NOT NULL DEFAULT 0 CHECK (disabled IN (0, 1)),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,            -- SHA-256 of the bearer token; plaintext is never stored
    user_id TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,                   -- NULL = no expiry (API tokens)
    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);

CREATE TABLE IF NOT EXISTS agent_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL CHECK (channel IN ('web', 'admin', 'cli')),
    session_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL,
    ok INTEGER NOT NULL CHECK (ok IN (0, 1)),
    result_preview TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_audit_session ON agent_audit(session_id, id);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    user_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id, id);

CREATE TABLE IF NOT EXISTS schema_migrations (
    id TEXT PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path or get_settings().db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolled back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """Short-lived connection for tools and background work."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_core(conn: sqlite3.Connection) -> None:
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    if str(mode).lower() != "wal":
        raise RuntimeError(f"SQLite refused WAL mode (got {mode!r})")
    conn.executescript(CORE_SCHEMA)


def apply_migration(conn: sqlite3.Connection, migration_id: str, sql: str) -> bool:
    """Run `sql` once. Returns True when it was applied now."""
    if conn.execute("SELECT 1 FROM schema_migrations WHERE id = ?", (migration_id,)).fetchone():
        return False
    conn.executescript(sql)
    with transaction(conn):
        conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (migration_id,))
    return True


def kv_get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps(value, ensure_ascii=False)))


def add_audit(conn: sqlite3.Connection, channel: str, session_id: str, actor: str, tool: str,
              args: dict[str, Any], ok: bool, preview: str) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO agent_audit (channel, session_id, actor, tool, args_json, ok, result_preview) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (channel, session_id, actor, tool, json.dumps(args, ensure_ascii=False, default=str)[:8000],
             1 if ok else 0, preview[:2000]),
        )


def add_chat_message(conn: sqlite3.Connection, session_id: str, channel: str, user_id: str | None,
                     role: str, content: str) -> None:
    with transaction(conn):
        conn.execute("INSERT INTO chat_messages (session_id, channel, user_id, role, content) VALUES (?, ?, ?, ?, ?)",
                     (session_id, channel, user_id, role, content))
