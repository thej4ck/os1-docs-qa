"""Usage aggregation and monthly limit checking."""

from datetime import datetime, timezone

from app.config import settings
from app.db import get_conn


def get_current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def get_monthly_usage(user_id: int, month: str | None = None) -> dict:
    """Get a user's usage for a specific month."""
    month = month or get_current_month()
    row = get_conn().execute(
        "SELECT * FROM monthly_usage WHERE user_id = ? AND month = ?",
        (user_id, month),
    ).fetchone()
    if row:
        return dict(row)
    return {
        "user_id": user_id,
        "month": month,
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
        "total_cost_usd": 0.0,
        "total_questions": 0,
    }


def check_limit(user_id: int) -> tuple[bool, dict]:
    """Check if user is within monthly limit. Returns (allowed, usage_info)."""
    usage = get_monthly_usage(user_id)
    total_tokens = usage["total_prompt_tokens"] + usage["total_completion_tokens"]

    # Get per-user limit or fall back to global default
    row = get_conn().execute(
        "SELECT monthly_token_limit FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    limit = row["monthly_token_limit"] if row and row["monthly_token_limit"] is not None else settings.default_monthly_token_limit

    usage["total_tokens"] = total_tokens
    usage["limit"] = limit
    usage["remaining"] = max(0, limit - total_tokens) if limit > 0 else float("inf")

    # 0 = unlimited
    return (limit == 0 or total_tokens < limit), usage


def get_all_usage(month: str | None = None) -> list[dict]:
    """Get all users' usage for a month (admin)."""
    month = month or get_current_month()
    rows = get_conn().execute(
        "SELECT * FROM monthly_usage WHERE month = ? ORDER BY total_cost_usd DESC",
        (month,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_usage_summary(month: str | None = None) -> dict:
    """Get aggregate summary across all users for a month."""
    month = month or get_current_month()
    row = get_conn().execute(
        "SELECT COUNT(DISTINCT user_id) AS active_users, "
        "COALESCE(SUM(total_prompt_tokens), 0) AS total_prompt, "
        "COALESCE(SUM(total_completion_tokens), 0) AS total_completion, "
        "COALESCE(SUM(total_cost_usd), 0) AS total_cost, "
        "COALESCE(SUM(total_questions), 0) AS total_questions "
        "FROM monthly_usage WHERE month = ?",
        (month,),
    ).fetchone()
    return dict(row) if row else {
        "active_users": 0, "total_prompt": 0, "total_completion": 0,
        "total_cost": 0.0, "total_questions": 0,
    }


def get_domain_usage(month: str | None = None) -> list[dict]:
    """Get usage aggregated by email domain for a month."""
    month = month or get_current_month()
    rows = get_conn().execute(
        "SELECT "
        "  SUBSTR(u.email, INSTR(u.email, '@') + 1) AS domain, "
        "  COUNT(DISTINCT u.id) AS users, "
        "  COALESCE(SUM(m.total_prompt_tokens), 0) AS total_prompt_tokens, "
        "  COALESCE(SUM(m.total_completion_tokens), 0) AS total_completion_tokens, "
        "  COALESCE(SUM(m.total_cost_usd), 0) AS total_cost_usd, "
        "  COALESCE(SUM(m.total_questions), 0) AS total_questions "
        "FROM monthly_usage m "
        "JOIN users u ON u.id = m.user_id "
        "WHERE m.month = ? "
        "GROUP BY domain "
        "ORDER BY total_cost_usd DESC",
        (month,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_cost_by_model(start_date: str, end_date: str) -> list[dict]:
    """Cost breakdown by model for a date range."""
    rows = get_conn().execute(
        "SELECT "
        "  COALESCE(m.model, 'sconosciuto') AS model, "
        "  COUNT(*) AS requests, "
        "  COALESCE(SUM(m.prompt_tokens), 0) AS prompt_tokens, "
        "  COALESCE(SUM(m.completion_tokens), 0) AS completion_tokens, "
        "  COALESCE(SUM(m.cost_usd), 0) AS cost_usd, "
        "  COALESCE(SUM(m.rerank_tokens), 0) AS rerank_tokens, "
        "  COALESCE(SUM(m.rerank_cost_usd), 0) AS rerank_cost_usd "
        "FROM messages m "
        "WHERE m.role = 'assistant' AND m.created_at >= ? AND m.created_at < ? "
        "GROUP BY COALESCE(m.model, 'sconosciuto') "
        "ORDER BY cost_usd DESC",
        (start_date, end_date),
    ).fetchall()
    return [dict(r) for r in rows]


def get_cost_by_day(start_date: str, end_date: str) -> list[dict]:
    """Daily cost totals for a date range."""
    rows = get_conn().execute(
        "SELECT "
        "  DATE(m.created_at) AS day, "
        "  COUNT(*) AS requests, "
        "  COALESCE(SUM(m.prompt_tokens), 0) + COALESCE(SUM(m.completion_tokens), 0) AS tokens, "
        "  COALESCE(SUM(m.cost_usd), 0) AS cost_usd "
        "FROM messages m "
        "WHERE m.role = 'assistant' AND m.created_at >= ? AND m.created_at < ? "
        "GROUP BY DATE(m.created_at) "
        "ORDER BY day DESC",
        (start_date, end_date),
    ).fetchall()
    return [dict(r) for r in rows]


def get_cost_by_user(start_date: str, end_date: str, limit: int = 20) -> list[dict]:
    """Top users by cost for a date range."""
    rows = get_conn().execute(
        "SELECT "
        "  u.email, "
        "  SUBSTR(u.email, INSTR(u.email, '@') + 1) AS domain, "
        "  COUNT(*) AS requests, "
        "  COALESCE(SUM(m.prompt_tokens), 0) + COALESCE(SUM(m.completion_tokens), 0) AS tokens, "
        "  COALESCE(SUM(m.cost_usd), 0) AS cost_usd "
        "FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id "
        "JOIN users u ON u.id = c.user_id "
        "WHERE m.role = 'assistant' AND m.created_at >= ? AND m.created_at < ? "
        "GROUP BY u.id "
        "ORDER BY cost_usd DESC "
        "LIMIT ?",
        (start_date, end_date, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_cost_summary(start_date: str, end_date: str) -> dict:
    """Aggregate cost summary for a date range."""
    row = get_conn().execute(
        "SELECT "
        "  COUNT(*) AS requests, "
        "  COALESCE(SUM(m.prompt_tokens), 0) AS prompt_tokens, "
        "  COALESCE(SUM(m.completion_tokens), 0) AS completion_tokens, "
        "  COALESCE(SUM(m.cost_usd), 0) AS cost_usd, "
        "  COALESCE(SUM(m.rerank_tokens), 0) AS rerank_tokens, "
        "  COALESCE(SUM(m.rerank_cost_usd), 0) AS rerank_cost_usd "
        "FROM messages m "
        "WHERE m.role = 'assistant' AND m.created_at >= ? AND m.created_at < ?",
        (start_date, end_date),
    ).fetchone()
    d = dict(row) if row else {
        "requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "cost_usd": 0.0, "rerank_tokens": 0, "rerank_cost_usd": 0.0,
    }
    if d["requests"] > 0:
        d["avg_cost"] = d["cost_usd"] / d["requests"]
    else:
        d["avg_cost"] = 0.0
    return d


def get_recent_questions(limit: int = 50) -> list[dict]:
    """Get recent questions across all users (admin dashboard)."""
    rows = get_conn().execute(
        "SELECT m.content, m.created_at, u.email, c.id AS conversation_id "
        "FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id "
        "JOIN users u ON u.id = c.user_id "
        "WHERE m.role = 'user' "
        "ORDER BY m.created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
