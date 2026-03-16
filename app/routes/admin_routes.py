"""Admin backoffice routes."""

import csv
import io

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse, JSONResponse

from app.auth.session import get_session_email
from app.models.user import get_user_by_email, is_admin, list_users, set_user_limit
from app.models.conversation import list_conversations, get_conversation_any, get_messages
from app.models.usage import (
    get_all_usage, get_usage_summary, get_monthly_usage,
    get_domain_usage, get_recent_questions, get_current_month,
)
from app.models.domain import list_domains, add_domain, update_domain, delete_domain

router = APIRouter(prefix="/admin")


def _templates():
    from app.main import templates
    return templates


def _require_admin(request: Request) -> dict | None:
    """Return user dict if admin, else None."""
    email = get_session_email(request)
    if not email:
        return None
    user = get_user_by_email(email)
    if not user or not user["is_admin"]:
        return None
    return user


# ── Dashboard ──

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    month = get_current_month()
    summary = get_usage_summary(month)
    users = list_users()
    recent = get_recent_questions(30)

    return _templates().TemplateResponse("admin/dashboard.html", {
        "request": request,
        "email": admin["email"],
        "is_admin": True,
        "month": month,
        "summary": summary,
        "total_users": len(users),
        "recent_questions": recent,
    })


# ── Users ──

@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    month = get_current_month()
    users = list_users()
    usage_list = get_all_usage(month)
    usage_map = {u["email"]: u for u in usage_list}

    # Merge usage into users
    for user in users:
        u = usage_map.get(user["email"], {})
        user["questions"] = u.get("total_questions", 0)
        user["tokens"] = u.get("total_prompt_tokens", 0) + u.get("total_completion_tokens", 0)
        user["cost"] = u.get("total_cost_usd", 0.0)

    return _templates().TemplateResponse("admin/users.html", {
        "request": request,
        "email": admin["email"],
        "is_admin": True,
        "month": month,
        "users": users,
    })


@router.get("/users/{user_email}", response_class=HTMLResponse)
async def user_detail(request: Request, user_email: str):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    user = get_user_by_email(user_email)
    if not user:
        return RedirectResponse(url="/admin/users", status_code=302)

    month = get_current_month()
    usage = get_monthly_usage(user["id"], month)
    conversations = list_conversations(user["id"])

    from app.config import settings
    limit = user["monthly_token_limit"] if user["monthly_token_limit"] is not None else settings.default_monthly_token_limit

    return _templates().TemplateResponse("admin/user_detail.html", {
        "request": request,
        "email": admin["email"],
        "is_admin": True,
        "user": user,
        "usage": usage,
        "limit": limit,
        "conversations": conversations,
        "month": month,
    })


@router.post("/users/{user_email}/limit")
async def set_limit(request: Request, user_email: str, limit: str = Form(...)):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    limit_val = int(limit) if limit.strip() else None
    set_user_limit(user_email, limit_val)
    return RedirectResponse(url=f"/admin/users/{user_email}", status_code=302)


# ── Usage ──

@router.get("/usage", response_class=HTMLResponse)
async def usage_page(request: Request, month: str | None = None):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    month = month or get_current_month()
    usage_list = get_all_usage(month)
    summary = get_usage_summary(month)
    domain_list = get_domain_usage(month)

    return _templates().TemplateResponse("admin/usage.html", {
        "request": request,
        "email": admin["email"],
        "is_admin": True,
        "month": month,
        "usage_list": usage_list,
        "summary": summary,
        "domain_list": domain_list,
    })


@router.get("/export/usage")
async def export_usage(request: Request, month: str | None = None):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    month = month or get_current_month()
    usage_list = get_all_usage(month)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Email", "Domande", "Token Input", "Token Output", "Costo USD"])
    for u in usage_list:
        writer.writerow([
            u["email"], u["total_questions"],
            u["total_prompt_tokens"], u["total_completion_tokens"],
            f"{u['total_cost_usd']:.4f}",
        ])
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="usage-{month}.csv"'},
    )


# ── Conversations ──

@router.get("/conversations/{conv_id}", response_class=HTMLResponse)
async def view_conversation(request: Request, conv_id: str):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    conv = get_conversation_any(conv_id)
    if not conv:
        return RedirectResponse(url="/admin", status_code=302)

    messages = get_messages(conv_id)

    return _templates().TemplateResponse("admin/conversation.html", {
        "request": request,
        "email": admin["email"],
        "is_admin": True,
        "conv": conv,
        "messages": messages,
    })


# ── Domains ──

@router.get("/domains", response_class=HTMLResponse)
async def domains_page(request: Request):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    domains = list_domains()
    return _templates().TemplateResponse("admin/domains.html", {
        "request": request,
        "email": admin["email"],
        "is_admin": True,
        "domains": domains,
    })


@router.post("/domains/add")
async def add_domain_route(
    request: Request,
    pattern: str = Form(...),
    daily_limit: int = Form(default=50),
    monthly_token_limit: int = Form(default=500000),
):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    pattern = pattern.strip().lower()
    if pattern:
        try:
            add_domain(pattern, daily_limit, monthly_token_limit)
        except Exception:
            pass  # duplicate pattern, ignore
    return RedirectResponse(url="/admin/domains", status_code=302)


@router.post("/domains/{domain_id}/update")
async def update_domain_route(
    request: Request,
    domain_id: int,
    daily_limit: int = Form(...),
    monthly_token_limit: int = Form(...),
    enabled: str = Form(default=""),
):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    update_domain(domain_id, daily_limit, monthly_token_limit, enabled == "on")
    return RedirectResponse(url="/admin/domains", status_code=302)


@router.post("/domains/{domain_id}/delete")
async def delete_domain_route(request: Request, domain_id: int):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    delete_domain(domain_id)
    return RedirectResponse(url="/admin/domains", status_code=302)


# ── Settings ──

def _get_setting(key: str, default: str = "") -> str:
    from app.db import get_conn
    row = get_conn().execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _get_all_settings() -> dict:
    """Get all admin-configurable settings with defaults."""
    from app.models.conversation import get_max_messages_setting
    from app.config import settings as app_settings
    return {
        "groq_model": _get_setting("groq_model", "llama-3.1-8b-instant"),
        "otp_sender_name": _get_setting("otp_sender_name", "OS1 Docs"),
        "otp_sender_email": _get_setting("otp_sender_email", "noreply@ai.scao.it"),
        "allowed_emails": _get_setting("allowed_emails", app_settings.allowed_emails),
        "max_messages": get_max_messages_setting(),
        "announcement": _get_setting("announcement", ""),
    }


@router.get("/announcement", response_class=HTMLResponse)
async def settings_page(request: Request):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    return _templates().TemplateResponse("admin/announcement.html", {
        "request": request,
        "email": admin["email"],
        "is_admin": True,
        "settings": _get_all_settings(),
    })


@router.post("/settings")
async def save_settings(request: Request):
    admin = _require_admin(request)
    if not admin:
        return RedirectResponse(url="/login", status_code=302)

    form = await request.form()
    from app.db import get_conn
    conn = get_conn()

    settings_map = {
        "groq_model": str(form.get("groq_model", "")).strip(),
        "otp_sender_name": str(form.get("otp_sender_name", "")).strip(),
        "otp_sender_email": str(form.get("otp_sender_email", "")).strip(),
        "allowed_emails": str(form.get("allowed_emails", "")).strip(),
        "max_messages_per_conversation": str(max(1, min(int(form.get("max_messages_per_conversation", 20)), 200))),
        "announcement": str(form.get("announcement", "")).strip(),
    }

    for key, value in settings_map.items():
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    conn.commit()
    return RedirectResponse(url="/admin/announcement", status_code=302)
