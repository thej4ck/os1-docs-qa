"""App database manager for user data, conversations, and usage tracking."""

import sqlite3
from pathlib import Path


_conn: sqlite3.Connection | None = None


def init(db_path: str):
    """Open (or create) the app database and ensure schema exists."""
    global _conn
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.execute("PRAGMA foreign_keys=ON")
    _create_schema()


def get_conn() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("App database not initialized. Call db.init() first.")
    return _conn


def close():
    global _conn
    if _conn:
        _conn.close()
        _conn = None


def _create_schema():
    assert _conn is not None
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT NOT NULL UNIQUE,
            is_admin    INTEGER NOT NULL DEFAULT 0,
            monthly_token_limit INTEGER,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            last_login  TEXT
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id          TEXT PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id),
            title       TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS idx_conversations_user
            ON conversations(user_id, updated_at DESC);

        CREATE TABLE IF NOT EXISTS messages (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id   TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role              TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content           TEXT NOT NULL,
            sources           TEXT,
            prompt_tokens     INTEGER,
            completion_tokens INTEGER,
            cost_usd          REAL,
            created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS idx_messages_conv
            ON messages(conversation_id, created_at);

        CREATE TABLE IF NOT EXISTS feedback (
            message_id  INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
            rating      INTEGER NOT NULL CHECK(rating IN (-1, 1)),
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS app_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS allowed_domains (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern     TEXT NOT NULL UNIQUE,
            daily_limit INTEGER NOT NULL DEFAULT 50,
            monthly_token_limit INTEGER NOT NULL DEFAULT 500000,
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        CREATE VIEW IF NOT EXISTS monthly_usage AS
        SELECT
            u.id AS user_id,
            u.email,
            strftime('%Y-%m', m.created_at) AS month,
            COALESCE(SUM(m.prompt_tokens), 0) AS total_prompt_tokens,
            COALESCE(SUM(m.completion_tokens), 0) AS total_completion_tokens,
            COALESCE(SUM(m.cost_usd), 0) AS total_cost_usd,
            COUNT(CASE WHEN m.role = 'user' THEN 1 END) AS total_questions
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        JOIN users u ON u.id = c.user_id
        WHERE m.role = 'assistant'
        GROUP BY u.id, strftime('%Y-%m', m.created_at);
    """)
    _conn.commit()
    _migrate()


def _migrate():
    """Additive migrations — safe to run repeatedly."""
    assert _conn is not None
    existing = {
        row[1] for row in _conn.execute("PRAGMA table_info(messages)").fetchall()
    }
    new_columns = [
        ("model", "TEXT"),
        ("rerank_tokens", "INTEGER"),
        ("rerank_cost_usd", "REAL"),
        ("rerank_model", "TEXT"),
    ]
    for col_name, col_type in new_columns:
        if col_name not in existing:
            _conn.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type}")
    _conn.commit()
