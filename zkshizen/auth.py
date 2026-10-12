"""Users, passwords, bearer tokens and TOTP two-factor auth.

- Passwords: scrypt (stdlib hashlib), per-user random salt.
- Tokens: random 32-byte urlsafe strings; only SHA-256 hashes are stored.
- TOTP: RFC 6238 (SHA-1, 30 s, 6 digits), compatible with Google
  Authenticator / Authy / 1Password.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import secrets
import sqlite3
import struct
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from zkshizen.db import transaction

ROLES = ("admin", "user", "viewer")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


class AuthError(ValueError):
    pass


# ─── Passwords ───────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    if len(password) < 10:
        raise AuthError("password must be at least 10 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, salt_b64, digest_b64 = stored.split("$")
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
    except ValueError:
        return False
    actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return hmac.compare_digest(actual, expected)


# ─── TOTP ────────────────────────────────────────────────────────────

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret_b32: str, counter: int) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_now(secret_b32: str, at: float | None = None) -> str:
    return _hotp(secret_b32, int((at if at is not None else time.time()) // 30))


def verify_totp(secret_b32: str, code: str, at: float | None = None, window: int = 1) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not (code.isdigit() and len(code) == 6):
        return False
    step = int((at if at is not None else time.time()) // 30)
    return any(hmac.compare_digest(_hotp(secret_b32, step + d), code) for d in range(-window, window + 1))


def totp_uri(secret_b32: str, username: str, issuer: str = "zkshizen") -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret_b32}"
            f"&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30")


# ─── Users ───────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class User:
    id: str
    username: str
    display_name: str
    role: str
    has_totp: bool

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "username": self.username, "display_name": self.display_name,
                "role": self.role, "has_totp": self.has_totp}


def _user(row: sqlite3.Row) -> User:
    return User(row["id"], row["username"], row["display_name"], row["role"], bool(row["totp_secret"]))


def create_user(conn: sqlite3.Connection, username: str, password: str | None, role: str = "user",
                display_name: str = "") -> User:
    if role not in ROLES:
        raise AuthError(f"role must be one of {', '.join(ROLES)}")
    if not username or len(username) > 64 or not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise AuthError("username: letters, digits, . _ - (max 64)")
    if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        raise AuthError(f"user '{username}' already exists")
    uid = uuid.uuid4().hex[:16]
    with transaction(conn):
        conn.execute("INSERT INTO users (id, username, display_name, role, password_hash) VALUES (?, ?, ?, ?, ?)",
                     (uid, username, display_name or username, role, hash_password(password) if password else None))
    return get_user(conn, uid)  # type: ignore[return-value]


def get_user(conn: sqlite3.Connection, user_id: str) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE id = ? AND disabled = 0", (user_id,)).fetchone()
    return _user(row) if row else None


def find_user(conn: sqlite3.Connection, username: str) -> User | None:
    row = conn.execute("SELECT * FROM users WHERE username = ? AND disabled = 0", (username,)).fetchone()
    return _user(row) if row else None


def list_users(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [_user(r).as_dict() | {"disabled": bool(r["disabled"])}
            for r in conn.execute("SELECT * FROM users ORDER BY created_at")]


def set_password(conn: sqlite3.Connection, user_id: str, password: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))


def set_totp_secret(conn: sqlite3.Connection, user_id: str, secret: str | None) -> None:
    with transaction(conn):
        conn.execute("UPDATE users SET totp_secret = ? WHERE id = ?", (secret, user_id))


def disable_user(conn: sqlite3.Connection, user_id: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE users SET disabled = 1 WHERE id = ?", (user_id,))
        conn.execute("UPDATE tokens SET revoked = 1 WHERE user_id = ?", (user_id,))


def login(conn: sqlite3.Connection, username: str, password: str, totp_code: str | None) -> User:
    """Password check, then TOTP if the user enrolled it. Admins must use TOTP once enrolled."""
    row = conn.execute("SELECT * FROM users WHERE username = ? AND disabled = 0", (username,)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        raise AuthError("invalid username or password")
    if row["totp_secret"] and not verify_totp(row["totp_secret"], totp_code or ""):
        raise AuthError("invalid or missing 2FA code")
    return _user(row)


# ─── Tokens ──────────────────────────────────────────────────────────

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(conn: sqlite3.Connection, user_id: str, label: str, ttl_hours: int | None = None) -> str:
    token = "zks_" + secrets.token_urlsafe(32)
    expires = None
    if ttl_hours:
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")
    with transaction(conn):
        conn.execute("INSERT INTO tokens (token_hash, user_id, label, expires_at) VALUES (?, ?, ?, ?)",
                     (hash_token(token), user_id, label[:80], expires))
    return token


def ensure_token(conn: sqlite3.Connection, user_id: str, token: str, label: str) -> None:
    """Register a known plaintext token (e.g. ZKS_ADMIN_TOKEN from the environment)."""
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO tokens (token_hash, user_id, label) VALUES (?, ?, ?)",
                     (hash_token(token), user_id, label))


def user_for_token(conn: sqlite3.Connection, token: str) -> User | None:
    row = conn.execute(
        "SELECT u.* FROM tokens t JOIN users u ON u.id = t.user_id "
        "WHERE t.token_hash = ? AND t.revoked = 0 AND u.disabled = 0 "
        "AND (t.expires_at IS NULL OR t.expires_at > CURRENT_TIMESTAMP)",
        (hash_token(token),),
    ).fetchone()
    return _user(row) if row else None


def revoke_token(conn: sqlite3.Connection, token: str) -> None:
    with transaction(conn):
        conn.execute("UPDATE tokens SET revoked = 1 WHERE token_hash = ?", (hash_token(token),))


def ensure_admin(conn: sqlite3.Connection, env_token: str | None) -> str | None:
    """Create user 'admin' on first run. Returns a generated plaintext token, shown once."""
    admin = find_user(conn, "admin")
    if admin is None:
        admin = create_user(conn, "admin", None, role="admin", display_name="Administrator")
    if env_token:
        ensure_token(conn, admin.id, env_token, "bootstrap-env")
        return None
    if conn.execute("SELECT 1 FROM tokens WHERE user_id = ? AND revoked = 0", (admin.id,)).fetchone():
        return None
    return issue_token(conn, admin.id, "bootstrap-generated")
