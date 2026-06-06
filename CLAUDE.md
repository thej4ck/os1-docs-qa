# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Progetto
**OS1 Virgilio** — servizio web Q&A per la documentazione OS1 (gestionale ERP di OSItalia).
Chat con **retrieval ibrido BM25 + semantico (model2vec)** e LLM (Groq), 4 esperti specialisti,
auth OTP + access-token, **self-signup freemium con tier**, backoffice admin, tracking costi, dark/light theme.

- `app/version.py` è single source of truth: `VERSION`, `BUILD`, `BUILD_DATE`, `PRODUCT_NAME = "OS1 Virgilio"`.
- Stato attuale: VERSION `2.1.0`, BUILD `76`.
- Stack web: FastAPI `0.135.1` + **Starlette `>=1.0.1,<2`** (pin floating; chiude **CVE-2026-48710** Host-header → path poisoning). NB: con Starlette 1.x `Jinja2Templates.TemplateResponse` vuole `request` come **primo** arg: `TemplateResponse(request, name, context)`.

## Comandi sviluppo
```bash
pip install -r requirements.txt
# (solo se cambia il modello semantico — richiede torch/transformers, vedi distill)
python scripts/distill_model.py                  # → searchdata/static_model/
python scripts/build_index.py --repo "../os1-documentation/Claude Code Playground"  # → searchdata/search.db (+ embeddings)
uvicorn app.main:app --reload --port 8000
```

## Deploy
Due path supportati. Il search.db è **baked nell'immagine** (rigenerato quando cambiano i docs);
`data/app.db` vive su **volume persistente** (MAI nell'immagine).

### Railway (primario)
Build via `Dockerfile` (l'ARG `RAILWAY_GIT_COMMIT_SHA` busta la cache). Auto-deploy ON su push a `main`.
```bash
# 1. Prepara l'indice (search.db + copia help-files con immagini)
./scripts/prepare_deploy.sh        # oppure: python scripts/build_index.py --repo "..."
# 2. Commit & push — Railway fa auto-deploy da main
git add searchdata/search.db searchdata/static_model && git commit -m "Update index" && git push
```
Railway config: Volume su `/app/data` (preserva `app.db`), variabili in dashboard, SSL automatico.

### Self-host (alternativa)
`docker-compose.yml` → 2 servizi: `app` (uvicorn:8000) + `caddy:2-alpine` (reverse proxy 80/443, SSL Let's Encrypt auto via `Caddyfile`).
Volumi: `app-data` (app.db), `caddy-data`/`caddy-config` (certs). Variabile `DOMAIN` per l'host.

## Architettura

### Database
- `searchdata/search.db` — FTS5 + HTML preprocessato + **embeddings semantici**. Committato nel repo. Rigenerabile con `build_index.py`.
- `searchdata/static_model/` — modello **model2vec distillato** (256-dim, L2-norm). Committato. Generato da `distill_model.py`.
- `data/app.db` — Utenti, conversazioni, messaggi, usage, feedback, domini, settings. **MAI cancellare/committare.** Volume persistente in prod. Schema additivo (`IF NOT EXISTS` + ALTER idempotenti, WAL, `foreign_keys=ON`).

### Pipeline offline (`scripts/`)
- **`build_index.py`** (~1130 righe) — legge il repo docs → `search.db` con 5 ingestor:
  - **table-def**: markdown tabelle DB (1 file = 1 chunk) + integrity/cross-ref
  - **functional**: docs funzionali (split per `##`)
  - **schema**: censimento tabelle per modulo
  - **help**: `.htm` RoboHelp → HTML semantico preprocessato (BeautifulSoup) in `html_content`
  - **pdf** (schede operative): PyMuPDF/`fitz`, parse metadata + estrazione immagini → WebP
  - Poi `add_embeddings()`: vettori model2vec L2-norm nella tabella `embeddings`.
  - CLI: `--repo`, `--db`, `--static-model`, `--skip-embeddings`, `--embeddings-only`.
- **`distill_model.py`** — distilla `paraphrase-multilingual-MiniLM-L12-v2` → model2vec static in `searchdata/static_model/`. **Solo locale pre-push** (serve `pip install "model2vec[distill]"` = torch/transformers, MAI su Railway).
- **`optimize_images.py`** — ridimensiona le immagini avatar esperti (master in `static/img/originals/`).
- **`prepare_deploy.sh`** — build index + copia `help-files/` dal repo docs.

### search.db (schema)
- `documents` — id, source_file, module, doc_type, title, content, `html_content`, indexed_at
- `docs_fts` — FTS5 esterna (title, content), `unicode61 remove_diacritics`, trigger auto-sync, stopwords IT
- `embeddings` — doc_id, vec (BLOB float32 L2-norm)
- `embedding_meta` — model, dim (256), count, built_at

### Servizio web (FastAPI)
Flusso `POST /api/ask`: auth → rate limit (10/min) → limiti tier (daily/monthly request + token) → **disambiguazione opzionale** → **retrieval ibrido** → budget contesto → streaming Groq (SSE) → **remap citazioni** → salva in DB.

Strati:
- `app/main.py` — Lifespan: search.db (RO) + embeddings + app.db + static + help-files. Healthcheck `GET /healthz`.
- `app/config.py` — Settings pydantic (`.env`). **Il pricing NON è più qui**: è dinamico in `query.py`.
- `app/version.py` — VERSION/BUILD/BUILD_DATE/PRODUCT_NAME.
- `app/routes/chat_routes.py` (~780) — chat, ask SSE, conversazioni CRUD/export, feedback, doc viewer, announcements, usage summary, onboarding, request-upgrade, `/api/debug/retrieve` (solo dev).
- `app/routes/auth_routes.py` — login OTP, verify, logout, **access-token login** (`/login/token`, `/api/access-token[/regenerate]`).
- `app/routes/signup_routes.py` — **self-signup freemium** (`/signup`, `/signup/verify`) con autoprovisioning TRIAL.
- `app/routes/admin_routes.py` (~555) — dashboard, utenti, usage, costi, conversazioni, domini, feedback, settings, export CSV.
- `app/auth/otp.py` — OTP in-memory (TTL 300s, cooldown 10s, max 5 tentativi), sender e domini da DB.
- `app/auth/session.py` — cookie firmato itsdangerous (24h, HTTPOnly, SameSite=lax, Secure in prod).
- `app/auth/email_sender.py` + `email_templates.py` — invio via Resend (console in dev), template welcome/trial/admin.
- `app/db.py` — singleton app.db, schema + migrazioni additive.
- `app/models/` — user, conversation, usage, **domain** (tier+trial, ~370), settings (KV).

### Server MCP (`app/mcp/`) — retrieval-only
Server **MCP remoto** (FastMCP, Streamable HTTP) montato su **`/mcp`** in [main.py](app/main.py) via
`mcp.http_app(path="/")` + `combine_lifespans` (il session-manager FastMCP gira insieme al lifespan esistente,
senza doppio-init). Espone **solo retrieval** (costo Groq zero), schema canonico ChatGPT Deep Research (digerito anche da Claude):
- `search(query)` → `{"results":[{id,title,url}]}` — riusa `query.mcp_search()` (= `_hybrid_candidates`, **NO LLM/budget/rerank a pagamento**).
- `fetch(id)` → `{id,title,text,url,metadata}` — `query.mcp_fetch()` → `SearchIndex.get_document()` (match slash-tolerant come `/api/doc`). `id` = `source_file`.
- `app/mcp/tools.py` `_doc_url()` usa `settings.base_url` per URL citabili.
- **Auth** — modalità **configurabile da admin** (`/admin/mcp`): setting `mcp_auth_mode` = `off|bearer|oauth` (precedenza **DB admin > env > default**: `oauth` in prod, `off` in dev). Letta all'avvio (route OAuth montate a import-time) → **il cambio modalità applica al RIAVVIO**. Logica in `resolve_mcp_auth_mode`/`build_mcp_auth` ([app/mcp/auth.py](app/mcp/auth.py)):
  - `oauth` (env `MCP_OAUTH_ENABLED` come fallback) → **OAuth 2.1 AS autonomo** `OS1OAuthProvider` ([app/mcp/oauth.py](app/mcp/oauth.py)): DCR (RFC 7591) + PKCE S256 (verificata dal framework FastMCP) + authorization_code/refresh/revoke. Login = pagina dedicata **`/mcp-login`** ([routes/mcp_auth_routes.py](app/routes/mcp_auth_routes.py)) che riusa le primitive OTP (NON il login-cookie: completa con redirect al client). Token **sha256-hashed** in app.db (`oauth_clients`/`oauth_login_tickets`/`oauth_auth_codes`/`oauth_tokens` — [db.py](app/db.py) + [models/oauth.py](app/models/oauth.py)). Discovery `.well-known` montata a **root** dominio (sotto `/mcp` non basta). Scope `docs:read`. Sblocca **claude.ai + ChatGPT** (richiedono OAuth+PKCE). IdP esterni esclusi (prodotto venduto apertamente → IdP non prevedibile).
  - `MCP_AUTH_ENABLED` (default `false`) → **Bearer = `access_token` utente** via `OS1TokenVerifier` → mappa a utente OS1. Solo dev/CLI: Claude **Code** (`--header`), Messages API, Inspector. NON le UI connettori.
  - `off` → `/mcp` **no-auth** (solo dev/Inspector — NON per prod).
- **Master switch (LIVE)**: setting `mcp_enabled` (admin `/admin/mcp`) → middleware ASGI `_MCPMasterGate` ([main.py](app/main.py)) fa rispondere **503** a `/mcp*`+`/mcp-login` se off. Effetto **immediato (no riavvio)**. Default **on in prod** (senza env), off in dev.
- **Gate per-dominio**: `allowed_domains.mcp_enabled` (toggle in `/admin/domains`) → `is_mcp_enabled_for_email` ([models/domain.py](app/models/domain.py)) nega l'auth (verify_token + `/mcp-login`) agli utenti di un dominio con MCP off.
- **Admin** `/admin/mcp`: stato/endpoint, master, modalità auth, lista **client OAuth** (revoca) e **token attivi** (revoca). Modello `app/models/oauth.py`.
- **Endpoint**: reale `/mcp/`; `/mcp` fa 307→`/mcp/` (i client conformi httpx/Claude/ChatGPT preservano l'auth same-origin).
- **Dep**: `fastmcp>=3.4,<4` (3.4.2; pulls mcp/authlib/cryptography/pyjwt). Richiede **`starlette>=1.0.1,<2`** (migrazione 1.x fatta su main, build 75; chiude **CVE-2026-48710**). `fastapi` resta `0.135.1`.

### Retrieval ibrido (`app/search/`)
- `query.py` (~860) — **orchestratore**. `ask_stream()` async generator: disambigua → candidati ibridi → budget → context → streaming → cost. Contiene `ALLOWED_MODELS`, `CONTEXT_PRESETS`, prompt CORE, deep mode, remap citazioni, screenshot.
- `fts.py` — wrapper FTS5/BM25. AND sui termini originali; fallback OR con expansion se <3 risultati.
- `embeddings.py` — model2vec static. Carica corpus L2-norm da search.db, query embed CPU (mean-pool) → cosine. Degrada a BM25-only se non pronto.
- `fusion.py` — **RRF** (`rrf_fuse`, k=60, Cormack 2009) con pesi adattivi (lexical/semantic).
- `signals.py` — re-rank a segnali ERP CPU-side (`rescore`): definition boost (table-def/schema), overlap stem identifier, coerenza file/modulo, noise penalty; + `adaptive_weights` (query tecnica CamelCase/ALLCAPS/_ → pesa BM25, naturale → pesa semantico).
- `expand.py` — query expansion deterministica (mappa lemma ERP→sinonimi, stemming IT). Solo nel ramo OR-fallback di BM25, mai sulla query densa (anti-drift).
- `disambiguate.py` — solo 1° messaggio, query ≤3 token, ≥3 topic non-dominanti, no discriminator → domanda di chiarimento LLM (`llama-3.1-8b-instant`, JSON options).
- `rerank.py` — re-rank LLM (`llama-3.1-8b-instant`, score 0–10). **OFF di default** (admin setting `reranking_enabled`), attivo solo se >5 candidati. Token/costo contabilizzati separatamente (`rerank_*`).

Ordine pipeline: BM25 ∪ semantic → **RRF fuse** → **signals.rescore** → (opz. LLM rerank) → **budget trim in PAROLE**.

### Esperti specialisti (`agents.py`)
4 personas, **selezione manuale dall'UI** (param `agent`/`agent_id`), nessun router LLM. System prompt = CORE (grounding invariante) + stile esperto. L'esperto scelto è bloccato sulla conversazione (`get_conversation_agent`).

| id | Label UI | Persona | Stile |
|----|----------|---------|-------|
| `virgilio` | Il Manuale | concetti/processi, mai step-by-step (brief) | spiegazione |
| `pilota` | Guidami | Prerequisiti → passi numerati → verifica | procedurale |
| `doc` | Ho un problema | Cause → verifiche → soluzioni | diagnostico |
| `stella` | Sono nuovo | zero acronimi, analogie, next step | onboarding/tutor |

### Modelli & pricing (dinamico)
`ALLOWED_MODELS` in [query.py](app/search/query.py) (NON più env var). Modello standard e "deep" scelti da settings DB (`groq_model` / `groq_deep_model`); default `llama-3.1-8b-instant`. Reasoning effort per i gpt-oss.

| config key | model_id | input $/M | output $/M |
|---|---|---|---|
| `llama-3.1-8b-instant` | llama-3.1-8b-instant | 0.05 | 0.08 |
| `llama-3.3-70b-versatile` | llama-3.3-70b-versatile | 0.59 | 0.79 |
| `openai/gpt-oss-120b:{low,medium,high}` | openai/gpt-oss-120b | 0.15 | 0.60 |
| `openai/gpt-oss-20b:{low,medium,high}` | openai/gpt-oss-20b | 0.075 | 0.30 |

Budget contesto in **parole** (`CONTEXT_PRESETS`: conservative 5k / normal 15k / aggressive 30k). Deep mode = `min(budget × 2.5, 60k)` + addendum "sii esaustivo" (saltato per esperti brief). Costo: `_calculate_cost` con sconto 50% sui cached token Groq. `reasoning_tokens` tracciati ma non fatturati. Rerank LLM contabilizzato a parte.

### Usage tiers (`app/models/domain.py`)
Tier applicati ai **domini** (`allowed_domains.tier`), risolti per email. `TIER_PRESETS`:

| Tier | Req/mese | Token/mese | Daily | Note |
|------|---------:|-----------:|------:|------|
| TRIAL | 100 | 2.5M | 0 | auto-provisioning self-signup, `expires_at` |
| FREE | 5 | 100k | 2 | freemium minimo |
| BASE | 100 | 2.5M | 0 | default nuovi domini |
| PLUS | 300 | 7M | 0 | |
| POWER | 800 | 18M | 0 | |

Risoluzione limite token utente: override `users.monthly_token_limit` → tier dominio → `DEFAULT_MONTHLY_TOKEN_LIMIT`. Downgrade lazy TRIAL→FREE alla scadenza. Email personali (gmail/yahoo/…) bloccate al signup.

### app.db (schema principale)
- `users` — email, is_admin, monthly_token_limit (override), `access_token` (passwordless), onboarding_completed, created_at, last_login
- `conversations` — id (uuid), user_id, title, created/updated
- `messages` — role, content, sources(json), prompt/completion/cached/rerank tokens, cost_usd, rerank_cost_usd, model, rerank_model, **agent**, created_at
- `feedback` — message_id, rating(-1/1), category, comment, query, response_preview, chunks_used, model, search_scores
- `allowed_domains` — pattern, tier, monthly_request/token_limit, daily_limit, enabled, expires_at, dati registrant trial
- `app_settings` — KV admin-config (modello, suppress_reasoning, reranking_enabled, otp_sender_*, max_messages_per_conversation, announcement, admin_notification_email, trial_days…)
- vista `monthly_usage` — aggregato per utente/mese

### Frontend (Jinja2 + vanilla JS)
- Layout 3 pannelli: conversazioni (sx) + chat (centro) + documenti (dx). Landing "Virgilio & Co." + login.
- Markdown via marked.js; citazioni `[Dn]` come chip apice; immagini in carousel.
- Design system SCAO: rosso `#E2231A`, DM Sans + Source Sans 3 + JetBrains Mono. Dark/light con localStorage.
- CTA "approfondimento" inline sotto la risposta → ri-chiama in deep mode.

### Sicurezza
- Tutti gli endpoint API richiedono sessione (401). Admin gated.
- Rate limit: 10 req/min su `/api/ask`, 3/min su invio email. Limiti tier per dominio (daily/monthly request + token).
- Cookie HTTPOnly, SameSite=lax, Secure in prod. SQL parametrizzato. Static traversal protetto da FastAPI.

## Variabili d'ambiente (.env)
| Variabile | Obbligatoria | Default | Descrizione |
|-----------|:---:|---------|-------------|
| `GROQ_API_KEY` | Si | — | API key Groq |
| `RESEND_API_KEY` | No | — | OTP/email (senza: console) |
| `ALLOWED_EMAILS` | No | `*@scao.it` | Fallback se nessun dominio in DB |
| `ADMIN_EMAILS` | No | — | Pattern flag admin al primo login |
| `SECRET_KEY` | Si | `change-me` | Firma cookie sessione |
| `PRODUCTION` | No | `false` | Cookie Secure + trust proxy headers |
| `BASE_URL` | No | — | URL pubblico (CORS/redirect) |
| `DOMAIN` | No (self-host) | `localhost` | Host per Caddy |
| `DB_PATH` | No | `searchdata/search.db` | Search index (deploy: `data/search.db`) |
| `APP_DB_PATH` | No | `data/app.db` | App database |
| `DOCS_REPO_PATH` | No | `../os1-documentation/...` | Repo docs (solo dev/build) |
| `STATIC_MODEL_PATH` | No | `searchdata/static_model` | Dir model2vec distillato |
| `HYBRID_ENABLED` | No | `true` | BM25+semantic; `false` = BM25-only |
| `DEFAULT_MONTHLY_TOKEN_LIMIT` | No | `500000` | Fallback limite token/mese |
| `DEFAULT_MAX_MESSAGES_PER_CONVERSATION` | No | `20` | Limite domande/chat default |

> Il pricing dei modelli **non** è più via env (`GROQ_*_PRICE` rimossi): è in `ALLOWED_MODELS` ([query.py](app/search/query.py)).

## Admin (/admin)
- **Dashboard**: KPI (utenti, domande, costo, attivi) + domande recenti
- **Utenti**: lista con usage mensile, dettaglio + override limite
- **Consumi/Costi**: breakdown per utente/dominio/modello/periodo, export CSV
- **Conversazioni**: viewer completo
- **Domini**: CRUD con tier, limiti, trial, **toggle MCP per-dominio**
- **MCP**: master switch (live), modalità auth (off/bearer/oauth, applica al riavvio), client OAuth + token (revoca)
- **Feedback**: lista con filtri categoria/data
- **Impostazioni**: modello standard/deep, suppress_reasoning, reranking_enabled, email mittente OTP, max domande/chat, banner annunci, trial days, notifiche admin

## Convenzioni
- Codice in inglese, UI in italiano
- Groq via client `openai` (AsyncOpenAI) → `api.groq.com/openai/v1`
- `searchdata/search.db` + `searchdata/static_model/` committati (rigenerabili), `data/app.db` MAI committato
- **Incrementare `BUILD` e aggiornare `BUILD_DATE` in [app/version.py](app/version.py) ad ogni commit**
- **Aggiornare questo `CLAUDE.md` ad ogni commit che introduce informazioni rilevanti** (nuovi moduli/endpoint, cambi architettura/pipeline/schema/env, modelli o pricing, tier/limiti, deploy). Commit puramente cosmetici/fix interni non lo richiedono.

## Chunking
Chunk GRANDI (file interi). Ogni file HTML help OS1 è già un concetto coerente.

## Repo documentazione sorgente
`d:\dev\os1-documentation\Claude Code Playground` — ~2300+ chunks indicizzati

## Troubleshooting noto

### Risposte troncate a metà frase/parola in coda
**Sintomo**: la risposta visibile si taglia mid-parola in fondo (es. "...assistenza tecn"), con QUALSIASI modello (gpt-oss E llama).

**CAUSA VERA ACCERTATA (giu 2026, build 66) — RACE DI RENDERING FRONTEND.** NON è Groq, NON è il modello, NON è il cap token. Provato con test Groq diretti + esecuzione di `ask_stream`:
- Groq ritorna sempre `finish=stop` con risposta COMPLETA e chiusura pulita (mai `length`, mai mid-parola).
- `ask_stream` (backend) restituisce testo completo, `truncated=False`.
- Il taglio era nel frontend [app/templates/chat.html](app/templates/chat.html): `scheduleRender` faceva throttle a 60ms catturando `fullText`; un timer pendente scattava DOPO `renderFinal` e **sovrascriveva il testo completo con la cattura stale** → taglio mid-parola in coda.

**Fix (build 66)**: (1) cancella `renderTimer` prima di `renderFinal` ai due call-site; (2) `scheduleRender` renderizza sempre `fullText` live, mai una cattura.

**Depistaggi scartati** (NON erano la causa, ma le modifiche restano valide):
- `include_reasoning: False` su gpt-oss/harmony (build 65) → rimosso di default. Toggle admin `suppress_reasoning` (default OFF) per rollback.
- Budget token / `max_completion_tokens` → mai raggiunto (`finish` sempre `stop`).

**Diagnosi via log** — `[ask_stream] Stream complete ... finish=<reason>`:
- `finish=stop` + risposta visibile monca → guardare il FRONTEND (race di render), non il modello.
- `finish=length` + `completion_tokens` ≈ cap → limite token → alza `max_completion_tokens`. `usage.truncated` attiva il badge "Risposta interrotta" + Continua (raro).

### Railway CLI
Progetto `doc-os1-ai`, env `production`, service `os1-docs-qa` (URL `https://os1.ai.scao.it`). Log: `railway logs -d --lines N`.
