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
