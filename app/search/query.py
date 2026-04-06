"""Retrieval + Groq LLM streaming for Q&A."""

import logging
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from app.config import settings
from app.search.fts import SearchIndex

DEFAULT_SYSTEM_PROMPT = """\
Sei l'assistente documentazione di OS1, il gestionale ERP di OSItalia. \
Rispondi SOLO in base al contesto documentale fornito, in italiano.

# Regole fondamentali

1. **Risposta diretta PRIMA di tutto**: rispondi alla domanda in 1-3 righe, POI dettaglia.
2. **NON inventare**: se il contesto non contiene la risposta, dillo. Meglio breve e onesto che lungo e inventato.
3. **Solo emoji codificate**: 📌 titolo, 📍 percorso, ⚠️ warning, 💡 tip, ℹ️ nota, ✅ obbligatorio, 📄 fonte.

# Schema di risposta

📌 **Titolo operazione**

**→ Risposta diretta (1-3 righe)**

---

📍 **Percorso:** Menù → Voce → Sottovoce

---

> ⚠️ **Prima di iniziare** (se ci sono prerequisiti)

---

### Procedura

Passaggi numerati (**1.** **2.** **3.**) con riga vuota tra ogni passaggio. \
Separatore `---` ogni 2-3 passaggi. Callout `💡` o `⚠️` per spezzare il ritmo.

Se un passaggio coinvolge una maschera, inserisci tabella campi:

| Campo | Cosa inserire | Obbl. |
|-------|--------------|:-----:|
| **Nome** | Descrizione | ✅/— |

---

📄 *Fonte: nome-documento (file.htm)*

### Per saperne di più
- 3 domande suggerite specifiche e correlate

# Stile

- `###` per sezioni (mai h1/h2). **Grassetto** per campi/pulsanti/azioni. `Codice` solo per nomi tecnici DB.
- Paragrafi max 3 righe, poi spezza con elenco o callout. Mai muri di testo.
- Non iniziare con "Certo!", non ripetere la domanda, non usare emoji fuori dalla lista.

# Screenshot

Nel contesto: `[Screenshot: descrizione | url]` → includili con `![descrizione](url)` subito dopo il paragrafo pertinente. Max 2-3, solo se rilevanti.

# Se non hai la risposta

Dì: "La documentazione disponibile non copre questo aspetto." \
Indica cosa hai trovato di parziale e suggerisci termini alternativi o di contattare il supporto OSItalia."""

DEFAULT_DEEP_ADDENDUM = """\
## MODALITÀ APPROFONDIMENTO
Stai rispondendo in modalità approfondita. Hai a disposizione più contesto documentale.
- Sii ESAUSTIVO: elenca TUTTI gli elementi, campi, tabelle pertinenti, non solo i principali.
- Fornisci dettagli tecnici completi: nomi esatti di tabelle DB, campi, relazioni.
- Usa tabelle Markdown per strutturare elenchi lunghi.
- Se il contesto include molti documenti, sintetizzali tutti, non solo i primi.
- Non tralasciare informazioni: l'utente ha chiesto esplicitamente di approfondire."""


def _get_prompt_setting(key: str, default: str) -> str:
    """Get a prompt from app_settings, falling back to default."""
    try:
        from app.db import get_conn
        row = get_conn().execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row and row["value"].strip():
            return row["value"]
    except Exception:
        pass
    return default


def get_system_prompt() -> str:
    """Return the system prompt (always the same for cache-friendly prefix)."""
    return _get_prompt_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)


def get_deep_addendum() -> str:
    """Return the deep addendum text (appended to user message, not system prompt)."""
    return _get_prompt_setting("deep_addendum", DEFAULT_DEEP_ADDENDUM)

# Shared index instance — set by main.py at startup
_index: SearchIndex | None = None
_client: AsyncOpenAI | None = None


def init(index: SearchIndex):
    global _index, _client
    _index = index
    _client = AsyncOpenAI(
        api_key=settings.groq_api_key,
        base_url="https://api.groq.com/openai/v1",
    )


# ── Allowed models with pricing ($/M tokens) ──
ALLOWED_MODELS = {
    "llama-3.1-8b-instant": {
        "label": "Llama 3.1 8B Instant",
        "input_price": 0.05,
        "output_price": 0.08,
        "context_window": 131_072,
    },
    "llama-3.3-70b-versatile": {
        "label": "Llama 3.3 70B Versatile",
        "input_price": 0.59,
        "output_price": 0.79,
        "context_window": 131_072,
    },
    "openai/gpt-oss-120b:low": {
        "label": "GPT-OSS 120B (Low Effort)",
        "model_id": "openai/gpt-oss-120b",
        "reasoning_effort": "low",
        "input_price": 0.15,
        "output_price": 0.60,
        "context_window": 131_072,
    },
    "openai/gpt-oss-120b:medium": {
        "label": "GPT-OSS 120B (Medium Effort)",
        "model_id": "openai/gpt-oss-120b",
        "reasoning_effort": "medium",
        "input_price": 0.15,
        "output_price": 0.60,
        "context_window": 131_072,
    },
    "openai/gpt-oss-120b:high": {
        "label": "GPT-OSS 120B (High Effort)",
        "model_id": "openai/gpt-oss-120b",
        "reasoning_effort": "high",
        "input_price": 0.15,
        "output_price": 0.60,
        "context_window": 131_072,
    },
    "openai/gpt-oss-20b:low": {
        "label": "GPT-OSS 20B (Low Effort)",
        "model_id": "openai/gpt-oss-20b",
        "reasoning_effort": "low",
        "input_price": 0.075,
        "output_price": 0.30,
        "context_window": 131_072,
    },
    "openai/gpt-oss-20b:medium": {
        "label": "GPT-OSS 20B (Medium Effort)",
        "model_id": "openai/gpt-oss-20b",
        "reasoning_effort": "medium",
        "input_price": 0.075,
        "output_price": 0.30,
        "context_window": 131_072,
    },
    "openai/gpt-oss-20b:high": {
        "label": "GPT-OSS 20B (High Effort)",
        "model_id": "openai/gpt-oss-20b",
        "reasoning_effort": "high",
        "input_price": 0.075,
        "output_price": 0.30,
        "context_window": 131_072,
    },
}

CONTEXT_PRESETS = {
    "conservative": 5_000,
    "normal": 15_000,
    "aggressive": 40_000,
}


def _get_context_budget(deep: bool = False) -> int:
    """Get max context words from admin preset."""
    preset = "normal"
    try:
        from app.db import get_conn
        row = get_conn().execute("SELECT value FROM app_settings WHERE key = 'context_preset'").fetchone()
        if row and row["value"] in CONTEXT_PRESETS:
            preset = row["value"]
    except Exception:
        pass
    budget = CONTEXT_PRESETS[preset]
    return budget * 2 if deep else budget


async def check_disambiguation(
    question: str, is_first_message: bool
) -> dict | None:
    """Check if query needs disambiguation. Returns options dict or None."""
    if _index is None or not is_first_message:
        return None

    from app.search.disambiguate import analyze_ambiguity, ask_disambiguation

    candidates = _index.search(question, limit=20)
    result = analyze_ambiguity(question, candidates, is_first_message)
    if not result:
        return None

    # Use LLM to generate natural disambiguation question
    if _client:
        llm_result = await ask_disambiguation(question, result["areas"], _client)
        if llm_result:
            return llm_result

    # Fallback (no client)
    return {
        "question": "Ho trovato risultati in diverse aree. Di quale ti interessa?",
        "options": [
            {"label": a["label"], "topic": a["topic"], "keywords": ""}
            for a in result["areas"][:4]
        ],
    }


async def retrieve_with_budget(
    question: str, deep: bool = False, topic_filter: str | None = None,
) -> tuple[list[dict], dict | None]:
    """Search FTS5, optionally rerank, return docs up to word budget.

    Returns (selected_docs, rerank_usage_or_None).
    """
    if _index is None:
        return [], None
    max_words = _get_context_budget(deep)
    candidates = _index.search(question, limit=50, topic_filter=topic_filter)

    # If topic filter yielded too few results, fall back to unfiltered
    if topic_filter and len(candidates) < 3:
        candidates = _index.search(question, limit=50)

    # LLM reranking (if enabled and client available)
    rerank_usage = None
    if _client and len(candidates) > 5 and _is_reranking_enabled():
        from app.search.rerank import rerank
        candidates, rerank_usage = await rerank(question, candidates[:20], _client)

    selected = []
    word_count = 0
    for doc in candidates:
        doc_words = len(doc["content"].split())
        if word_count + doc_words > max_words and selected:
            break
        selected.append(doc)
        word_count += doc_words
    return selected, rerank_usage


def _is_reranking_enabled() -> bool:
    """Check admin setting for reranking toggle."""
    try:
        from app.db import get_conn
        row = get_conn().execute(
            "SELECT value FROM app_settings WHERE key = 'reranking_enabled'"
        ).fetchone()
        if row:
            return row["value"] == "1"
    except Exception:
        pass
    return True  # enabled by default


_logo_cache: dict[str, bool] = {}

def _is_logo(url: str) -> bool:
    """Filter out logo-like banner images (wide and short)."""
    if url in _logo_cache:
        return _logo_cache[url]
    # Try to check actual image dimensions from help-files
    try:
        from pathlib import Path
        from PIL import Image
        # URL like /help-files/BBAS/Anag_Clienti/img_003.webp
        rel = url.lstrip("/")
        candidates = [
            Path(__file__).parent.parent.parent / rel,  # dev
            Path("/app") / rel,                          # prod
        ]
        for p in candidates:
            if p.exists():
                with Image.open(p) as img:
                    w, h = img.size
                result = w > 300 and h < 120
                _logo_cache[url] = result
                return result
    except Exception:
        pass
    # Fallback: known logo pattern
    result = "img_003" in url
    _logo_cache[url] = result
    return result


def build_context(docs: list[dict]) -> str:
    """Build a context string from retrieved documents."""
    import re
    parts = []
    screenshots = []
    for i, doc in enumerate(docs, 1):
        source = doc["source_file"]
        title = doc["title"] or "Senza titolo"
        content = doc["content"]
        parts.append(f"--- Documento {i}: {title} (file: {source}) ---\n{content}")
        # Collect screenshots (skip logo)
        for m in re.finditer(r'\[Screenshot:\s*(.+?)\s*\|\s*(.+?)\s*\]', content):
            if not _is_logo(m.group(2)):
                screenshots.append(f"- ![{m.group(1)}]({m.group(2)}) (da Documento {i})")
    ctx = "\n\n".join(parts)
    if screenshots:
        ctx += "\n\n--- SCREENSHOT DISPONIBILI (usa la sintassi markdown esatta per includerli) ---\n"
        ctx += "\n".join(screenshots[:6])
    return ctx


def _get_model(deep: bool = False) -> tuple[str, str | None]:
    """Get model name and optional reasoning_effort from app_settings.

    Returns (api_model_id, reasoning_effort_or_None).
    The config key (e.g. "openai/gpt-oss-120b:high") may differ from the
    actual API model id ("openai/gpt-oss-120b").
    """
    default = "llama-3.1-8b-instant"
    config_key = default
    try:
        from app.db import get_conn
        if deep:
            row = get_conn().execute("SELECT value FROM app_settings WHERE key = 'groq_deep_model'").fetchone()
            if row and row["value"] and row["value"] in ALLOWED_MODELS:
                config_key = row["value"]
                info = ALLOWED_MODELS[config_key]
                return info.get("model_id", config_key), info.get("reasoning_effort")
        row = get_conn().execute("SELECT value FROM app_settings WHERE key = 'groq_model'").fetchone()
        if row and row["value"] and row["value"] in ALLOWED_MODELS:
            config_key = row["value"]
            info = ALLOWED_MODELS[config_key]
            return info.get("model_id", config_key), info.get("reasoning_effort")
    except Exception:
        pass
    return default, None


def _calculate_cost(prompt_tokens: int, completion_tokens: int, config_key: str,
                    cached_tokens: int = 0) -> float:
    """Calculate cost based on the model's pricing from ALLOWED_MODELS.

    Cached tokens get a 50% discount on input price (Groq prompt caching).
    """
    model_info = ALLOWED_MODELS.get(config_key, ALLOWED_MODELS["llama-3.1-8b-instant"])
    input_price = model_info["input_price"] / 1_000_000
    non_cached = prompt_tokens - cached_tokens
    input_cost = (non_cached * input_price) + (cached_tokens * input_price * 0.5)
    output_cost = completion_tokens * model_info["output_price"] / 1_000_000
    return input_cost + output_cost


async def ask_stream(
    question: str,
    history: list[dict] | None = None,
    deep: bool = False,
    topic_filter: str | None = None,
) -> AsyncIterator[tuple[str, list[dict], dict | None]]:
    """Retrieve context, call Groq, and yield (token, sources, usage) tuples.

    - First yield includes the sources list; subsequent yields have empty sources.
    - Final yield includes usage dict with token counts and cost.
    - deep=True doubles the context budget for more thorough answers.
    """
    if _client is None:
        yield "Errore: servizio non configurato.", [], None
        return

    import re as _re

    docs, rerank_usage = await retrieve_with_budget(question, deep=deep, topic_filter=topic_filter)
    sources = [{"title": d["title"], "source_file": d["source_file"]} for d in docs]

    # Extract screenshots from retrieved docs (avoids a second retrieval call, skip logo)
    screenshots = []
    for doc in docs[:5]:
        for m in _re.finditer(r'\[Screenshot:\s*(.+?)\s*\|\s*(.+?)\s*\]', doc["content"]):
            if not _is_logo(m.group(2)):
                screenshots.append({"desc": m.group(1), "url": m.group(2)})
            if len(screenshots) >= 3:
                break
        if len(screenshots) >= 3:
            break

    context = build_context(docs)

    # System prompt is always identical (cache-friendly prefix for Groq prompt caching)
    prompt = get_system_prompt()

    messages = [{"role": "system", "content": prompt}]
    if history:
        for msg in history:
            messages.append(msg)

    user_message = (
        f"Contesto documentale:\n\n{context}\n\n---\n\nDomanda dell'utente: {question}"
    )
    if deep:
        addendum = get_deep_addendum()
        user_message += f"\n\n---\n\n{addendum}"
    messages.append({"role": "user", "content": user_message})

    usage_data = None

    try:
        model_id, reasoning_effort = _get_model(deep=deep)
        # Find the config key for cost calculation
        config_key = next(
            (k for k, v in ALLOWED_MODELS.items()
             if v.get("model_id", k) == model_id
             and v.get("reasoning_effort") == reasoning_effort),
            model_id,
        )

        print(f"[ask_stream] model={model_id} effort={reasoning_effort} deep={deep} config_key={config_key} msgs={len(messages)}", flush=True)

        create_kwargs: dict = dict(
            model=model_id,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            temperature=0.2,
            max_tokens=2048,
        )
        if reasoning_effort:
            create_kwargs["reasoning_effort"] = reasoning_effort

        import time as _time
        import asyncio as _aio
        _t0 = _time.monotonic()
        try:
            stream = await _aio.wait_for(
                _client.chat.completions.create(**create_kwargs),
                timeout=30.0,
            )
        except _aio.TimeoutError:
            print(f"[ask_stream] Groq API timeout after 30s", flush=True)
            yield "Errore: timeout nella connessione al modello. Riprova.", [], None
            return
        print(f"[ask_stream] Groq stream opened in {_time.monotonic()-_t0:.1f}s", flush=True)

        first = True
        async for chunk in stream:
            # Capture usage from the final chunk
            if hasattr(chunk, "usage") and chunk.usage is not None:
                # Extract cached_tokens from prompt_tokens_details (Groq prompt caching)
                cached = 0
                details = getattr(chunk.usage, "prompt_tokens_details", None)
                if details:
                    cached = getattr(details, "cached_tokens", 0) or 0
                usage_data = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "cached_tokens": cached,
                    "cost_usd": _calculate_cost(
                        chunk.usage.prompt_tokens,
                        chunk.usage.completion_tokens,
                        config_key,
                        cached_tokens=cached,
                    ),
                    "model": model_id,
                }

            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                if first:
                    print(f"[ask_stream] First token at {_time.monotonic()-_t0:.1f}s", flush=True)
                    yield delta.content, sources, {"screenshots": screenshots}
                    first = False
                else:
                    yield delta.content, [], None

        # Merge rerank usage into final usage data
        if usage_data and rerank_usage:
            usage_data.update(rerank_usage)
            usage_data["cost_usd"] += rerank_usage.get("rerank_cost_usd", 0)

        # Final yield with usage data
        print(f"[ask_stream] Stream complete at {_time.monotonic()-_t0:.1f}s usage={usage_data}", flush=True)
        yield "", [], usage_data

    except Exception as e:
        import traceback
        print(f"[ask_stream] ERROR: {e}", flush=True)
        traceback.print_exc()
        yield f"Errore nella generazione della risposta: {e}", [], None
