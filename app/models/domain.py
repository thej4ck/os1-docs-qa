"""Allowed domains CRUD for access control and limits."""

from app.db import get_conn


def list_domains() -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM allowed_domains ORDER BY pattern"
    ).fetchall()
    return [dict(r) for r in rows]


def get_domain(domain_id: int) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM allowed_domains WHERE id = ?", (domain_id,)
    ).fetchone()
    return dict(row) if row else None


def add_domain(pattern: str, daily_limit: int = 50, monthly_token_limit: int = 500_000) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO allowed_domains (pattern, daily_limit, monthly_token_limit) VALUES (?, ?, ?)",
        (pattern.strip().lower(), daily_limit, monthly_token_limit),
    )
    conn.commit()
    return cur.lastrowid


def update_domain(domain_id: int, daily_limit: int, monthly_token_limit: int, enabled: bool):
    conn = get_conn()
    conn.execute(
        "UPDATE allowed_domains SET daily_limit = ?, monthly_token_limit = ?, enabled = ? WHERE id = ?",
        (daily_limit, monthly_token_limit, int(enabled), domain_id),
    )
    conn.commit()


def delete_domain(domain_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute("DELETE FROM allowed_domains WHERE id = ?", (domain_id,))
    conn.commit()
    return cur.rowcount > 0


def is_email_allowed_by_domains(email: str) -> bool:
    """Check if email matches any enabled domain pattern."""
    domains = get_conn().execute(
        "SELECT pattern FROM allowed_domains WHERE enabled = 1"
    ).fetchall()

    if not domains:
        return False  # No domains configured = no access

    email_lower = email.lower()
    for d in domains:
        pattern = d["pattern"].lower()
        if pattern.startswith("*@"):
            if email_lower.endswith(f"@{pattern[2:]}"):
                return True
        elif pattern == email_lower:
            return True
    return False


def get_domain_for_email(email: str) -> dict | None:
    """Get the matching domain config for an email (for limits)."""
    domains = get_conn().execute(
        "SELECT * FROM allowed_domains WHERE enabled = 1"
    ).fetchall()

    email_lower = email.lower()
    for d in domains:
        pattern = d["pattern"].lower()
        if pattern.startswith("*@"):
            if email_lower.endswith(f"@{pattern[2:]}"):
                return dict(d)
        elif pattern == email_lower:
            return dict(d)
    return None


def get_daily_question_count(user_id: int) -> int:
    """Count user questions today."""
    row = get_conn().execute(
        "SELECT COUNT(*) as cnt FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id "
        "WHERE c.user_id = ? AND m.role = 'user' "
        "AND date(m.created_at) = date('now')",
        (user_id,),
    ).fetchone()
    return row["cnt"] if row else 0
