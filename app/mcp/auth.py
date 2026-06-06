"""MCP authentication.

M2 — bearer gate (dev/CLI): the Bearer token IS the user's existing
passwordless `access_token`. Works for Claude Code (`--header`), the Messages
API MCP connector, and MCP Inspector. It does NOT cover the claude.ai / ChatGPT
connector UIs — those require full OAuth 2.1 + PKCE (M3).

Enable with `MCP_AUTH_ENABLED=true`. When off, `/mcp` is unauthenticated
(local dev / Inspector only — never expose unauthenticated in production).
"""

from fastmcp.server.auth import AccessToken, TokenVerifier

from app.config import settings
from app.models.user import get_user_by_access_token


class OS1TokenVerifier(TokenVerifier):
    """Validate a Bearer token against the user's personal `access_token`."""

    async def verify_token(self, token: str) -> AccessToken | None:
        user = get_user_by_access_token(token)
        if not user:
            return None
        return AccessToken(
            token=token,
            client_id=user["email"],
            scopes=["docs:read"],
        )


def build_mcp_auth():
    """Return the MCP auth provider for the current config, or None (no-auth).

    Priority: OAuth 2.1 (M3, claude.ai/ChatGPT) > static bearer (M2, dev/CLI) > none.
    """
    if settings.mcp_oauth_enabled:
        from app.mcp.oauth import build_oauth_provider
        return build_oauth_provider()
    if settings.mcp_auth_enabled:
        return OS1TokenVerifier(base_url=settings.base_url or None)
    return None
