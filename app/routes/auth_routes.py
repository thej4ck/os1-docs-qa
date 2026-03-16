"""Authentication routes: login, OTP verification, logout."""

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth.otp import generate_otp, verify_otp, is_email_allowed, send_otp_email
from app.auth.session import create_session, clear_session, get_session_email
from app.models.user import get_or_create_user, update_last_login

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

    if verify_otp(email, code):
        get_or_create_user(email)
        update_last_login(email)
        response = RedirectResponse(url="/chat", status_code=302)
        create_session(response, email)
        return response

    return _templates().TemplateResponse(
        "verify.html", {"request": request, "email": email, "error": "Codice non valido o scaduto."}
    )


@router.get("/logout")
async def logout(request: Request):
    response = RedirectResponse(url="/login", status_code=302)
    clear_session(response)
    return response
