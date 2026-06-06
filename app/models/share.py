"""Share CRUD: immutable snapshot of one shared answer + view/conversion tracking.

A `share` is created when a user emails an assistant answer to an external
recipient. The content is snapshotted (the public landing must stay stable even
if the source conversation changes/deletes) and counters track the marketing
funnel: email open → landing view → CTA click → trial signup.
"""

import json
import secrets

from app.db import get_conn


def create_share(
    *,
    message_id: int,
    conversation_id: str,
    sender_user_id: int,
    sender_email: str,
    recipient_email: str,
    recipient_name: str = "",
    snap_content_md: str,
    snap_sources: list | None = None,
    snap_screenshots: list | None = None,
    snap_agent: str | None = None,
    snap_question: str = "",
    expires_at: str | None = None,
) -> str:
    """Create a share with an immutable content snapshot. Returns the token."""
    token = secrets.token_urlsafe(16)
    conn = get_conn()
    conn.execute(
        "INSERT INTO shares (token, message_id, conversation_id, sender_user_id, "
        "sender_email, recipient_email, recipient_name, snap_content_md, snap_sources, "
        "snap_screenshots, snap_agent, snap_question, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            token, message_id, conversation_id, sender_user_id,
            sender_email, recipient_email, recipient_name.strip() or None,
            snap_content_md,
            json.dumps(snap_sources) if snap_sources else None,
            json.dumps(snap_screenshots) if snap_screenshots else None,
            snap_agent, snap_question.strip() or None, expires_at,
        ),
    )
    conn.commit()
    return token


def get_share(token: str) -> dict | None:
    """Active share by token (None if missing, revoked, or expired)."""
    row = get_conn().execute(
        "SELECT * FROM shares WHERE token = ? AND revoked = 0 AND "
        "(expires_at IS NULL OR expires_at > strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
        (token,),
    ).fetchone()
    return dict(row) if row else None


def _bump(token: str, column: str) -> None:
    """Increment one counter column by 1. `column` is a fixed internal literal.
    No-op on unknown token."""
    conn = get_conn()
    conn.execute(f"UPDATE shares SET {column} = {column} + 1 WHERE token = ?", (token,))
    conn.commit()


def increment_open(token: str) -> None:
    """Email-open ping (tracking pixel)."""
    _bump(token, "open_count")


def increment_cta_click(token: str) -> None:
    """CTA click ('Prova gratis')."""
    _bump(token, "cta_click_count")


def increment_view(token: str) -> None:
    """Landing-page view; stamps first_viewed_at on the first hit."""
    conn = get_conn()
    conn.execute(
        "UPDATE shares SET view_count = view_count + 1, "
        "first_viewed_at = COALESCE(first_viewed_at, strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
        "WHERE token = ?",
        (token,),
    )
    conn.commit()


def mark_converted(token: str, domain_id: int) -> None:
    """Attribute a trial signup to this share (idempotent: only first conversion sticks)."""
    conn = get_conn()
    conn.execute(
        "UPDATE shares SET converted = 1, converted_domain_id = ?, "
        "converted_at = strftime('%Y-%m-%dT%H:%M:%SZ','now') "
        "WHERE token = ? AND converted = 0",
        (domain_id, token),
    )
    conn.commit()


def revoke_share(token: str, user_id: int) -> bool:
    """Soft-delete a share (ownership-checked). Used for GDPR/abuse removal."""
    conn = get_conn()
    cur = conn.execute(
        "UPDATE shares SET revoked = 1 WHERE token = ? AND sender_user_id = ?",
        (token, user_id),
    )
    conn.commit()
    return cur.rowcount > 0


def list_shares_admin(limit: int = 200) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM shares ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_share_funnel() -> dict:
    """Aggregate funnel counters for the admin dashboard."""
    row = get_conn().execute(
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(CASE WHEN open_count > 0 THEN 1 ELSE 0 END),0) AS opened, "
        "COALESCE(SUM(CASE WHEN view_count > 0 THEN 1 ELSE 0 END),0) AS viewed, "
        "COALESCE(SUM(CASE WHEN cta_click_count > 0 THEN 1 ELSE 0 END),0) AS clicked, "
        "COALESCE(SUM(converted),0) AS converted "
        "FROM shares"
    ).fetchone()
    return dict(row) if row else {"total": 0, "opened": 0, "viewed": 0, "clicked": 0, "converted": 0}
