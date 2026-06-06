"""MCP authentication.

Auth mode is **admin-configurable** (app_settings `mcp_auth_mode` = off|bearer|oauth),
read at startup because the auth provider — and the OAuth routes — are mounted at
import time. Precedence: DB setting (admin) > env flags > default (oauth in prod, off in
dev). Changing the mode applies at the next service restart.

- `bearer` (M2, dev/CLI): the Bearer token IS the user's `access_token`. Covers Claude
  Code (`--header`), Messages API, Inspector. NOT the claude.ai/ChatGPT connector UIs.
- `oauth` (M3): autonomous OAuth 2.1 AS (see app/mcp/oauth.py) — claude.ai + ChatGPT.

Both modes additionally honor the per-domain `mcp_enabled` flag (allowed_domains):
a valid token whose user-domain has MCP disabled is rejected.
"""

from fastmcp.server.auth import AccessToken, TokenVerifier

from app.config import settings
from app.mcp import MCP_SCOPES
from app.models.user import get_user_by_access_token


def gate_token_by_domain(at: AccessToken | None) -> AccessToken | None:
    """Single enforcement point for the per-domain MCP gate (bearer + oauth).

    Rejects a resolved token whose user-domain has MCP disabled. The email is
    `subject` (oauth) or `client_id` (bearer)."""
    if at is None:
        return None
    from app.models.domain import is_mcp_enabled_for_email
    email = at.subject or at.client_id
    return at if (email and is_mcp_enabled_for_email(email)) else None


class OS1TokenVerifier(TokenVerifier):
    """Validate a Bearer token against the user's personal `access_token`."""

    async def verify_token(self, token: str) -> AccessToken | None:
        user = get_user_by_access_token(token)
        if not user:
            return None
        return gate_token_by_domain(
            AccessToken(token=token, client_id=user["email"], scopes=MCP_SCOPES)
        )


def resolve_mcp_auth_mode() -> str:
    """Return 'off' | 'bearer' | 'oauth'. DB (admin) > env > default."""
    from app.models.settings import read_setting_standalone
    val = read_setting_standalone("mcp_auth_mode", "").strip().lower()
    if val in ("off", "bearer", "oauth"):
        return val
    if settings.mcp_oauth_enabled:
        return "oauth"
    if settings.mcp_auth_enabled:
        return "bearer"
    return "oauth" if settings.production else "off"


def build_mcp_auth():
    """Return the MCP auth provider for the resolved mode, or None (no-auth)."""
    mode = resolve_mcp_auth_mode()
    if mode == "oauth":
        from app.mcp.oauth import build_oauth_provider
        return build_oauth_provider()
    if mode == "bearer":
        return OS1TokenVerifier(base_url=settings.base_url or None)
    return None
