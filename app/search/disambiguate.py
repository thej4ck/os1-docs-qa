"""Disambiguation logic for ambiguous queries.

Detects when BM25 results span multiple topic areas and generates
clarifying options via a lightweight LLM call.
"""

from collections import Counter

from app.search.fts import ITALIAN_STOPWORDS

# ── Topic extraction from document metadata ──

HELP_MODULE_LABELS = {
    "bbas": "Anagrafiche di base",
    "sbas": "Anagrafiche di base",
    "bcge": "Contabilità generale",
    "bipn": "Prima nota",
    "sven": "Vendite - DDT e fatture",
    "sved": "Vendite - Documenti",
    "sorc": "Vendite - Ordini clienti",
    "sofc": "Vendite - Offerte clienti",
    "sacq": "Acquisti - DDT e fatture",
    "sorf": "Acquisti - Ordini fornitori",
    "soff": "Acquisti - Offerte fornitori",
    "bmag": "Magazzino",
    "slis": "Listini",
    "scom": "Provvigioni e agenti",
    "bces": "Cespiti",
    "bcan": "Contabilità analitica",
    "sprb": "Produzione",
    "sprl": "Produzione - Lanci",
    "sclv": "Conto lavoro",
    "spar": "Parcellazione",
    "bpcf": "Portafoglio effetti",
    "sraf": "Raggruppamento fatture",
    "sfvb": "Fatturazione c/vendita",
    "smat": "Materiali",
    "bcsp": "Contabilità semplificata",
    "brda": "Ritenute d'acconto",
    "babi": "Analisi di bilancio",
    "sstp": "Stampe",
    "index": "Indice generale",
    "Ambiente": "Ambiente e configurazione",
}

TABLE_MODULE_LABELS = {
    "acquisti": "Acquisti",
    "vendite": "Vendite",
    "base-anagrafiche": "Anagrafiche di base",
    "contabilita": "Contabilità",
    "magazzino": "Magazzino",
    "contolavoro": "Conto lavoro",
    "produzione": "Produzione",
    "cespiti": "Cespiti",
}

# Thresholds
MAX_QUERY_WORDS = 3
MIN_TOPIC_AREAS = 3
MAX_OPTIONS = 5
DEFAULT_DOMINANCE_THRESHOLD = 70  # percent


def extract_topic(doc: dict) -> str:
    """Derive a human-readable business area from a document's metadata."""
    source = doc.get("source_file", "").replace("\\", "/")
    doc_type = doc.get("doc_type", "")
    module = doc.get("module", "")

    if doc_type == "scheda-operativa":
        # Path: schede-operative/AreaName/SubArea/File.pdf#sezione
        parts = source.split("/")
        # Find "schede-operative" segment and take next one
        for i, p in enumerate(parts):
            if p == "schede-operative" and i + 1 < len(parts):
                return parts[i + 1]
        return module or "Altro"

    if doc_type == "help":
        return HELP_MODULE_LABELS.get(module, module or "Altro")

    if doc_type == "table-def":
        return TABLE_MODULE_LABELS.get(module, module or "Altro")

    if doc_type == "functional":
        stem = source.split("/")[-1].replace(".md", "").replace("#", " ").split()[0]
        return stem.replace("-", " ").title()

    return module or "Altro"


def _get_dominance_threshold() -> int:
    """Read threshold from admin settings, default 70."""
    try:
        from app.db import get_conn
        row = get_conn().execute(
            "SELECT value FROM app_settings WHERE key = 'disambig_dominance_threshold'"
        ).fetchone()
        if row and row["value"]:
            return int(row["value"])
    except Exception:
        pass
    return DEFAULT_DOMINANCE_THRESHOLD


def analyze_ambiguity(
    query: str,
    candidates: list[dict],
    is_first_message: bool,
) -> dict | None:
    """Check if results are ambiguous across topic areas.

    Returns {areas: [{label, topic, count, sample_title}]} or None.
    """
    if not is_first_message:
        return None

    # Check query length (after stopword removal)
    tokens = [
        t.lower() for t in query.strip().split()
        if t.lower().strip("?!.,;:") not in ITALIAN_STOPWORDS
    ]
    if len(tokens) > MAX_QUERY_WORDS:
        return None

    if len(candidates) < MIN_TOPIC_AREAS:
        return None

    # Count topics across top results
    top_n = candidates[:20]
    topic_counts: Counter[str] = Counter()
    topic_samples: dict[str, str] = {}
    for doc in top_n:
        topic = extract_topic(doc)
        topic_counts[topic] += 1
        if topic not in topic_samples:
            topic_samples[topic] = doc.get("title", "")

    if len(topic_counts) < MIN_TOPIC_AREAS:
        return None

    # Check dominance
    threshold = _get_dominance_threshold() / 100.0
    top_count = topic_counts.most_common(1)[0][1]
    if top_count / len(top_n) >= threshold:
        return None

    # Build areas list
    areas = []
    for topic, count in topic_counts.most_common(MAX_OPTIONS):
        areas.append({
            "label": topic,
            "topic": topic,
            "count": count,
            "sample_title": topic_samples.get(topic, ""),
        })

    return {"areas": areas}


async def ask_disambiguation(
    query: str,
    areas: list[dict],
    client,
) -> dict | None:
    """Call fast LLM to generate a natural disambiguation question.

    Returns {question: str, options: [{label, topic, keywords}]} or None on failure.
    """
    areas_desc = "\n".join(
        f"- {a['label']} ({a['count']} risultati, es: \"{a['sample_title']}\")"
        for a in areas
    )

    prompt = f"""L'utente ha chiesto: "{query}"

Ho trovato risultati in queste aree della documentazione OS1:
{areas_desc}

Genera una BREVE domanda di chiarimento (1 riga) e le opzioni tra cui scegliere.
Rispondi SOLO con JSON valido, senza markdown:
{{"question": "domanda breve e chiara in italiano", "options": [{{"label": "etichetta leggibile", "topic": "nome area esatto dalla lista sopra", "keywords": "parole chiave aggiuntive per filtrare"}}]}}

Regole:
- La domanda deve CHIEDERE CHIARIMENTO (es. "Stai cercando informazioni su una bolla di vendita o di acquisto?"), NON riformulare la domanda dell'utente
- Max 4 opzioni, ordinate per rilevanza
- "label" deve essere specifico e comprensibile per l'utente (es. "Bolla di vendita (DDT clienti)" non "Ciclo attivo")
- "topic" deve corrispondere ESATTAMENTE a uno dei nomi area sopra
- "keywords" sono termini extra per affinare la ricerca (opzionale, stringa vuota se non servono)
- La domanda deve essere naturale, breve, in italiano"""

    try:
        import asyncio
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=300,
            ),
            timeout=5.0,
        )
        import json
        text = response.choices[0].message.content.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        result = json.loads(text)
        if "question" in result and "options" in result:
            return result
    except Exception:
        pass

    # Fallback: generate from areas directly
    return {
        "question": "Ho trovato risultati in diverse aree. Di quale ti interessa?",
        "options": [
            {"label": a["label"], "topic": a["topic"], "keywords": ""}
            for a in areas[:4]
        ],
    }
