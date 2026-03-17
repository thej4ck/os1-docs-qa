"""Chat routes: page, SSE streaming, conversation management."""

import json
import re
import time
from collections import defaultdict
from datetime import datetime

import markdown as md
import resend

from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse

from app.auth.session import get_session_email
from app.config import settings
from app.models.user import get_user_by_email, get_or_create_user
from app.models.conversation import (
    create_conversation, list_conversations, get_conversation,
    get_messages, get_messages_for_llm, add_message, update_title,
    delete_conversation, count_user_messages, get_max_messages_setting,
)
from app.models.usage import check_limit
from app.search import query as query_module
from app.version import VERSION, BUILD, BUILD_DATE

router = APIRouter()

MAX_LLM_HISTORY = 10  # messages sent to LLM for context
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 10  # max requests per window
_rate_limits: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(email: str) -> bool:
    """Returns True if request is allowed, False if rate limited."""
    now = time.time()
    timestamps = _rate_limits[email]
    # Purge old entries
    _rate_limits[email] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_limits[email]) >= RATE_LIMIT_MAX:
        return False
    _rate_limits[email].append(now)
    return True


def _templates():
    from app.main import templates
    return templates


def _get_user(request: Request) -> dict | None:
    """Get authenticated user dict or None. Auto-creates DB record if missing."""
    email = get_session_email(request)
    if not email:
        return None
    user = get_user_by_email(email)
    if not user:
        user = get_or_create_user(email)
    return user


# ── Pages ──

@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if get_session_email(request):
        return RedirectResponse(url="/chat", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request, c: str | None = None):
    user = _get_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    messages = []
    conversation_id = None
    msg_count = 0
    max_messages = get_max_messages_setting()
    last_sources = []
    if c:
        conv = get_conversation(c, user["id"])
        if conv:
            conversation_id = c
            messages = get_messages(c)
            msg_count = count_user_messages(c)
            # Get sources from last assistant message for docs panel
            for m in reversed(messages):
                if m["role"] == "assistant" and m.get("sources"):
                    try:
                        last_sources = json.loads(m["sources"]) if isinstance(m["sources"], str) else m["sources"]
                    except (json.JSONDecodeError, TypeError):
                        pass
                    break

    conversations = list_conversations(user["id"])

    return _templates().TemplateResponse("chat.html", {
        "request": request,
        "email": user["email"],
        "is_admin": bool(user["is_admin"]),
        "conversation_id": conversation_id,
        "messages": messages,
        "conversations": conversations,
        "msg_count": msg_count,
        "max_messages": max_messages,
        "last_sources": last_sources,
        "app_version": f"v{VERSION} build {BUILD} ({BUILD_DATE})",
    })


# ── Streaming Q&A ──

@router.post("/api/ask")
async def ask(
    request: Request,
    question: str = Form(...),
    conversation_id: str = Form(default=""),
    deep: str = Form(default=""),
):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)

    question = question.strip()
    if not question:
        return JSONResponse({"error": "Domanda vuota."}, status_code=400)

    # Rate limiting (burst)
    if not _check_rate_limit(user["email"]):
        return JSONResponse(
            {"error": "Troppe richieste. Attendi un minuto."},
            status_code=429,
        )

    # Daily limit per domain (0 = unlimited)
    from app.models.domain import get_domain_for_email, get_daily_question_count
    domain_config = get_domain_for_email(user["email"])
    if domain_config and domain_config["daily_limit"] > 0:
        daily_count = get_daily_question_count(user["id"])
        if daily_count >= domain_config["daily_limit"]:
            async def daily_limit_event():
                yield {"data": json.dumps({
                    "error": f"Hai esaurito le domande di oggi ({daily_count}/{domain_config['daily_limit']} domande giornaliere). Il contatore si azzera a mezzanotte.",
                    "done": True,
                })}
            return EventSourceResponse(daily_limit_event())

    # Monthly token limit — domain setting overrides user limit
    # If domain has monthly_token_limit = 0, skip user-level check
    domain_monthly_unlimited = domain_config and domain_config.get("monthly_token_limit", 0) == 0
    if not domain_monthly_unlimited:
        allowed, usage_info = check_limit(user["id"])
        if not allowed and usage_info["limit"] > 0:
            used_k = round(usage_info["total_tokens"] / 1000)
            limit_k = round(usage_info["limit"] / 1000)
            async def limit_event():
                yield {"data": json.dumps({
                    "error": f"Hai esaurito i token mensili ({used_k}K/{limit_k}K token consumati questo mese). Contatta l'amministratore per aumentare il limite.",
                    "done": True,
                })}
            return EventSourceResponse(limit_event())

    # Check message limit per conversation
    max_msgs = get_max_messages_setting()
    if conversation_id:
        current_count = count_user_messages(conversation_id)
        if current_count >= max_msgs:
            async def msg_limit_event():
                yield {"data": json.dumps({
                    "error": f"Questa conversazione ha raggiunto il limite di {max_msgs} domande. Apri una nuova chat per continuare.",
                    "done": True,
                    "limit_reached": True,
                })}
            return EventSourceResponse(msg_limit_event())

    # Get or create conversation
    conv_id = conversation_id or None
    is_new_conv = False
    if not conv_id:
        title = question[:60].rsplit(" ", 1)[0] if len(question) > 60 else question
        conv_id = create_conversation(user["id"], title)
        is_new_conv = True
    else:
        conv = get_conversation(conv_id, user["id"])
        if not conv:
            return JSONResponse({"error": "Conversazione non trovata."}, status_code=404)

    # Save user message
    add_message(conv_id, "user", question)

    # Get LLM history (excluding the message we just added — it goes with context)
    all_msgs = get_messages_for_llm(conv_id, max_messages=MAX_LLM_HISTORY + 1)
    llm_history = all_msgs[:-1]  # exclude the last user message

    async def event_generator():
        full_response = []
        sources = []
        usage_data = None

        is_deep = deep == "true"
        async for token, token_sources, token_usage in query_module.ask_stream(
            question, history=llm_history, deep=is_deep
        ):
            if token_sources:
                sources = token_sources
            if token_usage:
                usage_data = token_usage
            if token:
                full_response.append(token)
                yield {"data": json.dumps({"token": token})}

        # Send sources + screenshots
        if sources:
            # Extract screenshot URLs from retrieved documents for frontend rendering
            import re as _re
            screenshots = []
            for doc in query_module.retrieve_with_budget(question, deep=is_deep)[:5]:
                for m in _re.finditer(r'\[Screenshot:\s*(.+?)\s*\|\s*(.+?)\s*\]', doc["content"]):
                    screenshots.append({"desc": m.group(1), "url": m.group(2)})
                    if len(screenshots) >= 3:
                        break
                if len(screenshots) >= 3:
                    break
            src_data = {"sources": sources}
            if screenshots:
                src_data["screenshots"] = screenshots
            yield {"data": json.dumps(src_data)}

        # Save assistant message with usage
        assistant_text = "".join(full_response)
        msg_id = None
        if assistant_text:
            msg_id = add_message(
                conv_id, "assistant", assistant_text,
                sources=sources,
                prompt_tokens=usage_data["prompt_tokens"] if usage_data else None,
                completion_tokens=usage_data["completion_tokens"] if usage_data else None,
                cost_usd=usage_data["cost_usd"] if usage_data else None,
            )

        # Signal completion with metadata
        new_count = count_user_messages(conv_id)
        done_data = {
            "done": True,
            "conversation_id": conv_id,
            "msg_count": new_count,
            "max_messages": max_msgs,
        }
        if msg_id:
            done_data["message_id"] = msg_id
        if usage_data:
            done_data["usage"] = usage_data
        yield {"data": json.dumps(done_data)}

    return EventSourceResponse(event_generator())


# ── Conversation API ──

@router.get("/api/conversations")
async def api_list_conversations(request: Request):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)
    convs = list_conversations(user["id"])
    return JSONResponse(convs)


@router.post("/api/conversations")
async def api_create_conversation(request: Request, title: str = Form(default="")):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)
    conv_id = create_conversation(user["id"], title)
    return JSONResponse({"id": conv_id, "title": title})


@router.delete("/api/conversations/{conv_id}")
async def api_delete_conversation(request: Request, conv_id: str):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)
    deleted = delete_conversation(conv_id, user["id"])
    if not deleted:
        return JSONResponse({"error": "Non trovata."}, status_code=404)
    return JSONResponse({"ok": True})


@router.get("/api/conversations/{conv_id}/export")
async def api_export_conversation(request: Request, conv_id: str):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)
    conv = get_conversation(conv_id, user["id"])
    if not conv:
        return JSONResponse({"error": "Non trovata."}, status_code=404)
    messages = get_messages(conv_id)
    lines = [f"# {conv['title'] or 'Conversazione'}\n"]
    for m in messages:
        role_label = "Tu" if m["role"] == "user" else "OS1 Docs"
        lines.append(f"## {role_label}\n{m['content']}\n")
    return PlainTextResponse(
        "\n".join(lines),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="chat-{conv_id[:8]}.md"'},
    )


# ── Feedback ──

@router.post("/api/feedback/{message_id}")
async def api_feedback(request: Request, message_id: int, rating: int = Form(...)):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)
    if rating not in (-1, 1):
        return JSONResponse({"error": "Rating non valido."}, status_code=400)

    from app.db import get_conn
    conn = get_conn()
    # Verify ownership through conversation
    row = conn.execute(
        "SELECT m.id FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE m.id = ? AND c.user_id = ?",
        (message_id, user["id"]),
    ).fetchone()
    if not row:
        return JSONResponse({"error": "Messaggio non trovato."}, status_code=404)

    conn.execute(
        "INSERT OR REPLACE INTO feedback (message_id, rating) VALUES (?, ?)",
        (message_id, rating),
    )
    conn.commit()
    return JSONResponse({"ok": True})


# ── Document viewer ──

@router.get("/api/doc")
async def get_doc(request: Request, file: str = Query(...)):
    """Return a document chunk by source_file for the overlay viewer."""
    if not get_session_email(request):
        return JSONResponse({"error": "Non autenticato."}, status_code=401)

    if query_module._index is None:
        return JSONResponse({"error": "Indice non disponibile."}, status_code=503)

    row = query_module._index.conn.execute(
        "SELECT title, content, source_file, doc_type, html_content FROM documents WHERE source_file = ? LIMIT 1",
        (file,),
    ).fetchone()

    if not row:
        return JSONResponse({"error": "Documento non trovato."})

    # If preprocessed HTML is available (built during indexing), serve it
    if row["html_content"]:
        from fastapi.responses import Response
        payload = json.dumps({
            "title": row["title"] or file,
            "html": row["html_content"],
            "source_file": row["source_file"],
            "is_html": True,
        }, ensure_ascii=False)
        return Response(content=payload, media_type="application/json")

    # Fallback: plain text rendered as markdown by frontend
    return JSONResponse({
        "title": row["title"] or file,
        "content": row["content"],
        "source_file": row["source_file"],
    })


# ── Announcements ──

@router.get("/api/announcement")
async def get_announcement(request: Request):
    from app.db import get_conn
    row = get_conn().execute(
        "SELECT value FROM app_settings WHERE key = 'announcement'"
    ).fetchone()
    if row and row["value"]:
        return JSONResponse({"text": row["value"]})
    return JSONResponse(None, status_code=204)


# ── Email chat ──

EMAIL_RATE_LIMIT_MAX = 3
_email_rate_limits: dict[str, list[float]] = defaultdict(list)


def _check_email_rate_limit(email: str) -> bool:
    now = time.time()
    timestamps = _email_rate_limits[email]
    _email_rate_limits[email] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(_email_rate_limits[email]) >= EMAIL_RATE_LIMIT_MAX:
        return False
    _email_rate_limits[email].append(now)
    return True


def _get_sender() -> str:
    """Get email sender from app_settings or default."""
    name = "OS1 AI Docs"
    email_addr = "noreply@ai.scao.it"
    try:
        from app.db import get_conn
        conn = get_conn()
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'otp_sender_name'").fetchone()
        if row and row["value"]:
            name = row["value"]
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'otp_sender_email'").fetchone()
        if row and row["value"]:
            email_addr = row["value"]
    except Exception:
        pass
    return f"{name} <{email_addr}>"


def _md_to_html(text: str, base_url: str) -> str:
    """Convert markdown content to email-safe HTML with absolute image URLs."""
    # Convert [Screenshot: desc | url] markers to markdown images
    text = re.sub(
        r'\[Screenshot:\s*(.+?)\s*\|\s*(.+?)\s*\]',
        r'![\1](\2)',
        text,
    )
    # Make relative image URLs absolute
    text = re.sub(
        r'!\[([^\]]*)\]\((/[^)]+)\)',
        lambda m: f'![{m.group(1)}]({base_url}{m.group(2)})',
        text,
    )
    html = md.markdown(text, extensions=["tables", "fenced_code", "nl2br"])
    # Style images inline for email
    html = html.replace(
        "<img ",
        '<img style="max-width:100%;height:auto;border-radius:8px;border:1px solid #E5E7EB;margin:8px 0;display:block;" ',
    )
    # Style tables inline
    html = html.replace("<table>", '<table style="border-collapse:collapse;width:100%;margin:8px 0;font-size:14px;">')
    html = html.replace("<th>", '<th style="border:1px solid #E5E7EB;padding:6px 10px;background:#F0F2F5;font-weight:600;text-align:left;">')
    html = html.replace("<td>", '<td style="border:1px solid #E5E7EB;padding:6px 10px;text-align:left;">')
    # Style code blocks
    html = html.replace("<pre>", '<pre style="background:#1E293B;color:#E2E8F0;padding:12px 16px;border-radius:6px;overflow-x:auto;margin:8px 0;font-size:13px;">')
    html = html.replace("<code>", '<code style="font-family:monospace;font-size:0.9em;">')
    return html


def _build_email_html(conv: dict, messages: list[dict], user_email: str) -> str:
    """Build a professional HTML email for the conversation."""
    base_url = settings.base_url.rstrip("/") if settings.base_url else "https://os1docs.ai.scao.it"
    logo_url = f"{base_url}/static/img/logo.png"
    title = conv.get("title") or "Conversazione"
    conv_date = conv.get("created_at", "")[:10] if conv.get("created_at") else ""

    # Build message blocks
    msg_blocks = []
    for m in messages:
        if m["role"] == "user":
            msg_blocks.append(f'''
            <div style="margin-bottom:16px;">
                <div style="font-size:12px;font-weight:600;color:#6B7280;margin-bottom:4px;">Tu</div>
                <div style="background:#E2231A;color:#ffffff;padding:12px 16px;border-radius:10px;border-bottom-right-radius:4px;font-size:15px;line-height:1.6;">
                    {_escape(m["content"])}
                </div>
            </div>''')
        else:
            html_content = _md_to_html(m["content"], base_url)
            msg_blocks.append(f'''
            <div style="margin-bottom:16px;">
                <div style="font-size:12px;font-weight:600;color:#6B7280;margin-bottom:4px;">OS1 AI Docs</div>
                <div style="background:#FFFFFF;border:1px solid #E5E7EB;padding:12px 16px;border-radius:10px;border-bottom-left-radius:4px;font-size:15px;line-height:1.65;color:#2D2D2D;">
                    {html_content}
                </div>
            </div>''')

    messages_html = "\n".join(msg_blocks)
    now = datetime.now().strftime("%d/%m/%Y alle %H:%M")

    return f'''<!DOCTYPE html>
<html lang="it">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F7F8FA;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif;">
<div style="max-width:640px;margin:0 auto;padding:24px 16px;">

    <!-- Header -->
    <div style="text-align:center;padding:24px 0 16px;">
        <img src="{logo_url}" alt="SCAO" style="height:24px;width:auto;margin:0 auto 12px;display:block;">
        <div style="font-size:20px;font-weight:700;color:#2D2D2D;letter-spacing:-0.02em;">OS1 AI Docs</div>
        <div style="font-size:13px;color:#6B7280;margin-top:2px;">SCAO Informatica &mdash; Assistente documentazione OS1</div>
    </div>
    <div style="height:3px;background:#E2231A;border-radius:2px;margin-bottom:20px;"></div>

    <!-- Conversation title -->
    <div style="margin-bottom:16px;">
        <div style="font-size:16px;font-weight:600;color:#2D2D2D;">{_escape(title)}</div>
        <div style="font-size:12px;color:#9CA3AF;margin-top:2px;">{conv_date}</div>
    </div>

    <!-- AI Disclaimer -->
    <div style="background:#F0F2F5;border:1px solid #E5E7EB;border-radius:8px;padding:12px 16px;margin-bottom:20px;font-size:12px;line-height:1.5;color:#6B7280;">
        Questo contenuto è stato generato da <strong style="color:#2D2D2D;">OS1 AI Docs</strong>, un assistente basato su intelligenza artificiale per la documentazione del gestionale OS1. Le informazioni fornite sono indicative e potrebbero non essere completamente accurate. Si consiglia di verificare sempre i dati con la documentazione ufficiale.
    </div>

    <!-- Messages -->
    {messages_html}

    <!-- Footer -->
    <div style="border-top:1px solid #E5E7EB;margin-top:24px;padding-top:16px;text-align:center;">
        <div style="font-size:13px;font-weight:600;color:#2D2D2D;">OS1 AI Docs &mdash; SCAO Informatica</div>
        <div style="font-size:12px;color:#9CA3AF;margin-top:4px;">Inviato il {now}</div>
        <div style="font-size:12px;color:#9CA3AF;margin-top:2px;">
            <a href="{base_url}/login" style="color:#E2231A;text-decoration:none;">Accedi a OS1 AI Docs</a>
        </div>
        <div style="font-size:11px;color:#9CA3AF;margin-top:8px;">
            Questa email è stata inviata su richiesta di {_escape(user_email)}
        </div>
    </div>

</div>
</body>
</html>'''


def _escape(text: str) -> str:
    """HTML-escape text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@router.post("/api/conversations/{conv_id}/email")
async def email_conversation(request: Request, conv_id: str):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)

    if not _check_email_rate_limit(user["email"]):
        return JSONResponse({"error": "Troppe richieste. Attendi un minuto."}, status_code=429)

    conv = get_conversation(conv_id, user["id"])
    if not conv:
        return JSONResponse({"error": "Conversazione non trovata."}, status_code=404)

    messages = get_messages(conv_id)
    if not messages:
        return JSONResponse({"error": "Nessun messaggio da inviare."}, status_code=400)

    html = _build_email_html(conv, messages, user["email"])
    title = conv.get("title") or "Conversazione"

    if not settings.resend_api_key:
        print(f"[DEV MODE] Email chat per {user['email']}: {title}", flush=True)
        return JSONResponse({"ok": True, "dev": True})

    resend.api_key = settings.resend_api_key
    try:
        resend.Emails.send({
            "from": _get_sender(),
            "to": [user["email"]],
            "subject": f"Chat OS1 Docs: {title}",
            "html": html,
        })
    except Exception as e:
        print(f"Failed to send chat email: {e}", flush=True)
        return JSONResponse({"error": "Errore nell'invio dell'email."}, status_code=500)

    return JSONResponse({"ok": True})
