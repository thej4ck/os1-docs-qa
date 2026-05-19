"""Allowed domains CRUD for access control and limits."""

from app.db import get_conn

# Usage tier presets — single source of truth for the commercial model.
# The contractual measure is monthly requests; the monthly token limit is a
# cost-protection ceiling. 0 means unlimited but presets never use 0.
TIER_PRESETS = {
    "BASE":  {"monthly_request_limit": 100, "monthly_token_limit": 2_500_000},
    "PLUS":  {"monthly_request_limit": 300, "monthly_token_limit": 7_000_000},
    "POWER": {"monthly_request_limit": 800, "monthly_token_limit": 18_000_000},
}
DEFAULT_TIER = "BASE"


def _normalize_tier(tier: str) -> str:
    """Clamp any input to a known tier. Single owner of tier validation."""
    return tier if tier in TIER_PRESETS else DEFAULT_TIER


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


def add_domain(pattern: str, tier: str = DEFAULT_TIER) -> int:
    """Insert a domain applying the given tier preset.

    daily_limit is set to 0 (unlimited): the commercial measure is the monthly
    request count; burst is covered by the per-minute rate limit.
    """
    tier = _normalize_tier(tier)
    preset = TIER_PRESETS[tier]
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO allowed_domains "
        "(pattern, tier, monthly_request_limit, monthly_token_limit, daily_limit) "
        "VALUES (?, ?, ?, ?, 0)",
        (
            pattern.strip().lower(),
            tier,
            preset["monthly_request_limit"],
            preset["monthly_token_limit"],
        ),
    )
    conn.commit()
    return cur.lastrowid


def apply_tier(domain_id: int, tier: str):
    """Atomically apply a tier preset to an existing domain."""
    tier = _normalize_tier(tier)
    preset = TIER_PRESETS[tier]
    conn = get_conn()
    conn.execute(
        "UPDATE allowed_domains SET tier = ?, monthly_request_limit = ?, "
        "monthly_token_limit = ?, daily_limit = 0 WHERE id = ?",
        (
            tier,
            preset["monthly_request_limit"],
            preset["monthly_token_limit"],
            domain_id,
        ),
    )
    conn.commit()


def set_domain_enabled(domain_id: int, enabled: bool):
    conn = get_conn()
    conn.execute(
        "UPDATE allowed_domains SET enabled = ? WHERE id = ?",
        (int(enabled), domain_id),
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
