"""User CRUD operations."""

import fnmatch
import secrets

from app.config import settings
from app.db import get_conn


def _gen_access_token() -> str:
    """Generate a long, URL-safe random token for passwordless access."""
    return secrets.token_urlsafe(24)


def get_or_create_user(email: str) -> dict:
    """Get existing user or create a new one. Returns user dict."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        return dict(row)

    is_admin = _matches_admin_pattern(email)
    conn.execute(
        "INSERT INTO users (email, is_admin, access_token) VALUES (?, ?, ?)",
        (email, int(is_admin), _gen_access_token()),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row)


def ensure_access_token(email: str) -> str | None:
    """Return the user's personal access token, generating one if missing."""
    conn = get_conn()
    row = conn.execute(
        "SELECT access_token FROM users WHERE email = ?", (email,)
    ).fetchone()
    if not row:
        return None
    token = row["access_token"]
    if token:
        return token
    token = _gen_access_token()
    conn.execute(
        "UPDATE users SET access_token = ? WHERE email = ?", (token, email)
    )
    conn.commit()
    return token


def regenerate_access_token(email: str) -> str | None:
    """Rotate the user's access token, invalidating the previous one."""
    conn = get_conn()
    if not conn.execute(
        "SELECT 1 FROM users WHERE email = ?", (email,)
    ).fetchone():
        return None
    token = _gen_access_token()
    conn.execute(
        "UPDATE users SET access_token = ? WHERE email = ?", (token, email)
    )
    conn.commit()
    return token


def get_user_by_access_token(token: str) -> dict | None:
    """Resolve a user from a personal access token (constant-effort lookup)."""
    if not token or not token.strip():
        return None
    row = get_conn().execute(
        "SELECT * FROM users WHERE access_token = ?", (token.strip(),)
    ).fetchone()
    return dict(row) if row else None


def get_user_by_email(email: str) -> dict | None:
    row = get_conn().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def is_admin(email: str) -> bool:
    user = get_user_by_email(email)
    return bool(user and user["is_admin"])


def update_last_login(email: str):
    get_conn().execute(
        "UPDATE users SET last_login = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE email = ?",
        (email,),
    )
    get_conn().commit()


def list_users() -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM users ORDER BY last_login DESC NULLS LAST"
    ).fetchall()
    return [dict(r) for r in rows]


def set_user_limit(email: str, limit: int | None):
    get_conn().execute(
        "UPDATE users SET monthly_token_limit = ? WHERE email = ?",
        (limit, email),
    )
    get_conn().commit()


def mark_onboarding_completed(user_id: int):
    conn = get_conn()
    conn.execute("UPDATE users SET onboarding_completed = 1 WHERE id = ?", (user_id,))
    conn.commit()


def _matches_admin_pattern(email: str) -> bool:
    patterns = [p.strip().lower() for p in settings.admin_emails.split(",") if p.strip()]
    email_lower = email.lower()
    for pattern in patterns:
        if fnmatch.fnmatch(email_lower, pattern):
            return True
    return False
