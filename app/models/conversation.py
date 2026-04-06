"""Conversation and message CRUD operations."""

import json
import uuid

from app.db import get_conn


def create_conversation(user_id: int, title: str = "") -> str:
    """Create a new conversation. Returns the conversation ID."""
    conv_id = uuid.uuid4().hex
    get_conn().execute(
        "INSERT INTO conversations (id, user_id, title) VALUES (?, ?, ?)",
        (conv_id, user_id, title),
    )
    get_conn().commit()
    return conv_id


def list_conversations(user_id: int, limit: int = 50) -> list[dict]:
    rows = get_conn().execute(
        "SELECT id, title, created_at, updated_at FROM conversations "
        "WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: str, user_id: int) -> dict | None:
    """Get conversation with ownership check."""
    row = get_conn().execute(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def get_conversation_any(conv_id: str) -> dict | None:
    """Get conversation without ownership check (admin use)."""
    row = get_conn().execute(
        "SELECT c.*, u.email FROM conversations c JOIN users u ON u.id = c.user_id WHERE c.id = ?",
        (conv_id,),
    ).fetchone()
    return dict(row) if row else None


def get_messages(conv_id: str, limit: int | None = None) -> list[dict]:
    if limit:
        rows = get_conn().execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at DESC LIMIT ?",
            (conv_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]
    rows = get_conn().execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at",
        (conv_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_messages_for_llm(conv_id: str, max_messages: int = 10) -> list[dict]:
    """Get last N messages formatted for LLM context."""
    messages = get_messages(conv_id, limit=max_messages)
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def add_message(
    conv_id: str,
    role: str,
    content: str,
    sources: list | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: float | None = None,
    model: str | None = None,
    cached_tokens: int | None = None,
    rerank_tokens: int | None = None,
    rerank_cost_usd: float | None = None,
    rerank_model: str | None = None,
) -> int:
    """Add a message and update conversation timestamp. Returns message ID."""
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO messages (conversation_id, role, content, sources, "
        "prompt_tokens, completion_tokens, cost_usd, model, cached_tokens, "
        "rerank_tokens, rerank_cost_usd, rerank_model) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (conv_id, role, content, json.dumps(sources) if sources else None,
         prompt_tokens, completion_tokens, cost_usd, model, cached_tokens,
         rerank_tokens, rerank_cost_usd, rerank_model),
    )
    conn.execute(
        "UPDATE conversations SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
        (conv_id,),
    )
    conn.commit()
    return cur.lastrowid


def update_title(conv_id: str, title: str):
    get_conn().execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))
    get_conn().commit()


def count_user_messages(conv_id: str) -> int:
    """Count user messages in a conversation (for limit checking)."""
    row = get_conn().execute(
        "SELECT COUNT(*) as cnt FROM messages WHERE conversation_id = ? AND role = 'user'",
        (conv_id,),
    ).fetchone()
    return row["cnt"] if row else 0


def get_max_messages_setting() -> int:
    """Get max messages per conversation from app_settings or config default."""
    from app.config import settings
    row = get_conn().execute(
        "SELECT value FROM app_settings WHERE key = 'max_messages_per_conversation'"
    ).fetchone()
    if row:
        try:
            return int(row["value"])
        except (ValueError, TypeError):
            pass
    return settings.default_max_messages_per_conversation


def delete_conversation(conv_id: str, user_id: int) -> bool:
    conn = get_conn()
    cur = conn.execute(
        "DELETE FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, user_id),
    )
    conn.commit()
    return cur.rowcount > 0
