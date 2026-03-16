"""Retrieval + Groq LLM streaming for Q&A."""

from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from app.config import settings
from app.search.fts import SearchIndex

SYSTEM_PROMPT = """\
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

Passaggi numerati in grassetto. Ogni passo = una azione concreta:

**1.** Descrizione dell'azione da compiere

**2.** Descrizione della seconda azione

> 💡 **Suggerimento:** consiglio contestuale utile

**3.** Descrizione della terza azione

Se la procedura coinvolge una maschera con campi, inserisci subito dopo una tabella:

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

**Ritmo visivo:**
- Paragrafi brevi: max 2-3 righe, poi a capo
- Separatori `---` tra le sezioni principali (non tra ogni paragrafo)
- Alternare blocchi densi (tabelle, procedure) con blocchi ariosi (intro, note)
- Mai muri di testo: se una spiegazione supera 4 righe, spezzala con un elenco

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

## Aree documentate
Base & Anagrafiche, Magazzino, Vendite, Acquisti, Contabilità, Cespiti, \
e la struttura completa del database OS1 (958 tabelle).

Se non trovi la risposta nel contesto, dillo e suggerisci termini alternativi da cercare.

## Suggerimenti di follow-up
Alla fine di ogni risposta, aggiungi una sezione:
### Per saperne di più
- Domanda suggerita 1
- Domanda suggerita 2
- Domanda suggerita 3

Le domande devono essere specifiche, correlate al tema, e utili per approfondire."""

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


def retrieve_with_budget(question: str, deep: bool = False) -> list[dict]:
    """Search FTS5 and return docs up to the word budget."""
    if _index is None:
        return []
    max_words = _get_context_budget(deep)
    candidates = _index.search(question, limit=50)
    selected = []
    word_count = 0
    for doc in candidates:
        doc_words = len(doc["content"].split())
        if word_count + doc_words > max_words and selected:
            break
        selected.append(doc)
        word_count += doc_words
    return selected


def build_context(docs: list[dict]) -> str:
    """Build a context string from retrieved documents."""
    parts = []
    for i, doc in enumerate(docs, 1):
        source = doc["source_file"]
        title = doc["title"] or "Senza titolo"
        content = doc["content"]
        parts.append(f"--- Documento {i}: {title} (file: {source}) ---\n{content}")
    return "\n\n".join(parts)


def _get_model(deep: bool = False) -> str:
    """Get model name from app_settings. Uses deep_model for Approfondisci."""
    try:
        from app.db import get_conn
        if deep:
            row = get_conn().execute("SELECT value FROM app_settings WHERE key = 'groq_deep_model'").fetchone()
            if row and row["value"]:
                return row["value"]
        row = get_conn().execute("SELECT value FROM app_settings WHERE key = 'groq_model'").fetchone()
        if row and row["value"]:
            return row["value"]
    except Exception:
        pass
    return "llama-3.1-8b-instant"


def _calculate_cost(prompt_tokens: int, completion_tokens: int, deep: bool = False) -> float:
    if deep:
        return (prompt_tokens * settings.groq_deep_input_price / 1_000_000) + \
               (completion_tokens * settings.groq_deep_output_price / 1_000_000)
    return (prompt_tokens * settings.groq_input_price / 1_000_000) + \
           (completion_tokens * settings.groq_output_price / 1_000_000)


async def ask_stream(
    question: str,
    history: list[dict] | None = None,
    deep: bool = False,
) -> AsyncIterator[tuple[str, list[dict], dict | None]]:
    """Retrieve context, call Groq, and yield (token, sources, usage) tuples.

    - First yield includes the sources list; subsequent yields have empty sources.
    - Final yield includes usage dict with token counts and cost.
    - deep=True doubles the context budget for more thorough answers.
    """
    if _client is None:
        yield "Errore: servizio non configurato.", [], None
        return

    docs = retrieve_with_budget(question, deep=deep)
    sources = [{"title": d["title"], "source_file": d["source_file"]} for d in docs]
    context = build_context(docs)

    prompt = SYSTEM_PROMPT
    if deep:
        prompt += "\n\n## MODALITÀ APPROFONDIMENTO\n" \
            "Stai rispondendo in modalità approfondita. Hai a disposizione più contesto documentale.\n" \
            "- Sii ESAUSTIVO: elenca TUTTI gli elementi, campi, tabelle pertinenti, non solo i principali.\n" \
            "- Fornisci dettagli tecnici completi: nomi esatti di tabelle DB, campi, relazioni.\n" \
            "- Usa tabelle Markdown per strutturare elenchi lunghi.\n" \
            "- Se il contesto include molti documenti, sintetizzali tutti, non solo i primi.\n" \
            "- Non tralasciare informazioni: l'utente ha chiesto esplicitamente di approfondire."

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
        stream = await _client.chat.completions.create(
            model=_get_model(deep=deep),
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
                }

            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                if first:
                    yield delta.content, sources, None
                    first = False
                else:
                    yield delta.content, [], None

        # Final yield with usage data
        yield "", [], usage_data

    except Exception as e:
        yield f"Errore nella generazione della risposta: {e}", [], None
