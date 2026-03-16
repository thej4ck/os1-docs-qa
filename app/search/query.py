"""Retrieval + Groq LLM streaming for Q&A."""

from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from app.config import settings
from app.search.fts import SearchIndex

SYSTEM_PROMPT = """\
Sei l'assistente documentazione OS1, il gestionale ERP di OSItalia.
Rispondi alle domande basandoti ESCLUSIVAMENTE sul contesto documentale fornito.
Rispondi in italiano. Se non trovi la risposta nel contesto, dillo chiaramente
e suggerisci termini alternativi o aree correlate che l'utente potrebbe cercare.
Quando citi tabelle o campi, usa i nomi esatti.
Indica da quale documento hai tratto l'informazione.

Aree documentate: Base & Anagrafiche (clienti, fornitori, articoli, piano dei conti, causali),
Magazzino (movimenti, giacenze, scorte, inventario, valorizzazione),
Vendite (offerte, ordini clienti, bolle, fatture),
Contabilità (prima nota, registri IVA, partite aperte, scadenze, cespiti),
e la struttura completa del database OS1 (958 tabelle)."""

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


def retrieve(question: str, limit: int = 10) -> list[dict]:
    """Search the FTS5 index and return matching documents."""
    if _index is None:
        return []
    return _index.search(question, limit=limit)


def build_context(docs: list[dict]) -> str:
    """Build a context string from retrieved documents."""
    parts = []
    for i, doc in enumerate(docs, 1):
        source = doc["source_file"]
        title = doc["title"] or "Senza titolo"
        content = doc["content"]
        # Truncate very long docs to ~3000 chars to fit in context
        if len(content) > 3000:
            content = content[:3000] + "\n[... troncato]"
        parts.append(f"--- Documento {i}: {title} (file: {source}) ---\n{content}")
    return "\n\n".join(parts)


async def ask_stream(
    question: str,
    history: list[dict] | None = None,
) -> AsyncIterator[tuple[str, list[dict]]]:
    """Retrieve context, call Groq, and yield (token, sources) tuples.

    The first yield includes the sources list; subsequent yields have empty sources.
    """
    if _client is None:
        yield "Errore: servizio non configurato.", []
        return

    # Retrieve relevant docs
    docs = retrieve(question)
    sources = [{"title": d["title"], "source_file": d["source_file"]} for d in docs]
    context = build_context(docs)

    # Build messages
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Add conversation history (last N exchanges)
    if history:
        for msg in history:
            messages.append(msg)

    # Add the current question with context
    user_message = (
        f"Contesto documentale:\n\n{context}\n\n---\n\nDomanda dell'utente: {question}"
    )
    messages.append({"role": "user", "content": user_message})

    try:
        stream = await _client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=messages,
            stream=True,
            temperature=0.2,
            max_tokens=2048,
        )

        first = True
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                if first:
                    yield delta.content, sources
                    first = False
                else:
                    yield delta.content, []
    except Exception as e:
        yield f"Errore nella generazione della risposta: {e}", []
