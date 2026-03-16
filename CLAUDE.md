# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Progetto
Servizio web Q&A per documentazione OS1 (gestionale ERP). Chat BM25 + LLM (Groq) con auth OTP, backoffice admin, tracking costi.

## Comandi
```bash
pip install -r requirements.txt
python scripts/build_index.py --repo "../os1-documentation/Claude Code Playground"
uvicorn app.main:app --reload --port 8000
```

## Architettura

### Database
- `data/search.db` — FTS5 read-only, rigenerabile con `build_index.py`. Sovrascrivibile.
- `data/app.db` — Dati utente, conversazioni, usage, settings. **MAI cancellare.** Schema con `IF NOT EXISTS`.

### Pipeline offline (`scripts/build_index.py`)
Legge repo docs → produce `data/search.db`. 4 ingestor: table-def, functional, schema, help.

### Servizio web (FastAPI)
Flusso: `POST /api/ask` → BM25 (top 10) → contesto → streaming Groq → SSE.

Strati:
- `app/main.py` — Lifespan: apre search.db read-only + app.db, monta static files
- `app/routes/chat_routes.py` — Chat + API conversazioni + feedback + doc viewer
- `app/routes/auth_routes.py` — Login OTP + creazione utente in DB
- `app/routes/admin_routes.py` — Backoffice admin: dashboard, utenti, usage, settings
- `app/auth/otp.py` — OTP in-memory, sender configurabile da admin
- `app/auth/session.py` — Cookie firmato itsdangerous (24h)
- `app/search/query.py` — Retrieval + Groq streaming, modello configurabile da admin
- `app/db.py` — Singleton app database (users, conversations, messages, feedback, settings)
- `app/models/` — CRUD: user.py, conversation.py, usage.py

### Frontend (Jinja2 + vanilla JS)
- Layout 3 pannelli: sidebar conversazioni (sx) + chat (centro) + documenti (dx)
- Markdown rendering via marked.js CDN
- Design system SCAO: rosso `#E2231A`, DM Sans + Source Sans 3
- Logo: `static/logo.png`

## Variabili d'ambiente (.env)
- `GROQ_API_KEY` — obbligatoria
- `RESEND_API_KEY` — per OTP email (senza, stampa in console)
- `ALLOWED_EMAILS` — pattern allowlist (es. `*@scao.it`)
- `ADMIN_EMAILS` — pattern admin (es. `admin@scao.it`)
- `DB_PATH` — search.db (default: `data/search.db`)
- `APP_DB_PATH` — app.db (default: `data/app.db`)
- `SECRET_KEY` — firma cookie sessione
- `DEFAULT_MONTHLY_TOKEN_LIMIT` — limite token/mese (default: 500000)
- `DEFAULT_MAX_MESSAGES_PER_CONVERSATION` — limite domande/chat (default: 20)

## Settings configurabili da admin (/admin/announcement)
Modello Groq, email mittente OTP, max domande/chat, annuncio banner. Salvati in `app_settings` table.

## Convenzioni
- Codice in inglese, UI in italiano
- Groq via client `openai` (AsyncOpenAI) → `api.groq.com/openai/v1`
- `data/` in `.gitignore` — search.db rigenerabile, **app.db da preservare nei deploy**

## Chunking
I chunk devono essere GRANDI (file interi). Ogni file HTML del help OS1 è già un concetto coerente.

## Repo documentazione sorgente
`d:\dev\os1-documentation\Claude Code Playground` — ~2300+ chunks indicizzati
