"""Chat routes: page, SSE streaming, conversation management."""

import json
import time
from collections import defaultdict

from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse

from app.auth.session import get_session_email
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

    # Daily limit per domain
    from app.models.domain import get_domain_for_email, get_daily_question_count
    domain_config = get_domain_for_email(user["email"])
    if domain_config:
        daily_count = get_daily_question_count(user["id"])
        if daily_count >= domain_config["daily_limit"]:
            async def daily_limit_event():
                yield {"data": json.dumps({
                    "error": f"Hai raggiunto il limite giornaliero di {domain_config['daily_limit']} domande. Riprova domani.",
                    "done": True,
                })}
            return EventSourceResponse(daily_limit_event())

    # Check monthly limit
    allowed, usage_info = check_limit(user["id"])
    if not allowed:
        async def limit_event():
            yield {"data": json.dumps({
                "error": "Hai raggiunto il limite mensile di utilizzo. Contatta l'amministratore.",
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
                    "error": f"Hai raggiunto il limite di {max_msgs} domande per questa conversazione. Apri una nuova chat.",
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
