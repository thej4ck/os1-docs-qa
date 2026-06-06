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
            -- tier/monthly_request_limit defaults mirror domain.DEFAULT_TIER + TIER_PRESETS['BASE']
            tier        TEXT NOT NULL DEFAULT 'BASE',
            monthly_request_limit INTEGER NOT NULL DEFAULT 100,
            daily_limit INTEGER NOT NULL DEFAULT 50,
            monthly_token_limit INTEGER NOT NULL DEFAULT 500000,
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        );

        -- Shared answers: immutable snapshot of one assistant reply, emailed to an
        -- external recipient (the landing /s/{token} is public). message_id is a
        -- weak ref (ON DELETE SET NULL) so the share survives chat deletion.
        CREATE TABLE IF NOT EXISTS shares (
            token            TEXT PRIMARY KEY,
            message_id       INTEGER REFERENCES messages(id) ON DELETE SET NULL,
            conversation_id  TEXT,
            sender_user_id   INTEGER NOT NULL REFERENCES users(id),
            sender_email     TEXT NOT NULL,
            recipient_email  TEXT NOT NULL,
            recipient_name   TEXT,
            snap_content_md  TEXT NOT NULL,
            snap_sources     TEXT,
            snap_screenshots TEXT,
            snap_agent       TEXT,
            snap_question    TEXT,
            created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            expires_at       TEXT,
            revoked          INTEGER NOT NULL DEFAULT 0,
            open_count       INTEGER NOT NULL DEFAULT 0,
            view_count       INTEGER NOT NULL DEFAULT 0,
            cta_click_count  INTEGER NOT NULL DEFAULT 0,
            converted        INTEGER NOT NULL DEFAULT 0,
            converted_domain_id INTEGER,
            first_viewed_at  TEXT,
            converted_at     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_shares_sender ON shares(sender_user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_shares_recipient ON shares(recipient_email);

        CREATE VIEW IF NOT EXISTS monthly_usage AS
        SELECT
            u.id AS user_id,
            u.email,
            strftime('%Y-%m', m.created_at) AS month,
            COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.prompt_tokens ELSE 0 END), 0) AS total_prompt_tokens,
            COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.completion_tokens ELSE 0 END), 0) AS total_completion_tokens,
            COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.cost_usd ELSE 0 END), 0) AS total_cost_usd,
            COUNT(CASE WHEN m.role = 'user' THEN 1 END) AS total_questions
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        JOIN users u ON u.id = c.user_id
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
        ("cached_tokens", "INTEGER"),
        ("rerank_tokens", "INTEGER"),
        ("rerank_cost_usd", "REAL"),
        ("rerank_model", "TEXT"),
        ("agent", "TEXT"),  # esperto che ha risposto (NULL = default generico)
    ]
    for col_name, col_type in new_columns:
        if col_name not in existing:
            _conn.execute(f"ALTER TABLE messages ADD COLUMN {col_name} {col_type}")

    # Feedback table migrations
    fb_existing = {
        row[1] for row in _conn.execute("PRAGMA table_info(feedback)").fetchall()
    }
    fb_new_columns = [
        ("category", "TEXT"),           # wrong|incomplete|irrelevant|outdated|unclear
        ("comment", "TEXT"),            # free text, max 500 chars
        ("query", "TEXT"),              # the user's question
        ("response_preview", "TEXT"),   # first ~200 chars of AI response
        ("chunks_used", "TEXT"),        # JSON array of chunk sources
        ("conversation_length", "INTEGER"),  # message count in conversation
        ("model", "TEXT"),              # Groq model that answered
        ("search_scores", "TEXT"),      # JSON BM25 scores (reserved)
    ]
    for col_name, col_type in fb_new_columns:
        if col_name not in fb_existing:
            _conn.execute(f"ALTER TABLE feedback ADD COLUMN {col_name} {col_type}")

    # Users table migrations
    user_existing = {
        row[1] for row in _conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "onboarding_completed" not in user_existing:
        _conn.execute("ALTER TABLE users ADD COLUMN onboarding_completed INTEGER DEFAULT 0")
    if "access_token" not in user_existing:
        _conn.execute("ALTER TABLE users ADD COLUMN access_token TEXT")
        _conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_access_token "
            "ON users(access_token) WHERE access_token IS NOT NULL"
        )

    # Allowed_domains tier migration (additive, idempotent).
    # No mass UPDATE: pre-existing domains keep their custom
    # daily_limit/monthly_token_limit; only the column defaults apply.
    dom_existing = {
        row[1] for row in _conn.execute("PRAGMA table_info(allowed_domains)").fetchall()
    }
    if "tier" not in dom_existing:
        _conn.execute(
            "ALTER TABLE allowed_domains ADD COLUMN tier TEXT NOT NULL DEFAULT 'BASE'"
        )
    if "monthly_request_limit" not in dom_existing:
        _conn.execute(
            "ALTER TABLE allowed_domains ADD COLUMN monthly_request_limit "
            "INTEGER NOT NULL DEFAULT 100"
        )

    # Freemium / autoprovisioning columns (additive)
    freemium_cols = [
        ("expires_at", "TEXT"),
        ("company_name", "TEXT"),
        ("vat_number", "TEXT"),
        ("contact_first_name", "TEXT"),
        ("contact_last_name", "TEXT"),
        ("contact_email", "TEXT"),
        ("welcome_sent", "INTEGER DEFAULT 0"),
        ("expiry_reminder_sent", "INTEGER DEFAULT 0"),
        ("downgrade_notice_sent", "INTEGER DEFAULT 0"),
    ]
    for col_name, col_type in freemium_cols:
        if col_name not in dom_existing:
            _conn.execute(
                f"ALTER TABLE allowed_domains ADD COLUMN {col_name} {col_type}"
            )

    # Per-domain MCP gate (default abilitato): se 0, gli utenti del dominio
    # non possono usare i connettori MCP anche con auth valida.
    if "mcp_enabled" not in dom_existing:
        _conn.execute(
            "ALTER TABLE allowed_domains ADD COLUMN mcp_enabled INTEGER NOT NULL DEFAULT 1"
        )

    # ── MCP OAuth 2.1 (Authorization Server autonomo per i connettori MCP) ──
    # Additivo. Token salvati come hash sha256 (mai in chiaro a riposo).
    _conn.executescript("""
        CREATE TABLE IF NOT EXISTS oauth_clients (
            client_id     TEXT PRIMARY KEY,
            client_secret TEXT,
            redirect_uris TEXT NOT NULL,            -- json array
            grant_types   TEXT,                     -- json array
            token_endpoint_auth_method TEXT,
            scope         TEXT,
            client_name   TEXT,
            metadata      TEXT,                      -- json: full OAuthClientInformationFull
            created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );

        CREATE TABLE IF NOT EXISTS oauth_login_tickets (
            ticket         TEXT PRIMARY KEY,
            client_id      TEXT NOT NULL,
            redirect_uri   TEXT NOT NULL,
            redirect_uri_provided_explicitly INTEGER NOT NULL DEFAULT 1,
            code_challenge TEXT,
            scopes         TEXT NOT NULL,            -- json array
            state          TEXT,
            expires_at     REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS oauth_auth_codes (
            code           TEXT PRIMARY KEY,
            client_id      TEXT NOT NULL,
            redirect_uri   TEXT NOT NULL,
            redirect_uri_provided_explicitly INTEGER NOT NULL DEFAULT 1,
            code_challenge TEXT,
            scopes         TEXT NOT NULL,            -- json array
            subject        TEXT NOT NULL,            -- email utente
            expires_at     REAL NOT NULL,
            used           INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS oauth_tokens (
            token_hash  TEXT PRIMARY KEY,            -- sha256(raw token)
            kind        TEXT NOT NULL CHECK(kind IN ('access','refresh')),
            client_id   TEXT NOT NULL,
            subject     TEXT NOT NULL,
            scopes      TEXT NOT NULL,               -- json array
            expires_at  INTEGER,
            revoked     INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        );
        CREATE INDEX IF NOT EXISTS idx_oauth_tokens_kind ON oauth_tokens(kind, expires_at);
    """)

    # Migrate old model keys to new reasoning_effort variants
    _model_renames = {
        "openai/gpt-oss-120b": "openai/gpt-oss-120b:medium",
        "openai/gpt-oss-20b": "openai/gpt-oss-20b:medium",
    }
    for setting_key in ("groq_model", "groq_deep_model"):
        row = _conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (setting_key,)
        ).fetchone()
        if row and row[0] in _model_renames:
            _conn.execute(
                "UPDATE app_settings SET value = ? WHERE key = ?",
                (_model_renames[row[0]], setting_key),
            )

    # Recreate monthly_usage view (fix: question count was always 0)
    _conn.execute("DROP VIEW IF EXISTS monthly_usage")
    _conn.execute("""
        CREATE VIEW monthly_usage AS
        SELECT
            u.id AS user_id,
            u.email,
            strftime('%Y-%m', m.created_at) AS month,
            COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.prompt_tokens ELSE 0 END), 0) AS total_prompt_tokens,
            COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.completion_tokens ELSE 0 END), 0) AS total_completion_tokens,
            COALESCE(SUM(CASE WHEN m.role = 'assistant' THEN m.cost_usd ELSE 0 END), 0) AS total_cost_usd,
            COUNT(CASE WHEN m.role = 'user' THEN 1 END) AS total_questions
        FROM messages m
        JOIN conversations c ON c.id = m.conversation_id
        JOIN users u ON u.id = c.user_id
        GROUP BY u.id, strftime('%Y-%m', m.created_at)
    """)

    _conn.commit()
