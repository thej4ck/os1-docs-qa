"""Chat routes: page and SSE streaming endpoint."""

import json
from collections import defaultdict

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse

from app.auth.session import get_session_email
from app.search import query as query_module

router = APIRouter()

# In-memory conversation history per session email
# {email: [{"role": "user"|"assistant", "content": "..."}]}
_history: dict[str, list[dict]] = defaultdict(list)
MAX_HISTORY = 10  # Keep last N messages (5 exchanges)


def _templates():
    from app.main import templates
    return templates


def _require_auth(request: Request):
    """Check auth, return email or None."""
    email = get_session_email(request)
    return email


@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    email = get_session_email(request)
    if email:
        return RedirectResponse(url="/chat", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    email = _require_auth(request)
    if not email:
        return RedirectResponse(url="/login", status_code=302)

    history = _history.get(email, [])
    return _templates().TemplateResponse(
        "chat.html", {"request": request, "email": email, "history": history}
    )


@router.post("/api/ask")
async def ask(request: Request, question: str = Form(...)):
    email = _require_auth(request)
    if not email:
        return RedirectResponse(url="/login", status_code=302)

    question = question.strip()
    if not question:
        return HTMLResponse("<p>Scrivi una domanda.</p>")

    # Add user message to history
    user_history = _history[email]
    user_history.append({"role": "user", "content": question})

    # Keep only last N messages
    if len(user_history) > MAX_HISTORY:
        _history[email] = user_history[-MAX_HISTORY:]
        user_history = _history[email]

    # Prepare history for LLM (exclude last user msg, it's added with context)
    llm_history = user_history[:-1]

    async def event_generator():
        full_response = []
        sources = []

        async for token, token_sources in query_module.ask_stream(
            question, history=llm_history
        ):
            if token_sources:
                sources = token_sources
            if token:
                full_response.append(token)
                # Send token as SSE data
                yield {"data": json.dumps({"token": token})}

        # Send sources at the end
        if sources:
            yield {"data": json.dumps({"sources": sources})}

        # Signal completion
        yield {"data": json.dumps({"done": True})}

        # Save assistant response to history
        assistant_text = "".join(full_response)
        if assistant_text:
            _history[email].append({"role": "assistant", "content": assistant_text})
            if len(_history[email]) > MAX_HISTORY:
                _history[email] = _history[email][-MAX_HISTORY:]

    return EventSourceResponse(event_generator())
