"""Autonomous OAuth 2.1 Authorization Server for MCP connectors.

`OS1OAuthProvider` subclasses FastMCP's `OAuthProvider`: the framework handles
discovery (RFC 8414/9728), DCR (RFC 7591) and PKCE S256 verification; we
implement only storage + the user-login step. Login is delegated to a dedicated
page `/mcp-login` (see app/routes/mcp_auth_routes.py) that reuses the existing
email-OTP primitives — no external IdP (the product is sold openly).

Flow: /authorize → create login ticket → redirect /mcp-login → email+OTP →
AuthorizationCode(subject=email) → client redirect → /token (PKCE by framework)
→ access+refresh (hashed at rest) → verify on every request.
"""

import time

from fastmcp.server.auth import OAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.config import settings
from app.mcp import MCP_SCOPES as SCOPES
from app.mcp.auth import gate_token_by_domain
from app.models import oauth as store

ACCESS_TTL = 3600              # 1h
REFRESH_TTL = 30 * 24 * 3600   # 30d
LOGIN_PATH = "/mcp-login"      # root-level (the /mcp mount would shadow /mcp/login)


class OS1OAuthProvider(OAuthProvider):
    # ── Clients (DCR) ──
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = store.get_client(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        store.save_client({
            "client_id": client_info.client_id,
            "client_secret": client_info.client_secret,
            "redirect_uris": [str(u) for u in (client_info.redirect_uris or [])],
            "grant_types": list(client_info.grant_types or []),
            "token_endpoint_auth_method": client_info.token_endpoint_auth_method,
            "scope": client_info.scope,
            "client_name": client_info.client_name,
            "metadata": client_info.model_dump(mode="json"),
        })

    # ── Authorize → delegate to OTP login page ──
    async def authorize(self, client: OAuthClientInformationFull, params) -> str:
        ticket = store.create_login_ticket(
            client_id=client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_explicit=params.redirect_uri_provided_explicitly,
            code_challenge=params.code_challenge,
            scopes=SCOPES,
            state=params.state,
        )
        return construct_redirect_uri(LOGIN_PATH, ticket=ticket)

    # ── Authorization codes ──
    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        d = store.get_auth_code(authorization_code)
        if not d or d["client_id"] != client.client_id:
            return None
        return AuthorizationCode(
            code=d["code"],
            scopes=d["scopes"],
            expires_at=d["expires_at"],
            client_id=d["client_id"],
            code_challenge=d["code_challenge"],
            redirect_uri=d["redirect_uri"],
            redirect_uri_provided_explicitly=d["redirect_uri_provided_explicitly"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        d = store.get_auth_code(authorization_code.code)
        if not d:
            raise TokenError("invalid_grant", "Authorization code not found or used.")
        store.consume_auth_code(authorization_code.code)  # single-use
        return self._issue(client.client_id, d["subject"], authorization_code.scopes)

    # ── Refresh ──
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        d = store.get_token(refresh_token, "refresh")
        if not d or d["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token, client_id=d["client_id"],
            scopes=d["scopes"], expires_at=d["expires_at"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull,
        refresh_token: RefreshToken, scopes: list[str],
    ) -> OAuthToken:
        d = store.get_token(refresh_token.token, "refresh")
        if not d:
            raise TokenError("invalid_grant", "Refresh token not found.")
        store.revoke_token(refresh_token.token)  # rotation
        return self._issue(client.client_id, d["subject"], scopes or refresh_token.scopes)

    # ── Access tokens ──
    async def load_access_token(self, token: str) -> AccessToken | None:
        d = store.get_token(token, "access")
        if not d:
            return None
        return AccessToken(
            token=token, client_id=d["client_id"], scopes=d["scopes"],
            expires_at=d["expires_at"], subject=d["subject"],
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return gate_token_by_domain(await self.load_access_token(token))

    async def revoke_token(self, token) -> None:
        store.revoke_token(token.token)

    # ── helper ──
    def _issue(self, client_id: str, subject: str, scopes: list[str]) -> OAuthToken:
        scopes = scopes or SCOPES
        access, refresh = store.new_token(), store.new_token()
        now = int(time.time())
        store.store_token(access, "access", client_id, subject, scopes, now + ACCESS_TTL)
        store.store_token(refresh, "refresh", client_id, subject, scopes, now + REFRESH_TTL)
        return OAuthToken(
            access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
            refresh_token=refresh, scope=" ".join(scopes),
        )


def build_oauth_provider() -> OS1OAuthProvider:
    base = (settings.base_url or "http://localhost:8000").rstrip("/")
    return OS1OAuthProvider(
        base_url=f"{base}/mcp",
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=SCOPES, default_scopes=SCOPES,
        ),
        revocation_options=RevocationOptions(enabled=True),
        required_scopes=None,
    )
