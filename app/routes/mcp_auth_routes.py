"""OAuth login page for MCP connectors (`/mcp-login`).

Dedicated page (NOT the cookie login — the completion differs: it redirects back
to the OAuth client with an authorization code, not to /chat). Reuses the email
+ OTP primitives. Lives at root `/mcp-login` because the `/mcp` mount would
shadow any `/mcp/...` route.

Flow: GET ?ticket → email form → (POST email) send OTP → (POST code) verify →
create AuthorizationCode(subject=email) → 302 to client redirect_uri?code&state.
"""

import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp.server.auth.provider import construct_redirect_uri

from app.auth.otp import (
    generate_otp,
    is_email_allowed,
    send_otp_email,
    verify_cooldown_remaining,
    verify_otp,
)
from app.config import settings
from app.models import oauth as store
from app.models.user import get_or_create_user
from app.version import PRODUCT_NAME

router = APIRouter()


def _templates():
    from app.main import templates
    return templates


def _page(request: Request, ticket: str, stage: str, email: str = "", error=None):
    return _templates().TemplateResponse(
        request,  # Starlette 1.x: request è il primo arg
        "mcp_login.html",
        {"request": request, "ticket": ticket, "stage": stage,
         "email": email, "error": error, "product": PRODUCT_NAME},
    )


def _complete(ticket: str, t: dict, email: str) -> RedirectResponse:
    """Issue the authorization code and redirect back to the OAuth client."""
    get_or_create_user(email)
    code = secrets.token_urlsafe(24)
    store.create_auth_code(
        code=code,
        client_id=t["client_id"],
        redirect_uri=t["redirect_uri"],
        redirect_uri_explicit=t["redirect_uri_provided_explicitly"],
        code_challenge=t["code_challenge"],
        scopes=t["scopes"],
        subject=email,
    )
    store.delete_login_ticket(ticket)
    url = construct_redirect_uri(t["redirect_uri"], code=code, state=t.get("state"))
    return RedirectResponse(url=url, status_code=302)


@router.get("/mcp-login", response_class=HTMLResponse)
async def mcp_login_page(request: Request, ticket: str = ""):
    if not store.get_login_ticket(ticket):
        return HTMLResponse("Richiesta di autorizzazione non valida o scaduta.", status_code=400)
    return _page(request, ticket, stage="email")


@router.post("/mcp-login", response_class=HTMLResponse)
async def mcp_login_submit(
    request: Request,
    ticket: str = Form(...),
    step: str = Form(...),
    email: str = Form(""),
    code: str = Form(""),
):
    t = store.get_login_ticket(ticket)
    if not t:
        return HTMLResponse("Richiesta di autorizzazione non valida o scaduta.", status_code=400)
    email = email.strip().lower()

    if step == "email":
        if not is_email_allowed(email):
            return _page(request, ticket, "email", email, "Email non autorizzata.")
        # Dev/local bypass: no OTP (mirrors /login), disabled in production.
        if not settings.production:
            return _complete(ticket, t, email)
        otp = generate_otp(email)
        if not send_otp_email(email, otp):
            return _page(request, ticket, "email", email, "Errore nell'invio dell'email. Riprova.")
        return _page(request, ticket, "otp", email)

    if step == "otp":
        wait = verify_cooldown_remaining(email)
        if wait > 0:
            return _page(request, ticket, "otp", email, f"Troppi tentativi. Riprova tra {wait}s.")
        if verify_otp(email, code.strip()):
            return _complete(ticket, t, email)
        return _page(request, ticket, "otp", email, "Codice non valido o scaduto.")

    return _page(request, ticket, "email", email, "Richiesta non valida.")
