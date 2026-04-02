# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Progetto
Servizio web Q&A per documentazione OS1 (gestionale ERP di OSItalia). Chat BM25 + LLM (Groq) con auth OTP, backoffice admin, tracking costi, dark/light theme.

## Comandi sviluppo
```bash
pip install -r requirements.txt
python scripts/build_index.py --repo "../os1-documentation/Claude Code Playground"  # → searchdata/search.db
uvicorn app.main:app --reload --port 8000
```

## Deploy (Railway)
```bash
# 1. Rebuild search.db (include HTML preprocessato)
python scripts/build_index.py --repo "../os1-documentation/Claude Code Playground"

# 2. Commit e push — Railway fa auto-deploy da main
git add searchdata/search.db && git commit -m "Update search index" && git push
```

Railway config:
- **Volume**: `/app/data` (preserva `app.db` tra i deploy)
- **Variabili**: vedi sezione "Variabili d'ambiente"
- **Auto-deploy**: ON su push a `main`
- SSL automatico via Railway (no Caddy necessario)

## Architettura

### Database
- `searchdata/search.db` — FTS5 + HTML preprocessato. Committato nel repo. Rigenerabile con `build_index.py`.
- `data/app.db` — Utenti, conversazioni, usage, feedback, settings. **MAI cancellare.** Su volume persistente in prod. Schema `IF NOT EXISTS` (aggiornamenti additivi sicuri).

### Pipeline offline (`scripts/build_index.py`)
Legge repo docs → produce `data/search.db` con 4 ingestor:
- **table-def**: markdown tabelle DB (1 file = 1 chunk)
- **functional**: docs funzionali (splittati per `##`)
- **schema**: censimento tabelle per modulo
- **help**: file .htm con preprocessing BeautifulSoup → HTML professionale salvato in `html_content`

### Servizio web (FastAPI)
Flusso: `POST /api/ask` → rate limit → daily/monthly limit check → BM25 (top 10) → contesto → streaming Groq → SSE → salva in DB.

Strati:
- `app/main.py` — Lifespan: search.db + app.db + static files + help-files
- `app/config.py` — Settings con pydantic-settings, carica `.env`
- `app/version.py` — VERSION/BUILD/BUILD_DATE (incrementare BUILD ad ogni deploy)
- `app/routes/chat_routes.py` — Chat, conversazioni API, feedback, doc viewer, announcements
- `app/routes/auth_routes.py` — Login OTP, creazione utente in DB
- `app/routes/admin_routes.py` — Dashboard, utenti, usage (per-utente + per-dominio), domini, settings
- `app/auth/otp.py` — OTP in-memory, sender e allowed_domains da DB
- `app/auth/session.py` — Cookie firmato itsdangerous (24h, Secure in prod)
- `app/search/fts.py` — SearchIndex: wrapper SQLite FTS5, BM25 ranking, schema creation
- `app/search/query.py` — Retrieval + Groq streaming, modello da DB settings
- `app/db.py` — Singleton app database con schema completo
- `app/models/` — user.py, conversation.py, usage.py, domain.py

### Frontend (Jinja2 + vanilla JS)
- Layout 3 pannelli: sidebar conversazioni (sx) + chat (centro) + documenti (dx)
- Markdown rendering via marked.js CDN
- Design system SCAO: rosso `#E2231A`, DM Sans + Source Sans 3 + JetBrains Mono
- Dark/light theme toggle con localStorage
- Logo: `static/img/logo.png` (header), `static/img/logo-lg.png` (login/welcome)
- Document overlay: HTML preprocessato con canvas bianco, field-def cards, screenshot con ombra

### Sicurezza
- Tutti gli endpoint API richiedono sessione autenticata (401)
- Rate limiting: max 10 req/min per utente su `/api/ask`
- Daily limit per dominio (configurabile da admin)
- Monthly token limit per utente/dominio
- Cookie: HTTPOnly, SameSite=lax, Secure in produzione
- SQL parametrizzato ovunque (no injection)
- Static files: directory traversal protetto da FastAPI

## Variabili d'ambiente (.env)
| Variabile | Obbligatoria | Default | Descrizione |
|-----------|:---:|---------|-------------|
| `GROQ_API_KEY` | Si | — | API key Groq per LLM |
| `RESEND_API_KEY` | No | — | Per OTP email (senza: console) |
| `ALLOWED_EMAILS` | No | `*@scao.it` | Fallback se nessun dominio in DB |
| `ADMIN_EMAILS` | No | — | Pattern per flag admin al primo login |
| `SECRET_KEY` | Si | `change-me` | Firma cookie sessione |
| `PRODUCTION` | No | `false` | Abilita cookie Secure |
| `DOCS_REPO_PATH` | No | `../os1-documentation/...` | Path repo docs (solo dev locale) |
| `APP_DB_PATH` | No | `data/app.db` | Path app database |
| `DB_PATH` | No | `searchdata/search.db` | Path search index |
| `DEFAULT_MONTHLY_TOKEN_LIMIT` | No | `500000` | Limite token/mese default |
| `DEFAULT_MAX_MESSAGES_PER_CONVERSATION` | No | `20` | Limite domande/chat default |
| `BASE_URL` | No | — | URL pubblico (es. `https://os1docs.ai.scao.it`) per CORS/redirects |
| `GROQ_INPUT_PRICE` | No | `0.05` | $/M token input (modello standard) |
| `GROQ_OUTPUT_PRICE` | No | `0.08` | $/M token output (modello standard) |
| `GROQ_DEEP_INPUT_PRICE` | No | `0.59` | $/M token input (modello deep, es. 70b) |
| `GROQ_DEEP_OUTPUT_PRICE` | No | `0.79` | $/M token output (modello deep) |

## Admin (/admin)
- **Dashboard**: KPI (utenti, domande, costo, attivi) + domande recenti
- **Utenti**: lista con usage mensile, dettaglio con barra progresso limite
- **Consumi**: breakdown per utente e per dominio, export CSV
- **Domini**: gestione accessi con limite giornaliero e mensile per dominio
- **Impostazioni**: modello Groq, email mittente OTP, max domande/chat, banner annunci

## Convenzioni
- Codice in inglese, UI in italiano
- Groq via client `openai` (AsyncOpenAI) → `api.groq.com/openai/v1`
- `searchdata/search.db` committato nel repo (rigenerabile), `data/app.db` MAI committato (volume)
- Incrementare `BUILD` e aggiornare `BUILD_DATE` in `app/version.py` ad **ogni commit** (non solo deploy)

## Chunking
I chunk devono essere GRANDI (file interi). Ogni file HTML del help OS1 è già un concetto coerente.

## Repo documentazione sorgente
`d:\dev\os1-documentation\Claude Code Playground` — ~2300+ chunks indicizzati
