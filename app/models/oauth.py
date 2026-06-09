"""Storage for the autonomous MCP OAuth 2.1 Authorization Server.

Pure DB access over the `oauth_*` tables (see app/db.py). Tokens are stored as
sha256 hashes — raw tokens never persist at rest. Higher-level OAuth logic
(the FastMCP OAuthProvider) lives in app/mcp/oauth.py.
"""

import hashlib
import json
import secrets
import time

from app.db import get_conn


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


# ── Clients (Dynamic Client Registration) ──

def save_client(client: dict) -> None:
    get_conn().execute(
        "INSERT OR REPLACE INTO oauth_clients "
        "(client_id, client_secret, redirect_uris, grant_types, "
        " token_endpoint_auth_method, scope, client_name, metadata) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            client["client_id"],
            client.get("client_secret"),
            json.dumps(client.get("redirect_uris") or []),
            json.dumps(client.get("grant_types") or []),
            client.get("token_endpoint_auth_method"),
            client.get("scope"),
            client.get("client_name"),
            json.dumps(client.get("metadata") or {}),
        ),
    )
    get_conn().commit()


def get_client(client_id: str) -> dict | None:
    row = get_conn().execute(
        "SELECT metadata FROM oauth_clients WHERE client_id = ?", (client_id,)
    ).fetchone()
    return json.loads(row["metadata"]) if row else None


# ── Login tickets (pending authorize → OTP login) ──

def create_login_ticket(
    client_id: str, redirect_uri: str, redirect_uri_explicit: bool,
    code_challenge: str | None, scopes: list[str], state: str | None,
    ttl: int = 600,
) -> str:
    ticket = secrets.token_urlsafe(24)
    get_conn().execute(
        "INSERT INTO oauth_login_tickets "
        "(ticket, client_id, redirect_uri, redirect_uri_provided_explicitly, "
        " code_challenge, scopes, state, expires_at) VALUES (?,?,?,?,?,?,?,?)",
        (ticket, client_id, redirect_uri, int(redirect_uri_explicit),
         code_challenge, json.dumps(scopes), state, time.time() + ttl),
    )
    get_conn().commit()
    return ticket


def get_login_ticket(ticket: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM oauth_login_tickets WHERE ticket = ?", (ticket,)
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] < time.time():
        delete_login_ticket(ticket)
        return None
    d = dict(row)
    d["scopes"] = json.loads(d["scopes"])
    d["redirect_uri_provided_explicitly"] = bool(d["redirect_uri_provided_explicitly"])
    return d


def delete_login_ticket(ticket: str) -> None:
    get_conn().execute("DELETE FROM oauth_login_tickets WHERE ticket = ?", (ticket,))
    get_conn().commit()


# ── Authorization codes (single-use, short TTL) ──

def create_auth_code(
    code: str, client_id: str, redirect_uri: str, redirect_uri_explicit: bool,
    code_challenge: str | None, scopes: list[str], subject: str, ttl: int = 60,
) -> None:
    get_conn().execute(
        "INSERT INTO oauth_auth_codes "
        "(code, client_id, redirect_uri, redirect_uri_provided_explicitly, "
        " code_challenge, scopes, subject, expires_at, used) "
        "VALUES (?,?,?,?,?,?,?,?,0)",
        (code, client_id, redirect_uri, int(redirect_uri_explicit),
         code_challenge, json.dumps(scopes), subject, time.time() + ttl),
    )
    get_conn().commit()


def get_auth_code(code: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM oauth_auth_codes WHERE code = ? AND used = 0", (code,)
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] < time.time():
        return None
    d = dict(row)
    d["scopes"] = json.loads(d["scopes"])
    d["redirect_uri_provided_explicitly"] = bool(d["redirect_uri_provided_explicitly"])
    return d


def consume_auth_code(code: str) -> None:
    get_conn().execute("UPDATE oauth_auth_codes SET used = 1 WHERE code = ?", (code,))
    get_conn().commit()


# ── Tokens (hashed at rest) ──

def store_token(
    raw: str, kind: str, client_id: str, subject: str,
    scopes: list[str], expires_at: int | None,
) -> None:
    get_conn().execute(
        "INSERT OR REPLACE INTO oauth_tokens "
        "(token_hash, kind, client_id, subject, scopes, expires_at, revoked) "
        "VALUES (?,?,?,?,?,?,0)",
        (_hash(raw), kind, client_id, subject, json.dumps(scopes), expires_at),
    )
    get_conn().commit()


def get_token(raw: str, kind: str) -> dict | None:
    row = get_conn().execute(
        "SELECT * FROM oauth_tokens WHERE token_hash = ? AND kind = ? AND revoked = 0",
        (_hash(raw), kind),
    ).fetchone()
    if not row:
        return None
    if row["expires_at"] is not None and row["expires_at"] < time.time():
        return None
    d = dict(row)
    d["scopes"] = json.loads(d["scopes"])
    return d


def revoke_token(raw: str) -> None:
    get_conn().execute(
        "UPDATE oauth_tokens SET revoked = 1 WHERE token_hash = ?", (_hash(raw),)
    )
    get_conn().commit()


def purge_expired() -> None:
    """Best-effort cleanup of expired codes/tickets (call opportunistically)."""
    now = time.time()
    c = get_conn()
    c.execute("DELETE FROM oauth_login_tickets WHERE expires_at < ?", (now,))
    c.execute("DELETE FROM oauth_auth_codes WHERE expires_at < ? OR used = 1", (now,))
    c.commit()


def purge_expired_tokens() -> int:
    """Cancella i token scaduti o revocati (access 1h, refresh 30g ruotati).
    Evita che le righe restino a lungo. Ritorna quante righe eliminate."""
    now = int(time.time())
    c = get_conn()
    cur = c.execute(
        "DELETE FROM oauth_tokens "
        "WHERE revoked = 1 OR (expires_at IS NOT NULL AND expires_at < ?)",
        (now,),
    )
    c.commit()
    return cur.rowcount


# ── MCP usage (richieste per utente) ──

def bump_mcp_usage(subject: str) -> None:
    """Incrementa il contatore richieste MCP per l'utente (subject = email)."""
    if not subject:
        return
    c = get_conn()
    c.execute(
        "INSERT INTO mcp_usage (subject, request_count, last_request_at) "
        "VALUES (?, 1, strftime('%Y-%m-%dT%H:%M:%SZ','now')) "
        "ON CONFLICT(subject) DO UPDATE SET "
        "request_count = request_count + 1, "
        "last_request_at = strftime('%Y-%m-%dT%H:%M:%SZ','now')",
        (subject,),
    )
    c.commit()


# ── Admin views (read/revoke) ──

def list_clients() -> list[dict]:
    rows = get_conn().execute(
        "SELECT client_id, client_name, redirect_uris, created_at "
        "FROM oauth_clients ORDER BY created_at DESC"
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["redirect_uris"] = json.loads(d["redirect_uris"])
        except Exception:
            d["redirect_uris"] = []
        out.append(d)
    return out


def delete_client(client_id: str) -> None:
    """Remove a registered client and revoke all its tokens."""
    c = get_conn()
    c.execute("DELETE FROM oauth_clients WHERE client_id = ?", (client_id,))
    c.execute("UPDATE oauth_tokens SET revoked = 1 WHERE client_id = ?", (client_id,))
    c.commit()


def counts() -> dict:
    now = int(time.time())
    c = get_conn()
    clients = c.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0]
    users = c.execute(
        "SELECT COUNT(DISTINCT subject) FROM oauth_tokens "
        "WHERE revoked = 0 AND (expires_at IS NULL OR expires_at > ?)",
        (now,),
    ).fetchone()[0]
    return {"clients": clients, "users": users}


def list_mcp_users() -> list[dict]:
    """Utenti con MCP attivo (almeno un token valido), con richieste totali.

    Una sola query: aggrega per subject i token non revocati/non scaduti e
    aggancia il contatore richieste (mcp_usage). last_seen = max(ultima
    richiesta, ultimo token emesso)."""
    now = int(time.time())
    rows = get_conn().execute(
        "SELECT t.subject AS subject, COUNT(*) AS tokens_active, "
        "       MAX(t.created_at) AS last_token_at, "
        "       COALESCE(u.request_count, 0) AS requests, "
        "       u.last_request_at AS last_request_at "
        "FROM oauth_tokens t "
        "LEFT JOIN mcp_usage u ON u.subject = t.subject "
        "WHERE t.revoked = 0 AND (t.expires_at IS NULL OR t.expires_at > ?) "
        "GROUP BY t.subject "
        "ORDER BY requests DESC, last_token_at DESC",
        (now,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        seen = [x for x in (d.get("last_request_at"), d.get("last_token_at")) if x]
        d["last_seen"] = max(seen) if seen else None
        out.append(d)
    return out


def revoke_by_subject(subject: str) -> int:
    """Revoke ALL active tokens for a user (email). Returns how many revoked.
    Used by the user-facing self-revoke (disconnect my MCP connectors)."""
    c = get_conn()
    cur = c.execute(
        "UPDATE oauth_tokens SET revoked = 1 WHERE subject = ? AND revoked = 0", (subject,)
    )
    c.commit()
    return cur.rowcount


def active_count_for_subject(subject: str) -> int:
    return get_conn().execute(
        "SELECT COUNT(*) FROM oauth_tokens WHERE subject = ? AND revoked = 0", (subject,)
    ).fetchone()[0]
