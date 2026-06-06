"""Self-signup freemium flow: signup → OTP verify → autoprovision TRIAL."""

import re

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import HTMLResponse

from app.auth.email_sender import (
    get_admin_notification_email,
    get_trial_duration_days,
    send_email,
)
from app.auth.email_templates import admin_new_signup, welcome_trial
from app.auth.otp import generate_otp, send_otp_email, verify_cooldown_remaining, verify_otp
from app.auth.session import login_and_redirect
from app.models.domain import (
    TIER_PRESETS,
    TIER_TRIAL,
    add_domain_trial,
    extract_email_domain,
    get_domain,
    get_domain_by_pattern,
    is_personal_email_domain,
    mark_flag,
)

router = APIRouter()

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _templates():
    from app.main import templates
    return templates


def _render_signup(request: Request, **ctx):
    base = {
        "request": request,
        "error": None,
        "first_name": "",
        "last_name": "",
        "email": "",
        "company_name": "",
    }
    base.update(ctx)
    return _templates().TemplateResponse(request, "signup.html", base)


def _render_verify(request: Request, **ctx):
    base = {"request": request, "error": None}
    base.update(ctx)
    return _templates().TemplateResponse(request, "signup_verify.html", base)


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    return _render_signup(request)


@router.post("/signup", response_class=HTMLResponse)
async def signup_submit(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    company_name: str = Form(...),
    gdpr: str = Form(""),
):
    first_name = first_name.strip()
    last_name = last_name.strip()
    email = email.strip().lower()
    company_name = company_name.strip()

    form_ctx = dict(
        first_name=first_name, last_name=last_name, email=email,
        company_name=company_name,
    )

    if not (first_name and last_name and email and company_name):
        return _render_signup(request, error="Compila tutti i campi.", **form_ctx)

    if not _EMAIL_RE.match(email):
        return _render_signup(request, error="Email non valida.", **form_ctx)

    if gdpr != "on":
        return _render_signup(
            request,
            error="Devi accettare l'informativa privacy per proseguire.",
            **form_ctx,
        )

    if is_personal_email_domain(email):
        return _render_signup(
            request,
            error=(
                "Le email personali (gmail, yahoo, hotmail, outlook, libero, ...) "
                "non sono accettate. Usa un'email aziendale. Eventuali iscrizioni "
                "con email personali saranno disabilitate."
            ),
            **form_ctx,
        )

    domain = extract_email_domain(email)
    pattern = f"*@{domain}"

    if get_domain_by_pattern(pattern):
        return _render_signup(
            request,
            error="Il dominio è già registrato. Usa il login standard.",
            **form_ctx,
        )

    code = generate_otp(email)
    if not send_otp_email(email, code):
        return _render_signup(
            request,
            error="Errore nell'invio dell'email. Riprova tra qualche istante.",
            **form_ctx,
        )

    return _render_verify(request, **form_ctx)


@router.post("/signup/verify", response_class=HTMLResponse)
async def signup_verify(
    request: Request,
    background_tasks: BackgroundTasks,
    first_name: str = Form(...),
    last_name: str = Form(...),
    email: str = Form(...),
    company_name: str = Form(...),
    code: str = Form(...),
):
    first_name = first_name.strip()
    last_name = last_name.strip()
    email = email.strip().lower()
    company_name = company_name.strip()
    code = code.strip()

    form_ctx = dict(
        first_name=first_name, last_name=last_name, email=email,
        company_name=company_name,
    )

    wait = verify_cooldown_remaining(email)
    if wait > 0:
        return _render_verify(
            request,
            error=f"Troppi tentativi. Riprova tra {wait} secondi.",
            **form_ctx,
        )

    if not verify_otp(email, code):
        return _render_verify(
            request, error="Codice non valido o scaduto.", **form_ctx
        )

    pattern = f"*@{extract_email_domain(email)}"

    # Race guard between POST /signup and POST /signup/verify (5-min OTP window).
    if get_domain_by_pattern(pattern):
        return _render_signup(
            request,
            error="Il dominio è già registrato. Usa il login standard.",
            **form_ctx,
        )

    domain_id = add_domain_trial(
        pattern=pattern,
        company_name=company_name,
        contact_first_name=first_name,
        contact_last_name=last_name,
        contact_email=email,
        duration_days=get_trial_duration_days(),
    )
    domain_row = get_domain(domain_id) or {}
    preset = TIER_PRESETS[TIER_TRIAL]

    welcome_subject, welcome_html = welcome_trial(
        first_name=first_name,
        domain_pattern=pattern,
        expires_at=domain_row.get("expires_at") or "",
        monthly_requests=preset["monthly_request_limit"],
        monthly_tokens=preset["monthly_token_limit"],
    )
    background_tasks.add_task(_send_welcome, email, domain_id, welcome_subject, welcome_html)

    admin_to = get_admin_notification_email()
    if admin_to:
        notif_subject, notif_html = admin_new_signup(domain_row)
        background_tasks.add_task(send_email, admin_to, notif_subject, notif_html)

    return login_and_redirect(email)


def _send_welcome(to: str, domain_id: int, subject: str, html: str) -> None:
    if send_email(to, subject, html):
        try:
            mark_flag(domain_id, "welcome_sent")
        except Exception as e:
            print(f"[signup] mark welcome_sent failed: {e}", flush=True)
