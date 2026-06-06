"""Chat routes: page, SSE streaming, conversation management."""

import json
import re

from fastapi import APIRouter, BackgroundTasks, Request, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from sse_starlette.sse import EventSourceResponse

from app.auth.session import get_session_email
from app.config import settings
from app.version import PRODUCT_NAME
from app.models.user import get_user_by_email, get_or_create_user
from app.models.conversation import (
    create_conversation, list_conversations, get_conversation,
    get_messages, get_messages_for_llm, add_message, update_title,
    delete_conversation, count_user_messages, get_max_messages_setting,
    get_conversation_agent, get_message_by_id, get_prev_user_message,
)
from app.models.usage import check_limit, get_monthly_usage, get_domain_usage, get_current_month
from app.models.domain import get_domain_for_email, get_trial_banner_info
from app.models.share import create_share
from app.auth.email_sender import send_email, get_trial_duration_days
from app.auth.email_templates import share_answer
from app.util.ratelimit import allow
from app.util.validation import EMAIL_RE
from app.search import query as query_module
from app.version import VERSION, BUILD, BUILD_DATE

router = APIRouter()

MAX_LLM_HISTORY = 10  # messages sent to LLM for context
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 10  # max requests per window


def _check_rate_limit(email: str) -> bool:
    """Returns True if request is allowed, False if rate limited."""
    from app.util.ratelimit import allow
    return allow(f"ask:{email}", RATE_LIMIT_MAX, RATE_LIMIT_WINDOW)


def _sse_error(message: str, **extra) -> EventSourceResponse:
    """Single-event SSE error response used by ask() limit gates."""
    async def gen():
        yield {"data": json.dumps({"error": message, "done": True, **extra})}
    return EventSourceResponse(gen())


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
    active_agent_id = None
    if c:
        conv = get_conversation(c, user["id"])
        if conv:
            conversation_id = c
            messages = get_messages(c)
            msg_count = count_user_messages(c)
            active_agent_id = get_conversation_agent(c)
            # Get sources from last assistant message for docs panel
            for m in reversed(messages):
                if m["role"] == "assistant" and m.get("sources"):
                    try:
                        last_sources = json.loads(m["sources"]) if isinstance(m["sources"], str) else m["sources"]
                    except (json.JSONDecodeError, TypeError):
                        pass
                    break

    conversations = list_conversations(user["id"])

    trial_info = get_trial_banner_info(get_domain_for_email(user["email"]))

    from app.search.agents import list_agents_public, get_agent_public

    # Conversazione attiva = ha già messaggi: l'esperto è bloccato (no picker).
    is_active_conversation = bool(messages)

    from app.config import settings as _cfg
    from app.models.settings import get_bool_setting
    _mcp_base = _cfg.base_url.rstrip("/") if _cfg.base_url else str(request.base_url).rstrip("/")

    return _templates().TemplateResponse(request, "chat.html", {
        "request": request,
        "mcp_enabled": get_bool_setting("mcp_enabled", _cfg.production),
        "mcp_url": f"{_mcp_base}/mcp",
        "email": user["email"],
        "is_admin": bool(user["is_admin"]),
        "conversation_id": conversation_id,
        "messages": messages,
        "conversations": conversations,
        "msg_count": msg_count,
        "max_messages": max_messages,
        "last_sources": last_sources,
        "app_version": f"v{VERSION} build {BUILD} ({BUILD_DATE})",
        "show_onboarding": not user.get("onboarding_completed", 0),
        "trial_info": trial_info,
        "agents": list_agents_public(),
        "is_active_conversation": is_active_conversation,
        "active_agent": get_agent_public(active_agent_id),
    })


@router.post("/api/request-upgrade")
async def request_upgrade(request: Request, background_tasks: BackgroundTasks):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)

    domain_cfg = get_domain_for_email(user["email"])
    if not domain_cfg:
        return JSONResponse({"error": "Dominio non riconosciuto."}, status_code=400)

    from app.auth.email_sender import get_admin_notification_email, send_email
    from app.auth.email_templates import admin_upgrade_request

    admin_to = get_admin_notification_email()
    if not admin_to:
        return JSONResponse(
            {"error": "Servizio non configurato. Scrivi a info@scao.it."},
            status_code=503,
        )

    subj, html = admin_upgrade_request(
        user_email=user["email"],
        domain=domain_cfg,
        current_tier=domain_cfg.get("tier", "—"),
    )
    background_tasks.add_task(send_email, admin_to, subj, html)
    return JSONResponse({"ok": True})


# ── Streaming Q&A ──

@router.post("/api/ask")
async def ask(
    request: Request,
    question: str = Form(...),
    conversation_id: str = Form(default=""),
    deep: str = Form(default=""),
    topic: str = Form(default=""),
    agent: str = Form(default=""),
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
    from app.models.domain import get_daily_question_count
    domain_config = get_domain_for_email(user["email"])
    if domain_config and domain_config["daily_limit"] > 0:
        daily_count = get_daily_question_count(user["id"])
        if daily_count >= domain_config["daily_limit"]:
            return _sse_error(
                f"Hai esaurito le domande di oggi ({daily_count}/{domain_config['daily_limit']} "
                f"domande giornaliere). Il contatore si azzera a mezzanotte."
            )

    # Single monthly aggregate (monthly_usage view) — reused for both the
    # request-count gate and the token gate, avoiding a second COUNT query.
    allowed, usage_info = check_limit(user["id"], domain_config)

    # Monthly request limit per domain (commercial tier; 0 = unlimited)
    if domain_config and domain_config["monthly_request_limit"] > 0:
        monthly_count = usage_info["total_questions"]
        if monthly_count >= domain_config["monthly_request_limit"]:
            return _sse_error(
                f"Limite mensile di richieste del piano {domain_config['tier']} raggiunto "
                f"({monthly_count}/{domain_config['monthly_request_limit']}). "
                f"Contatta il rivenditore per l'upgrade."
            )

    if not allowed and usage_info["limit"] > 0:
        used_k = round(usage_info["total_tokens"] / 1000)
        limit_k = round(usage_info["limit"] / 1000)
        return _sse_error(
            f"Hai esaurito i token mensili ({used_k}K/{limit_k}K token consumati questo mese). "
            f"Contatta l'amministratore per aumentare il limite."
        )

    # Check message limit per conversation
    max_msgs = get_max_messages_setting()
    if conversation_id:
        current_count = count_user_messages(conversation_id)
        if current_count >= max_msgs:
            return _sse_error(
                f"Questa conversazione ha raggiunto il limite di {max_msgs} domande. "
                f"Apri una nuova chat per continuare.",
                limit_reached=True,
            )

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

    # Esperto selezionato manualmente dall'UI (vuoto/ignoto = default generico)
    from app.search.agents import get_agent
    selected_agent = get_agent(agent.strip())
    active_agent = selected_agent["id"] if selected_agent else None
    user_role = "amministratore" if user.get("is_admin") else "utente"

    # Disambiguation check (only for first message in new conversations, no topic already selected)
    topic_filter = topic.strip() if topic else None
    if is_new_conv and not topic_filter:
        disambig = await query_module.check_disambiguation(question, is_first_message=True)
        if disambig:
            async def disambig_event():
                yield {"data": json.dumps({
                    "disambiguation": True,
                    "question": disambig["question"],
                    "options": disambig["options"],
                    "conversation_id": conv_id,
                })}
            return EventSourceResponse(disambig_event())

    # Save user message
    add_message(conv_id, "user", question)

    # Get LLM history (excluding the message we just added — it goes with context)
    all_msgs = get_messages_for_llm(conv_id, max_messages=MAX_LLM_HISTORY + 1)
    llm_history = all_msgs[:-1]  # exclude the last user message

    async def event_generator():
        full_response = []
        sources = []
        screenshots = []
        usage_data = None
        corrected_answer = None  # testo con citazioni [Dn] rimappate (canale meta)

        is_deep = deep == "true"
        async for token, token_sources, token_meta in query_module.ask_stream(
            question, history=llm_history, deep=is_deep, topic_filter=topic_filter,
            agent_id=active_agent, user_role=user_role,
        ):
            if token_sources:
                sources = token_sources
            if token_meta and "screenshots" in token_meta:
                screenshots = token_meta["screenshots"]
            if token_meta and "corrected_answer" in token_meta:
                corrected_answer = token_meta["corrected_answer"]
            if token_meta and "prompt_tokens" in token_meta:
                usage_data = token_meta
            if token:
                full_response.append(token)
                yield {"data": json.dumps({"token": token})}

        # Send sources + screenshots (extracted during retrieval, no duplicate call)
        if sources:
            src_data = {"sources": sources}
            if screenshots:
                src_data["screenshots"] = screenshots
            yield {"data": json.dumps(src_data)}

        # Save assistant message. corrected_answer (citazioni [Dn] rimappate dal
        # backend) sostituisce il testo grezzo se presente.
        assistant_text = corrected_answer or "".join(full_response)
        msg_id = None
        if assistant_text:
            msg_id = add_message(
                conv_id, "assistant", assistant_text,
                sources=sources,
                prompt_tokens=usage_data["prompt_tokens"] if usage_data else None,
                completion_tokens=usage_data["completion_tokens"] if usage_data else None,
                cost_usd=usage_data["cost_usd"] if usage_data else None,
                model=usage_data.get("model") if usage_data else None,
                cached_tokens=usage_data.get("cached_tokens") if usage_data else None,
                rerank_tokens=usage_data.get("rerank_tokens") if usage_data else None,
                rerank_cost_usd=usage_data.get("rerank_cost_usd") if usage_data else None,
                rerank_model=usage_data.get("rerank_model") if usage_data else None,
                agent=active_agent,
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
        # Testo con citazioni rimappate: il frontend ri-renderizza con questo per
        # allineare i chip [Dn] al documento corretto anche nella vista live.
        if corrected_answer:
            done_data["corrected_text"] = corrected_answer
        yield {"data": json.dumps(done_data)}

    return EventSourceResponse(event_generator())


# ── Retrieval observability (LOCAL/DEV ONLY) ──

@router.get("/api/debug/retrieve")
async def api_debug_retrieve(
    request: Request,
    q: str = Query(..., min_length=1),
    deep: bool = Query(False),
    topic: str | None = Query(None),
):
    """Inspect every stage of the retrieval pipeline for a query.

    Disabled in production (404). Returns BM25 / semantic / fused /
    post-signal / selected rankings so retrieval misses are debuggable.
    """
    if settings.production:
        return JSONResponse({"error": "not found"}, status_code=404)
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)
    trace = await query_module.trace_retrieve(q, deep=deep, topic_filter=topic)
    return JSONResponse(trace)


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
        role_label = "Tu" if m["role"] == "user" else PRODUCT_NAME
        lines.append(f"## {role_label}\n{m['content']}\n")
    return PlainTextResponse(
        "\n".join(lines),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="chat-{conv_id[:8]}.md"'},
    )


# ── Feedback ──

FEEDBACK_CATEGORIES = {"wrong", "incomplete", "irrelevant", "outdated", "unclear"}

@router.post("/api/feedback/{message_id}")
async def api_feedback(
    request: Request,
    message_id: int,
    rating: int = Form(...),
    category: str = Form(default=""),
    comment: str = Form(default=""),
):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)
    if rating not in (-1, 1):
        return JSONResponse({"error": "Rating non valido."}, status_code=400)

    category = category.strip() if category else None
    if category and category not in FEEDBACK_CATEGORIES:
        category = None
    comment = comment.strip()[:500] if comment else None

    from app.db import get_conn
    conn = get_conn()

    # Verify ownership + get context
    row = conn.execute(
        "SELECT m.id, m.content, m.sources, m.model, m.conversation_id "
        "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        "WHERE m.id = ? AND c.user_id = ?",
        (message_id, user["id"]),
    ).fetchone()
    if not row:
        return JSONResponse({"error": "Messaggio non trovato."}, status_code=404)

    # Get the user question (previous user message)
    user_msg = conn.execute(
        "SELECT content FROM messages "
        "WHERE conversation_id = ? AND role = 'user' AND id < ? "
        "ORDER BY id DESC LIMIT 1",
        (row["conversation_id"], message_id),
    ).fetchone()

    conv_length = conn.execute(
        "SELECT COUNT(*) as cnt FROM messages WHERE conversation_id = ?",
        (row["conversation_id"],),
    ).fetchone()["cnt"]

    query_text = user_msg["content"] if user_msg else None
    response_preview = row["content"][:200] if row["content"] else None

    conn.execute(
        "INSERT OR REPLACE INTO feedback "
        "(message_id, rating, category, comment, query, response_preview, "
        "chunks_used, conversation_length, model, search_scores) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (message_id, rating, category, comment, query_text,
         response_preview, row["sources"], conv_length, row["model"], None),
    )
    conn.commit()
    return JSONResponse({"ok": True})


# ── Onboarding ──

@router.post("/api/onboarding/complete")
async def complete_onboarding(request: Request):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)
    from app.models.user import mark_onboarding_completed
    mark_onboarding_completed(user["id"])
    return JSONResponse({"ok": True})


# ── Usage status bar ──

@router.get("/api/usage/summary")
async def usage_summary(request: Request):
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)

    month = get_current_month()
    u = get_monthly_usage(user["id"], month)
    user_tokens = u["total_prompt_tokens"] + u["total_completion_tokens"]
    _, lim = check_limit(user["id"])

    email = user["email"]
    domain = email.split("@", 1)[1] if "@" in email else ""
    company = {"domain": domain, "questions": 0, "tokens": 0, "token_limit": 0}
    for d in get_domain_usage(month):
        if d["domain"] == domain:
            company["questions"] = d["total_questions"]
            company["tokens"] = d["total_prompt_tokens"] + d["total_completion_tokens"]
            break
    dom_cfg = get_domain_for_email(email)
    if dom_cfg:
        company["token_limit"] = dom_cfg.get("monthly_token_limit") or 0

    return JSONResponse({
        "month": month,
        "user": {
            "questions": u["total_questions"],
            "tokens": user_tokens,
            "token_limit": lim.get("limit") or 0,
        },
        "company": company,
    })


# ── Document viewer ──

@router.get("/api/doc")
async def get_doc(request: Request, file: str = Query(...)):
    """Return a document chunk by source_file for the overlay viewer."""
    if not get_session_email(request):
        return JSONResponse({"error": "Non autenticato."}, status_code=401)

    if query_module._index is None:
        return JSONResponse({"error": "Indice non disponibile."}, status_code=503)

    # Slash-tolerant / basename-fallback lookup (shared with the MCP fetch tool).
    row = query_module._index.get_document(file)
    if not row:
        return JSONResponse({"error": "Documento non trovato."})

    # If preprocessed HTML is available (built during indexing), serve it
    if row["html_content"]:
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


@router.post("/api/mcp/revoke-mine")
async def revoke_my_mcp(request: Request):
    """User self-service: revoke ALL my active MCP tokens (disconnect connectors)."""
    email = get_session_email(request)
    if not email:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)
    from app.models import oauth as oauth_store
    return JSONResponse({"revoked": oauth_store.revoke_by_subject(email)})


# ── Announcements ──

@router.get("/api/announcement")
async def get_announcement(request: Request):
    from app.db import get_conn
    row = get_conn().execute(
        "SELECT value FROM app_settings WHERE key = 'announcement'"
    ).fetchone()
    if row and row["value"]:
        return JSONResponse({"text": row["value"]})
    return Response(status_code=204)


# ── Share a single answer with an external recipient ──

def _excerpt(markdown_text: str, max_chars: int) -> str:
    """Plain-text preview of an answer for the email body (strips markdown/images)."""
    t = re.sub(r'\[Screenshot:[^\]]*\]', '', markdown_text)
    t = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', t)
    t = re.sub(r'[#*_`>\[\]]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    if len(t) <= max_chars:
        return t
    return t[:max_chars].rsplit(' ', 1)[0] + '…'


@router.post("/api/messages/{message_id}/share")
async def share_answer_endpoint(
    request: Request,
    message_id: int,
    recipient_email: str = Form(...),
    note: str = Form(""),
    screenshots: str = Form(""),
):
    """Share one assistant answer via email with an arbitrary external recipient.

    Snapshots the answer into `shares` (screenshots come from the client, since
    they are not persisted server-side) and emails an excerpt + CTA to the public
    landing `/s/{token}`. Auth + ownership + double rate-limit guard against abuse.
    """
    user = _get_user(request)
    if not user:
        return JSONResponse({"error": "Non autenticato."}, status_code=401)

    recipient_email = recipient_email.strip().lower()
    if not EMAIL_RE.match(recipient_email):
        return JSONResponse({"error": "Email destinatario non valida."}, status_code=400)

    if not allow(f"share:from:{user['email']}", 10, 3600):
        return JSONResponse({"error": "Hai condiviso troppe risposte. Riprova tra un'ora."}, status_code=429)
    if not allow(f"share:to:{recipient_email}", 3, 86400):
        return JSONResponse({"error": "Troppe condivisioni verso questo destinatario. Riprova domani."}, status_code=429)

    msg = get_message_by_id(message_id, user["id"])
    if not msg or msg["role"] != "assistant":
        return JSONResponse({"error": "Risposta non trovata."}, status_code=404)

    # Screenshots are sent by the client (not stored in `messages`)
    try:
        shots = json.loads(screenshots) if screenshots else []
        if not isinstance(shots, list):
            shots = []
    except (ValueError, TypeError):
        shots = []
    sources = json.loads(msg["sources"]) if msg.get("sources") else None

    token = create_share(
        message_id=message_id,
        conversation_id=msg["conversation_id"],
        sender_user_id=user["id"],
        sender_email=user["email"],
        recipient_email=recipient_email,
        snap_content_md=msg["content"],
        snap_sources=sources,
        snap_screenshots=shots,
        snap_agent=msg.get("agent"),
        snap_question=get_prev_user_message(msg["conversation_id"], message_id),
    )

    base_url = settings.base_url.rstrip("/") if settings.base_url else "https://os1docs.ai.scao.it"
    share_url = f"{base_url}/s/{token}"
    subject, html = share_answer(
        sender_email=user["email"],
        excerpt=_excerpt(msg["content"], 320),
        note=note.strip()[:500],
        share_url=share_url,
        pixel_url=f"{share_url}/pixel.gif",
        trial_days=get_trial_duration_days(),
    )

    if not send_email(recipient_email, subject, html):
        return JSONResponse({"error": "Errore nell'invio dell'email."}, status_code=500)

    return JSONResponse({"ok": True, "url": share_url, "dev": not settings.resend_api_key})
