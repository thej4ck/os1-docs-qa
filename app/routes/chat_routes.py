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
    if c:
        conv = get_conversation(c, user["id"])
        if conv:
            conversation_id = c
            messages = get_messages(c)
            msg_count = count_user_messages(c)

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
    })


# ── Streaming Q&A ──

@router.post("/api/ask")
async def ask(
    request: Request,
    question: str = Form(...),
    conversation_id: str = Form(default=""),
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

        async for token, token_sources, token_usage in query_module.ask_stream(
            question, history=llm_history
        ):
            if token_sources:
                sources = token_sources
            if token_usage:
                usage_data = token_usage
            if token:
                full_response.append(token)
                yield {"data": json.dumps({"token": token})}

        # Send sources
        if sources:
            yield {"data": json.dumps({"sources": sources})}

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

HELP_CSS = """<style>
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@500;600;700&family=Source+Sans+3:wght@400;500;600&display=swap');

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: 'Source Sans 3', -apple-system, sans-serif;
    font-size: 14px; line-height: 1.75; color: #2D2D2D;
    background: #FAFBFC; padding: 24px;
}

/* Document canvas */
.doc-canvas {
    background: #FFFFFF;
    border: 1px solid #E5E7EB;
    border-radius: 10px;
    padding: 32px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.03);
    max-width: 680px;
    margin: 0 auto;
}

/* Headings */
.doc-title {
    font-family: 'DM Sans', sans-serif; font-size: 1.3em; font-weight: 700;
    color: #1E293B; padding-bottom: 0.5em; margin: 0 0 0.8em;
    border-bottom: 2px solid #E2231A;
}
.doc-subtitle {
    font-family: 'DM Sans', sans-serif; font-size: 1.05em; font-weight: 600;
    color: #1E293B; margin: 1.5em 0 0.6em;
    padding: 8px 0 6px; border-bottom: 1px solid #E8EAED;
}

/* Field definitions */
.field-def {
    padding: 12px 16px; margin: 8px 0;
    background: linear-gradient(135deg, #FAFBFC 0%, #F5F6F8 100%);
    border-left: 3px solid #E2231A;
    border-radius: 0 8px 8px 0;
    font-size: 13.5px; line-height: 1.65;
    border: 1px solid #EDEEF0; border-left: 3px solid #E2231A;
}
.field-name {
    font-weight: 700; color: #1E293B; font-size: 0.88em;
    letter-spacing: 0.02em; display: inline;
}
.field-sep { color: #CBD5E1; margin: 0 6px; font-weight: 300; }

/* Body text */
p { margin: 0.6em 0; line-height: 1.75; }

/* Images */
.doc-screenshot { margin: 1.2em 0; text-align: center; }
.doc-screenshot img {
    display: inline-block; border: 1px solid #E5E7EB;
    border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,0.06);
}
.doc-icon { display: inline; vertical-align: middle; margin: 0 4px; }

/* Tables */
table {
    border-collapse: collapse; width: 100%; margin: 1em 0;
    border-radius: 8px; overflow: hidden;
    border: 1px solid #E5E7EB;
}
th, td { border: 1px solid #E5E7EB; padding: 10px 14px; text-align: left; font-size: 13px; }
th { background: #F3F4F6; font-weight: 600; font-size: 12px; text-transform: uppercase; letter-spacing: 0.03em; color: #6B7280; }
tr:nth-child(even) td { background: #FAFBFC; }

/* Lists */
ul, ol { padding-left: 1.6em; margin: 0.6em 0; }
li { margin-bottom: 0.35em; line-height: 1.65; }
li::marker { color: #E2231A; }

/* Misc */
strong, b { font-weight: 600; color: #1E293B; }

/* ── Dark mode ── */
.doc-dark body, .doc-dark { color: #E4E6EB; background: #0F1117; }
.doc-dark .doc-canvas { background: #1A1D27; border-color: #2E3140; box-shadow: 0 1px 4px rgba(0,0,0,0.2); }
.doc-dark .doc-title { color: #E4E6EB; border-bottom-color: #EF4444; }
.doc-dark .doc-subtitle { color: #E4E6EB; border-bottom-color: #2E3140; }
.doc-dark .field-def { background: linear-gradient(135deg, #1A1D27 0%, #22252F 100%); border-color: #2E3140; border-left-color: #EF4444; }
.doc-dark .field-name { color: #E4E6EB; }
.doc-dark p, .doc-dark li { color: #C8CCD4; }
.doc-dark table { border-color: #2E3140; }
.doc-dark th, .doc-dark td { border-color: #2E3140; color: #C8CCD4; }
.doc-dark th { background: #22252F; color: #9CA3B4; }
.doc-dark tr:nth-child(even) td { background: #1A1D27; }
.doc-dark .doc-screenshot img { border-color: #2E3140; box-shadow: 0 4px 16px rgba(0,0,0,0.3); }
.doc-dark strong, .doc-dark b { color: #E4E6EB; }
.doc-dark li::marker { color: #EF4444; }
</style>"""


@router.get("/api/doc")
async def get_doc(request: Request, file: str = Query(...)):
    """Return a document chunk by source_file for the overlay viewer."""
    if not get_session_email(request):
        return JSONResponse({"error": "Non autenticato."}, status_code=401)

    if query_module._index is None:
        return JSONResponse({"error": "Indice non disponibile."}, status_code=503)

    row = query_module._index.conn.execute(
        "SELECT title, content, source_file, doc_type FROM documents WHERE source_file = ? LIMIT 1",
        (file,),
    ).fetchone()

    if not row:
        return JSONResponse({"error": "Documento non trovato."})

    # For HTML help files, serve original HTML with images
    sf = row["source_file"].replace("\\", "/")
    if row["doc_type"] == "help" and sf.endswith((".htm", ".html")):
        html = _load_help_html(sf, row["title"])
        if html:
            from fastapi.responses import Response
            payload = json.dumps({
                "title": row["title"] or file,
                "html": html,
                "source_file": row["source_file"],
                "is_html": True,
            }, ensure_ascii=False)
            return Response(content=payload, media_type="application/json")

    return JSONResponse({
        "title": row["title"] or file,
        "content": row["content"],
        "source_file": row["source_file"],
    })


def _load_help_html(source_file: str, title: str) -> str | None:
    """Load HTML help file, preprocess with BeautifulSoup into clean semantic HTML."""
    import re
    from pathlib import Path, PurePosixPath
    from bs4 import BeautifulSoup, NavigableString
    from app.config import settings

    repo = Path(settings.docs_repo_path).resolve()
    file_path = repo / source_file

    try:
        file_path.resolve().relative_to(repo)
    except ValueError:
        return None
    if not file_path.is_file():
        return None
    try:
        raw = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None

    # Image base URL
    parent = str(PurePosixPath(source_file).parent)
    prefix = "sources/help/"
    help_base = "/help-files/" + parent[len(prefix):] if parent.startswith(prefix) else "/help-files"

    soup = BeautifulSoup(raw, "html.parser")

    # ── 1. Strip unwanted elements ──
    for tag in soup.find_all(["script", "style", "link", "meta"]):
        tag.decompose()

    # Remove inline event handlers
    for tag in soup.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag[attr]

    # ── 2. Rewrite image paths ──
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src and not src.startswith(("http", "/")):
            img["src"] = f"{help_base}/{src}"

    # ── 3. Strip dead links (keep text content) ──
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if href and not href.startswith("http"):
            a.replace_with(a.get_text())

    # ── 4. Remove empty paragraphs and &nbsp; spacers ──
    for p in soup.find_all("p"):
        text = p.get_text(strip=True).replace("\xa0", "")
        if not text and not p.find("img"):
            p.decompose()

    # Remove <hr> tags
    for hr in soup.find_all("hr"):
        hr.decompose()

    # ── 5. Transform headings ──
    # <h5> → doc-title
    for h5 in soup.find_all("h5"):
        h5.name = "h2"
        h5["class"] = ["doc-title"]
        _strip_style(h5)

    # Bold paragraphs with large font → doc-title
    # Bold paragraphs without large font → doc-subtitle
    for p in soup.find_all("p"):
        style = p.get("style", "")
        if "font-weight" in style and "bold" in style:
            text = p.get_text(strip=True)
            if not text:
                continue
            if "font-size" in style and ("12pt" in style or "14pt" in style):
                new_tag = soup.new_tag("h2")
                new_tag["class"] = ["doc-title"]
                new_tag.string = text
                p.replace_with(new_tag)
            else:
                new_tag = soup.new_tag("h3")
                new_tag["class"] = ["doc-subtitle"]
                new_tag.string = text
                p.replace_with(new_tag)

    # ── 6. Transform field definitions ──
    # Pattern: paragraph with text-indent or margin-left:30pt containing ALLCAPS + colon
    for p in soup.find_all("p"):
        style = p.get("style", "")
        has_indent = "text-indent" in style or "margin-left" in style.replace(" ", "")
        text = p.get_text(strip=True)

        # Also detect ALLCAPS label: at least 3 uppercase words followed by colon
        if has_indent and ":" in text:
            _convert_to_field_def(soup, p)
        elif re.match(r'^[A-Z\s\.\'/]{4,}:', text):
            _convert_to_field_def(soup, p)

    # ── 7. Transform images ──
    for img in soup.find_all("img"):
        style = img.get("style", "")
        # Extract max-width value
        mw_match = re.search(r'max-width:\s*(\d+)', style)
        max_w = int(mw_match.group(1)) if mw_match else 0

        _strip_style(img)

        if max_w > 100:
            # Large image → screenshot with original max-width preserved
            img["style"] = f"max-width: {max_w}px; width: 100%; height: auto;"
            fig = soup.new_tag("div")
            fig["class"] = ["doc-screenshot"]
            img.replace_with(fig)
            fig.append(img)
        elif max_w > 0:
            # Small image → icon
            img["class"] = ["doc-icon"]
            img["style"] = f"max-width: {max_w}px; height: auto;"
        else:
            img["style"] = "max-width: 100%; height: auto;"

    # ── 8. Convert single-column tables to lists ──
    for table in soup.find_all("table"):
        cols = table.find_all("col")
        cells = table.find_all("td")
        # Single-column layout table (navigation lists)
        if len(cols) <= 1 or all(not c.find("td") for c in table.find_all("tr") if len(c.find_all("td")) <= 1):
            texts = []
            for td in cells:
                text = td.get_text(strip=True)
                if text:
                    texts.append(text)
            if texts:
                ul = soup.new_tag("ul")
                for t in texts:
                    li = soup.new_tag("li")
                    li.string = t
                    ul.append(li)
                table.replace_with(ul)

    # ── 9. Flatten nested list hacks ──
    # <ol><li style="display:inline"><ul>... → just <ul>
    for li in soup.find_all("li"):
        style = li.get("style", "")
        if "display" in style and "inline" in style:
            inner_ul = li.find("ul")
            if inner_ul:
                parent_ol = li.parent
                if parent_ol:
                    parent_ol.replace_with(inner_ul)

    # ── 10. Strip ALL remaining inline styles from text elements ──
    for tag in soup.find_all(["p", "span", "div", "td", "th", "li", "ul", "ol", "tr", "table"]):
        _strip_style(tag)

    # Also strip align attributes
    for tag in soup.find_all(True, attrs={"align": True}):
        del tag["align"]

    # ── 10. Remove empty wrappers ──
    for tag in soup.find_all(["span"]):
        if not tag.attrs:
            tag.unwrap()

    # ── 11. Extract body content only ──
    body = soup.find("body")
    if body:
        content_html = body.decode_contents()
    else:
        content_html = soup.decode_contents()

    return HELP_CSS + f'<div class="doc-canvas">{content_html}</div>'


def _strip_style(tag):
    """Remove style attribute from a tag."""
    if tag.has_attr("style"):
        del tag["style"]
    if tag.has_attr("class") and not tag["class"]:
        del tag["class"]


def _convert_to_field_def(soup, p):
    """Convert a paragraph to a field-def card."""
    import re
    text = p.get_text(strip=True)
    # Split on first colon
    idx = text.index(":")
    name = text[:idx].strip()
    desc = text[idx + 1:].strip()

    div = soup.new_tag("div")
    div["class"] = ["field-def"]

    name_span = soup.new_tag("span")
    name_span["class"] = ["field-name"]
    name_span.string = name

    sep = soup.new_tag("span")
    sep["class"] = ["field-sep"]
    sep.string = ":"

    div.append(name_span)
    div.append(sep)
    div.append(" " + desc)

    p.replace_with(div)


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
