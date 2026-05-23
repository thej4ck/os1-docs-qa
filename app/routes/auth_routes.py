"""Authentication routes: login, OTP verification, logout."""

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

from app.auth.otp import (
    generate_otp,
    is_email_allowed,
    send_otp_email,
    verify_cooldown_remaining,
    verify_otp,
)
from app.auth.session import (
    clear_session,
    create_session,
    get_session_email,
    login_and_redirect,
)
from app.config import settings
from app.models.user import (
    update_last_login,
    ensure_access_token,
    regenerate_access_token,
    get_user_by_access_token,
)

router = APIRouter()


def _templates():
    from app.main import templates
    return templates


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    email = get_session_email(request)
    if email:
        return RedirectResponse(url="/chat", status_code=302)
    return _templates().TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, email: str = Form(...)):
    email = email.strip().lower()
    if not is_email_allowed(email):
        return _templates().TemplateResponse(
            "login.html", {"request": request, "error": "Email non autorizzata."}
        )

    # Dev/local bypass: no OTP, log in immediately on email submit.
    if not settings.production:
        print(f"[auth] DEV bypass login: {email} (PRODUCTION=false)", flush=True)
        return login_and_redirect(email)

    code = generate_otp(email)
    success = send_otp_email(email, code)
    if not success:
        return _templates().TemplateResponse(
            "login.html", {"request": request, "error": "Errore nell'invio dell'email. Riprova."}
        )

    return _templates().TemplateResponse(
        "verify.html", {"request": request, "email": email, "error": None}
    )


@router.post("/verify", response_class=HTMLResponse)
async def verify_submit(request: Request, email: str = Form(...), code: str = Form(...)):
    email = email.strip().lower()
    code = code.strip()

    wait = verify_cooldown_remaining(email)
    if wait > 0:
        return _templates().TemplateResponse(
            "verify.html",
            {
                "request": request, "email": email,
                "error": f"Troppi tentativi. Riprova tra {wait} secondi.",
            },
        )

    if verify_otp(email, code):
        return login_and_redirect(email)

    return _templates().TemplateResponse(
        "verify.html", {"request": request, "email": email, "error": "Codice non valido o scaduto."}
    )


@router.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=302)
    clear_session(response)
    return response


# ── Passwordless access via personal token (for OS1 embedded webview) ──

def _public_base(request: Request) -> str:
    """Absolute base URL: configured BASE_URL, else inferred from the request."""
    if settings.base_url:
        return settings.base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _access_url(request: Request, token: str) -> str:
    return f"{_public_base(request)}/login/token?t={token}"


@router.get("/login/token")
async def login_with_token(request: Request, t: str = ""):
    """Establish a session from a personal access token, then strip the token
    from the URL by redirecting to a clean /chat (token never stays in the
    address bar or browser history beyond this single redirect)."""
    user = get_user_by_access_token(t)
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    update_last_login(user["email"])
    response = RedirectResponse(url="/chat", status_code=302)
    create_session(response, user["email"])
    return response


@router.get("/api/access-token")
async def get_access_token(request: Request):
    email = get_session_email(request)
    if not email:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)
    token = ensure_access_token(email)
    if not token:
        return JSONResponse({"error": "Utente non trovato."}, status_code=404)
    return JSONResponse({"url": _access_url(request, token)})


@router.post("/api/access-token/regenerate")
async def regenerate_access_token_route(request: Request):
    email = get_session_email(request)
    if not email:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)
    token = regenerate_access_token(email)
    if not token:
        return JSONResponse({"error": "Utente non trovato."}, status_code=404)
    return JSONResponse({"url": _access_url(request, token)})
