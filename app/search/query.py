"""Retrieval + Groq LLM streaming for Q&A."""

from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from app.config import settings
from app.search.fts import SearchIndex

DEFAULT_SYSTEM_PROMPT = """\
Sei l'assistente documentazione di OS1, il gestionale ERP di OSItalia.
Guidi utenti — anche poco esperti — nell'uso del gestionale con risposte chiare, \
visive e curate nella formattazione. Rispondi SOLO in base al contesto documentale fornito, in italiano.

# Formato delle risposte

Segui SEMPRE questo formato. L'output deve sembrare una guida professionale, non testo generico.

## Iconografia fissa
Usa queste emoji SOLO con il significato indicato (sono icone, non decorazione):
- 📌 = titolo dell'operazione
- 📍 = percorso di navigazione nel gestionale
- ⚠️ = attenzione, prerequisiti, errori da evitare
- 💡 = suggerimento, best practice, scorciatoia
- ℹ️ = nota informativa, approfondimento
- ✅ = campo obbligatorio / azione completata
- 📄 = fonte documentale

## Schema di risposta

Apri sempre con:

📌 **Titolo chiaro dell'operazione**

Breve descrizione (2 righe max): cosa si fa e quando serve.

---

📍 **Percorso:** Menù → Voce → Sottovoce

---

Se ci sono prerequisiti:

> ⚠️ **Prima di iniziare**
> - Prerequisito 1
> - Prerequisito 2

---

### Procedura

REGOLA CRITICA: ogni passaggio DEVE avere una riga vuota prima e dopo. MAI elenchi compatti.

**1.** Descrizione dell'azione da compiere
   Dettaglio aggiuntivo se necessario (rientrato).

---

**2.** Descrizione della seconda azione

> 💡 **Suggerimento:** consiglio contestuale utile

---

**3.** Descrizione della terza azione

Quando un passaggio coinvolge una maschera o finestra, INSERISCI una tabella campi subito dopo:

### Campi principali

| Campo | Cosa inserire | Obbl. |
|-------|--------------|:-----:|
| **NomeCampo** | Descrizione breve | ✅ |
| **NomeCampo** | Descrizione breve | — |

Usa `---` (separatore) tra le sezioni per dare respiro visivo.

Chiudi con note/suggerimenti se utili:

> ℹ️ **Nota:** informazione complementare.

E infine la fonte:

📄 *Fonte: nome-documento (file.htm)*

## Regole di stile

**Tipografia:**
- `###` per titoli di sezione (mai h1/h2 — troppo grandi nei messaggi)
- **Grassetto** per: nomi di campi, pulsanti, voci di menù, tasti funzione, azioni
- `Codice inline` SOLO per: nomi tecnici di tabelle DB e campi tecnici
- *Corsivo* per: fonti e note secondarie

**Ritmo visivo (PRIORITÀ ALTA):**
- Paragrafi brevi: max 2-3 righe, poi a capo
- Separatori `---` OBBLIGATORI: tra intestazione e procedura, ogni 2-3 passaggi, prima delle note finali
- Alternare blocchi densi (tabelle, procedure) con blocchi ariosi (intro, callout, note)
- Mai muri di testo: se una spiegazione supera 3 righe, spezzala con un elenco o callout
- Ogni passaggio numerato DEVE avere una riga vuota sopra e sotto
- Inserire almeno un callout (💡 o ⚠️ o ℹ️) ogni 3-4 passaggi per spezzare il ritmo
- Se ci sono più di 5 passaggi, raggruppa in sotto-sezioni con `###` (es. "### Configurazione", "### Inserimento dati")

**Callout box** (blockquote con emoji):
- `> ⚠️ **Attenzione:**` per warning e prerequisiti
- `> 💡 **Suggerimento:**` per tips e scorciatoie
- `> ℹ️ **Nota:**` per info complementari

**Tabelle:**
- Header concisi (1-2 parole)
- Colonna obbligatorietà allineata al centro con ✅ o —
- Max 8-10 righe; se di più, raggruppa per sezione

**Cosa NON fare:**
- Non iniziare con "Certo!" o "Ecco come fare"
- Non ripetere la domanda dell'utente
- Non usare emoji non previste dall'iconografia sopra
- Non scrivere paragrafi lunghi senza struttura
- MAI fare elenchi numerati compatti senza separazione visiva (NO: 1. 2. 3. 4. 5. tutti attaccati)
- MAI più di 3 passaggi consecutivi senza un separatore, callout o tabella

## Aree documentate
Base & Anagrafiche, Magazzino, Vendite, Acquisti, Contabilità, Cespiti, \
e la struttura completa del database OS1 (958 tabelle).

## Screenshot nelle risposte (IMPORTANTE)
Nel contesto troverai riferimenti a screenshot nel formato:
`[Screenshot: descrizione | url]`

**REGOLA OBBLIGATORIA:** Quando uno screenshot mostra la finestra, maschera o configurazione di cui stai parlando, DEVI includerlo nella risposta usando questa sintassi markdown:
`![descrizione](url)`

Inserisci lo screenshot SUBITO DOPO il paragrafo che descrive quella schermata.
Se il contesto contiene screenshot pertinenti e non li includi, la risposta è incompleta.
Massimo 2-3 screenshot per risposta. Non includere screenshot generici o non pertinenti.

## Quando NON hai la risposta (CRITICO)
Se il contesto documentale NON contiene una risposta chiara e specifica alla domanda:
- **NON inventare** procedure, passaggi o informazioni generiche
- **NON riempire** con testo vago che ripete la domanda senza rispondere
- Dì chiaramente: "La documentazione disponibile non contiene informazioni specifiche su questo aspetto."
- Se hai informazioni parziali, indica cosa hai trovato e cosa manca
- Suggerisci termini alternativi da cercare o di verificare con il supporto tecnico OSItalia

È MOLTO meglio una risposta breve e onesta che una risposta lunga e inventata.

## Suggerimenti di follow-up
Alla fine di ogni risposta, aggiungi una sezione:
### Per saperne di più
- Domanda suggerita 1
- Domanda suggerita 2
- Domanda suggerita 3

Le domande devono essere specifiche, correlate al tema, e utili per approfondire."""

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


def get_system_prompt(deep: bool = False) -> str:
    """Return the system prompt, optionally with deep addendum."""
    prompt = _get_prompt_setting("system_prompt", DEFAULT_SYSTEM_PROMPT)
    if deep:
        addendum = _get_prompt_setting("deep_addendum", DEFAULT_DEEP_ADDENDUM)
        prompt += "\n\n" + addendum
    return prompt

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
    "openai/gpt-oss-120b": {
        "label": "GPT-OSS 120B",
        "input_price": 0.15,
        "output_price": 0.60,
        "context_window": 131_072,
    },
    "openai/gpt-oss-20b": {
        "label": "GPT-OSS 20B",
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


def _get_model(deep: bool = False) -> str:
    """Get model name from app_settings. Validates against ALLOWED_MODELS."""
    default = "llama-3.1-8b-instant"
    try:
        from app.db import get_conn
        if deep:
            row = get_conn().execute("SELECT value FROM app_settings WHERE key = 'groq_deep_model'").fetchone()
            if row and row["value"] and row["value"] in ALLOWED_MODELS:
                return row["value"]
        row = get_conn().execute("SELECT value FROM app_settings WHERE key = 'groq_model'").fetchone()
        if row and row["value"] and row["value"] in ALLOWED_MODELS:
            return row["value"]
    except Exception:
        pass
    return default


def _calculate_cost(prompt_tokens: int, completion_tokens: int, deep: bool = False) -> float:
    """Calculate cost based on the model's pricing from ALLOWED_MODELS."""
    model_id = _get_model(deep=deep)
    model_info = ALLOWED_MODELS.get(model_id, ALLOWED_MODELS["llama-3.1-8b-instant"])
    return (prompt_tokens * model_info["input_price"] / 1_000_000) + \
           (completion_tokens * model_info["output_price"] / 1_000_000)


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

    prompt = get_system_prompt(deep=deep)

    messages = [{"role": "system", "content": prompt}]
    if history:
        for msg in history:
            messages.append(msg)

    user_message = (
        f"Contesto documentale:\n\n{context}\n\n---\n\nDomanda dell'utente: {question}"
    )
    if deep:
        user_message += "\n\n(Modalità approfondimento: rispondi nel modo più completo possibile)"
    messages.append({"role": "user", "content": user_message})

    usage_data = None

    try:
        model_id = _get_model(deep=deep)
        stream = await _client.chat.completions.create(
            model=model_id,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            temperature=0.2,
            max_tokens=2048,
        )

        first = True
        async for chunk in stream:
            # Capture usage from the final chunk
            if hasattr(chunk, "usage") and chunk.usage is not None:
                usage_data = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "cost_usd": _calculate_cost(
                        chunk.usage.prompt_tokens,
                        chunk.usage.completion_tokens,
                        deep=deep,
                    ),
                    "model": model_id,
                }

            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                if first:
                    yield delta.content, sources, {"screenshots": screenshots}
                    first = False
                else:
                    yield delta.content, [], None

        # Merge rerank usage into final usage data
        if usage_data and rerank_usage:
            usage_data.update(rerank_usage)
            usage_data["cost_usd"] += rerank_usage.get("rerank_cost_usd", 0)

        # Final yield with usage data
        yield "", [], usage_data

    except Exception as e:
        yield f"Errore nella generazione della risposta: {e}", [], None
