"""User CRUD operations."""

import fnmatch

from app.config import settings
from app.db import get_conn


def get_or_create_user(email: str) -> dict:
    """Get existing user or create a new one. Returns user dict."""
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        return dict(row)

    is_admin = _matches_admin_pattern(email)
    conn.execute(
        "INSERT INTO users (email, is_admin) VALUES (?, ?)",
        (email, int(is_admin)),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row)


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


def _matches_admin_pattern(email: str) -> bool:
    patterns = [p.strip().lower() for p in settings.admin_emails.split(",") if p.strip()]
    email_lower = email.lower()
    for pattern in patterns:
        if fnmatch.fnmatch(email_lower, pattern):
            return True
    return False
