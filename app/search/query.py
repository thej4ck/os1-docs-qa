"""Retrieval + Groq LLM streaming for Q&A."""

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from math import log

from openai import AsyncOpenAI

from app.config import settings
from app.search.fts import SearchIndex, ITALIAN_STOPWORDS

# ── Prompt a due livelli ──
# CORE = solo grounding (condiviso, invariante). NON deve contenere regole di
# formato/lunghezza/struttura: quelle stanno nel blocco "stile" di ogni esperto,
# così il prompt base non può mai contraddire l'assistant prompt.
CORE_SYSTEM_PROMPT = """\
Sei l'assistente documentazione di OS1, il gestionale ERP di OSItalia. \
Rispondi SOLO in base al contesto documentale fornito, in italiano.

# Regole non negoziabili (valide per ogni modalità)

- **NON inventare**: se il contesto non contiene la risposta, dillo. Meglio breve e onesto che lungo e inventato.
- **Cita la fonte**: alla fine della frase che usa un documento, scrivi il suo codice tra parentesi quadre ASCII, es. `[D2]`. Usa SOLO i codici `[Dn]` presenti nel contesto. NON scrivere una riga "Fonte:", né percorsi o nomi file.
- Non iniziare con "Certo!" e non ripetere la domanda dell'utente.

# Screenshot

NON inserire immagini nel testo: gli screenshot pertinenti vengono mostrati automaticamente sotto la risposta. Ignora i marcatori `[Screenshot: …]` del contesto.

# Se non hai la risposta

Dì: "La documentazione disponibile non copre questo aspetto." \
Indica cosa hai trovato di parziale e suggerisci termini alternativi o di contattare il supporto OSItalia."""

# STILE STANDARD = formato dell'assistente generico (default, nessun esperto scelto).
STYLE_STANDARD_DEFAULT = """\
# Stile di risposta

1. **Risposta diretta PRIMA di tutto**: rispondi alla domanda in 1-3 righe, POI dettaglia.
2. **Solo emoji codificate**: 📌 titolo, 📍 percorso, ⚠️ warning, 💡 tip, ℹ️ nota, ✅ obbligatorio, 📄 fonte.

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

### Per saperne di più
- 3 domande suggerite specifiche e correlate

# Stile

- `###` per sezioni (mai h1/h2). **Grassetto** per campi/pulsanti/azioni. `Codice` solo per nomi tecnici DB.
- Paragrafi max 3 righe, poi spezza con elenco o callout. Mai muri di testo.
- Non usare emoji fuori dalla lista codificata."""

# Back-compat: prompt completo = CORE + stile standard.
DEFAULT_SYSTEM_PROMPT = CORE_SYSTEM_PROMPT + "\n\n" + STYLE_STANDARD_DEFAULT

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
_emb = None  # app.search.embeddings.EmbeddingIndex | None


def init(index: SearchIndex):
    global _index, _client
    _index = index
    _client = AsyncOpenAI(
        api_key=settings.groq_api_key,
        base_url="https://api.groq.com/openai/v1",
    )


def init_embeddings(emb) -> None:
    """Register the semantic index (set by main.py lifespan). Optional."""
    global _emb
    _emb = emb


def embeddings_ready() -> bool:
    return bool(settings.hybrid_enabled and _emb is not None and _emb.ready)


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
    "aggressive": 30_000,
}

# Budget in PAROLE (non token). "Approfondisci" moltiplica il budget base per
# _DEEP_BUDGET_MULTIPLIER, poi _MAX_CONTEXT_WORDS fa da tetto di sicurezza: anche
# dopo il moltiplicatore il contesto non deve mai saturare la context window del
# modello (128K token). 60K parole ≈ 95-108K token, lasciando margine per system
# prompt, history e completamento.
_DEEP_BUDGET_MULTIPLIER = 2.5
_MAX_CONTEXT_WORDS = 60_000


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
    if deep:
        return min(int(budget * _DEEP_BUDGET_MULTIPLIER), _MAX_CONTEXT_WORDS)
    return budget


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


def _matches_topic(doc: dict, topic_filter: str) -> bool:
    src = (doc.get("source_file") or "").replace("\\", "/")
    return f"/{topic_filter}/" in src or (doc.get("module") or "") == topic_filter


def _snap(docs: list[dict], scores: dict | None = None, n: int = 20) -> list[dict]:
    """Compact stage snapshot for the debug trace."""
    out = []
    for rank, d in enumerate(docs[:n], 1):
        row = {
            "rank": rank,
            "id": d.get("id"),
            "doc_type": d.get("doc_type"),
            "title": (d.get("title") or "")[:70],
            "source": (d.get("source_file") or "").replace("\\", "/"),
        }
        if scores is not None and d.get("id") in scores:
            row["score"] = round(scores[d["id"]], 4)
        out.append(row)
    return out


async def _safe_dense(question: str, limit: int):
    """Semantic search off the event loop; returns results or the exception."""
    try:
        return await asyncio.to_thread(_emb.search, question, limit)
    except Exception as e:  # graceful degradation to BM25-only
        return e


async def _hybrid_candidates(
    question: str, topic_filter: str | None, trace: dict | None = None,
) -> list[dict]:
    """BM25 ∪ semantic → RRF → signal rerank. Falls back to BM25-only.

    If `trace` (a dict) is passed, every stage is recorded into it.
    BM25 and semantic legs run concurrently off the event loop.
    """
    from app.search import signals
    from app.search.fusion import rrf_fuse
    from app.search.expand import expand_terms

    # Espansione terminologica deterministica (usata solo nel ramo OR-fallback
    # di BM25). La leg dense resta sulla query originale: assorbe già le parafrasi.
    expansion = expand_terms(question)
    if trace is not None and expansion:
        trace["expanded_terms"] = expansion

    hybrid = embeddings_ready()
    if hybrid:
        bm25_ids, dense = await asyncio.gather(
            asyncio.to_thread(_index.search_ids, question, 80, topic_filter, expansion),
            _safe_dense(question, 80),
        )
    else:
        bm25_ids = await asyncio.to_thread(_index.search_ids, question, 80, topic_filter, expansion)
        dense = None

    if topic_filter and len(bm25_ids) < 3:
        bm25_ids = await asyncio.to_thread(_index.search_ids, question, 80, None, expansion)
        topic_filter = None  # widened fallback

    def _bm25_only(reason: str) -> list[dict]:
        if trace is not None:
            trace["mode"] = reason
        return _index.get_documents_by_ids(bm25_ids)

    if trace is not None:
        trace["topic_filter"] = topic_filter
        trace["bm25"] = _snap(_index.get_documents_by_ids(bm25_ids[:20]))

    if not hybrid:
        return _bm25_only("bm25-only (semantic not ready)")
    if isinstance(dense, Exception):
        print(f"[hybrid] semantic search failed, BM25-only: {dense}", flush=True)
        return _bm25_only(f"bm25-only (semantic error: {dense})")

    dense_ids = [doc_id for doc_id, _ in dense]
    w_lex, w_sem = signals.adaptive_weights(question)
    fused_ids = rrf_fuse(
        bm25_ids, dense_ids, k=60, limit=50,
        w_lexical=w_lex, w_semantic=w_sem,
    )

    if trace is not None:
        trace["mode"] = "hybrid"
        trace["technical_query"] = signals.is_technical_query(question)
        trace["weights"] = {"lexical": w_lex, "semantic": w_sem}
        trace["query_stems"] = sorted(signals.identifier_stems(question))
        dense_score = {doc_id: sc for doc_id, sc in dense}
        trace["semantic"] = _snap(
            _index.get_documents_by_ids(dense_ids[:20]), dense_score
        )

    if not fused_ids:
        return _bm25_only("bm25-only (empty fusion)")

    docs = _index.get_documents_by_ids(fused_ids)
    if trace is not None:
        trace["fused"] = _snap(docs)
    if topic_filter:
        on_topic = [d for d in docs if _matches_topic(d, topic_filter)]
        if len(on_topic) >= 3:
            docs = on_topic

    reranked = signals.rescore(question, docs)
    if trace is not None:
        trace["after_signals"] = _snap(reranked)
    return reranked


def _trim_to_budget(candidates: list[dict], max_words: int) -> tuple[list[dict], int]:
    """Greedily take docs until the word budget is exceeded (always keep ≥1)."""
    selected: list[dict] = []
    word_count = 0
    for doc in candidates:
        doc_words = len(doc["content"].split())
        if word_count + doc_words > max_words and selected:
            break
        selected.append(doc)
        word_count += doc_words
    return selected, word_count


async def _run_pipeline(
    question: str, deep: bool, topic_filter: str | None, trace: dict | None = None,
) -> tuple[list[dict], dict | None]:
    """Shared retrieve path: candidates → optional LLM rerank → budget trim.

    Used by both retrieve_with_budget (prod) and trace_retrieve (debug); the
    optional `trace` records each stage without forking the pipeline.
    """
    if _index is None:
        return [], None
    max_words = _get_context_budget(deep)
    candidates = await _hybrid_candidates(question, topic_filter, trace=trace)

    rerank_usage = None
    rerank_applied = False
    would_rerank = bool(_client and len(candidates) > 5 and _is_reranking_enabled())
    if would_rerank and trace is None:
        # Paid Groq call — never run it from the debug trace (untracked spend).
        from app.search.rerank import rerank
        candidates, rerank_usage = await rerank(question, candidates[:20], _client)
        rerank_applied = True
    if trace is not None:
        trace["llm_rerank_enabled"] = would_rerank
        trace["llm_rerank_applied"] = False  # trace never executes the paid rerank
        trace["llm_rerank_note"] = (
            "skipped in trace (would run in prod)" if would_rerank else "disabled"
        )

    selected, word_count = _trim_to_budget(candidates, max_words)
    if trace is not None:
        snaps = _snap(selected, n=50)
        trace["selected"] = [
            {**row, "words": len(selected[i]["content"].split())}
            for i, row in enumerate(snaps)
        ]
        trace["selected_count"] = len(selected)
        trace["selected_words"] = word_count
    return selected, rerank_usage


async def retrieve_with_budget(
    question: str, deep: bool = False, topic_filter: str | None = None,
) -> tuple[list[dict], dict | None]:
    """Hybrid retrieve (BM25 ∪ semantic, RRF, signal rerank), optional LLM
    rerank, then trim to the word budget.

    Returns (selected_docs, rerank_usage_or_None).
    """
    return await _run_pipeline(question, deep, topic_filter)


async def trace_retrieve(
    question: str, deep: bool = False, topic_filter: str | None = None,
) -> dict:
    """Run the full retrieve pipeline and return a structured trace (debug)."""
    trace: dict = {
        "query": question,
        "deep": deep,
        "hybrid_enabled": settings.hybrid_enabled,
        "embeddings_ready": embeddings_ready(),
        "rerank_llm_enabled": _is_reranking_enabled(),
        "context_budget_words": _get_context_budget(deep),
    }
    if _index is None:
        trace["error"] = "index not initialized"
        return trace
    await _run_pipeline(question, deep, topic_filter, trace=trace)
    return trace


def _is_reranking_enabled() -> bool:
    """Check admin setting for the optional LLM rerank.

    Default OFF: hybrid retrieval (BM25 + semantic + signal rerank) is already
    strong and free. The LLM rerank stays available as an admin toggle for
    hard cases.
    """
    try:
        from app.db import get_conn
        row = get_conn().execute(
            "SELECT value FROM app_settings WHERE key = 'reranking_enabled'"
        ).fetchone()
        if row:
            return row["value"] == "1"
    except Exception:
        pass
    return False  # disabled by default


def _is_reasoning_suppressed() -> bool:
    """Admin toggle per ri-attivare include_reasoning=False (comportamento legacy).

    Default OFF: sopprimere il canale reasoning su gpt-oss (formato harmony) fa
    chiudere a Groq il canale finale in anticipo → finish_reason=stop a metà
    risposta. Tenendolo OFF il reasoning arriva in un campo separato e viene
    scartato (leggiamo solo delta.content). Toggle per rollback senza deploy.

    TODO: rimuovere questo toggle e il ramo include_reasoning=False una volta
    confermata stabile la build 65 in prod (1-2 release).
    """
    from app.models.settings import get_setting
    return get_setting("suppress_reasoning", "0") == "1"


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
        title = doc["title"] or "Senza titolo"
        content = doc["content"]
        # [Dn] = codice opaco del documento (token-cheap, no path esposto all'LLM).
        # L'ordine corrisponde a `sources` → il frontend traduce [Dn] -> sources[n-1].
        parts.append(f"[D{i}] {title}\n{content}")
        # Collect screenshots (skip logo)
        for m in re.finditer(r'\[Screenshot:\s*(.+?)\s*\|\s*(.+?)\s*\]', content):
            if not _is_logo(m.group(2)):
                screenshots.append(f"- ![{m.group(1)}]({m.group(2)}) (da [D{i}])")
    ctx = "\n\n".join(parts)
    if screenshots:
        ctx += "\n\n--- SCREENSHOT DISPONIBILI (usa la sintassi markdown esatta per includerli) ---\n"
        ctx += "\n".join(screenshots[:6])
    return ctx


# ── Citation remap (fix misattribuzione [Dn] dell'LLM) ──
# Con molti chunk in contesto (modalità deep) il modello aggancia il fatto giusto
# ma può sbagliare l'indice [Dn], puntando a un documento che non regge la frase.
# Correzione deterministica: overlap lessicale IDF-pesato (unigrammi + bigrammi)
# tra la frase citata e il contenuto dei chunk. I bigrammi sono il segnale chiave:
# il bigramma "quantità secondaria" distingue il doc giusto da uno che parla solo
# di "quantità" generica. L'IDF abbatte i termini comuni ("magazzino"); il peso
# extra sui bigrammi premia la presenza della frase esatta.
_CITE_MARKER_RE = re.compile(r"[\[【]\s*D\s*(\d+)\s*[\]】]")
# Confini di "frase/cella" per isolare l'affermazione attorno al marcatore.
_CLAIM_BOUND_RE = re.compile(r"[.\n!?;:|]")
# Soglia: rimappa solo se il chunk citato ha < RATIO del supporto-bigrammi del
# best (validato su dati reali: bug ~0.23-0.29, citazione corretta ~1.0).
_REMAP_RATIO = 0.40


def _features(text: str) -> tuple[set[str], set[tuple[str, str]]]:
    """(unigrammi, bigrammi) significativi: token ≥4 char, no stopword."""
    toks = [
        w for w in re.split(r"\W+", text.lower(), flags=re.UNICODE)
        if len(w) >= 4 and w not in ITALIAN_STOPWORDS
    ]
    return set(toks), set(zip(toks, toks[1:]))


def _claim_around(text: str, pos: int) -> str:
    """Estrae la porzione di testo (frase o cella tabella) attorno a `pos`."""
    left = 0
    for m in _CLAIM_BOUND_RE.finditer(text, 0, pos):
        left = m.end()
    rm = _CLAIM_BOUND_RE.search(text, pos)
    right = rm.start() if rm else len(text)
    return text[left:right]


def remap_citations(text: str, docs: list[dict]) -> tuple[str, dict]:
    """Corregge le citazioni [Dn] mal attribuite dall'LLM.

    Se [Dn] cita un chunk che NON contiene la frase distintiva dell'affermazione
    (es. il bigramma "quantità secondaria") mentre un altro file la contiene,
    riscrive [Dn] → [Dm]. Conservativo: rimappa solo se il best condivide ≥2
    bigrammi e ≥2 unigrammi col claim, sta in un FILE diverso dal citato, e il
    citato ha < _REMAP_RATIO del supporto-bigrammi del best. Così le citazioni
    corrette (anche parafrasate) e gli scambi tra sezioni dello stesso PDF
    restano intatti.

    Ritorna (testo eventualmente riscritto, {n_originale: n_nuovo} per il log).
    """
    if not text or len(docs) < 2:
        return text, {}
    feats = [_features(d.get("content", "")) for d in docs]
    # Fonte base (senza #sezione-N) per ogni doc: il remap si fa solo TRA file
    # diversi. Citare la sezione sbagliata dello stesso PDF è minore (l'utente apre
    # comunque il documento giusto); puntare a un file del tutto diverso è il bug.
    bases = [
        (d.get("source_file") or "").split("#")[0].replace("\\", "/").lower()
        for d in docs
    ]
    n_docs = len(feats)

    dfu: dict[str, int] = {}
    dfb: dict[tuple[str, str], int] = {}
    for uni, bi in feats:
        for x in uni:
            dfu[x] = dfu.get(x, 0) + 1
        for x in bi:
            dfb[x] = dfb.get(x, 0) + 1

    def _idf(df: dict, x) -> float:  # raro → alto
        return log(1.0 + n_docs / (1 + df.get(x, 0)))

    # Decisione guidata dai BIGRAMMI: il doc citato sbagliato è spesso topicamente
    # adiacente (condivide "magazzino", "quantità", "nota") ma NON la frase esatta
    # ("quantità secondaria"). Lo score sui soli bigrammi isola questo segnale;
    # gli unigrammi servono solo come grounding di topic del best.
    def bscore(cb: set, idx: int) -> float:
        return sum(_idf(dfb, x) for x in cb & feats[idx][1])

    def uscore(cu: set, idx: int) -> float:
        return sum(_idf(dfu, x) for x in cu & feats[idx][0])

    # Aggrega il contesto di TUTTE le occorrenze di ogni [Dn]: lo stesso numero
    # può comparire con frase ricca (es. "...quantità secondaria... [D2]") e come
    # codice nudo nella tabella riferimenti (| 2 | ... |, isolato dai '|'). La
    # decisione di remap si prende una volta per numero e si applica a tutte le
    # occorrenze → niente incoerenze tra le citazioni.
    claims: dict[int, tuple[set, set]] = {}
    for m in _CITE_MARKER_RE.finditer(text):
        n = int(m.group(1))
        cu, cb = _features(_claim_around(text, m.start()))
        u, b = claims.get(n, (set(), set()))
        claims[n] = (u | cu, b | cb)

    remap: dict[int, int] = {}
    for n, (cu, cb) in claims.items():
        idx = n - 1
        if not (0 <= idx < n_docs) or len(cu) < 2:
            continue
        sb = [bscore(cb, i) for i in range(n_docs)]
        best = max(range(n_docs), key=lambda i: (sb[i], uscore(cu, i)))
        if best == idx or sb[best] <= 0:
            continue
        # Solo cross-documento: niente churn tra sezioni dello stesso file.
        if bases[best] and bases[best] == bases[idx]:
            continue
        # Evidenza forte sul best: ≥2 bigrammi (frase) e ≥2 unigrammi (topic) in
        # comune col claim → niente remap su segnale debole/casuale.
        if len(cb & feats[best][1]) < 2 or len(cu & feats[best][0]) < 2:
            continue
        if sb[idx] < _REMAP_RATIO * sb[best]:
            remap[n] = best + 1

    if not remap:
        return text, {}

    corrected = _CITE_MARKER_RE.sub(
        lambda m: f"[D{remap[int(m.group(1))]}]"
        if int(m.group(1)) in remap else m.group(0),
        text,
    )
    return corrected, {str(k): v for k, v in remap.items()}


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


def _get_token_limit(key: str, default: int) -> int:
    """Get max token limit from app_settings."""
    try:
        from app.db import get_conn
        row = get_conn().execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row and row["value"]:
            return max(256, int(row["value"]))
    except Exception:
        pass
    return default


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
    agent_id: str | None = None,
    user_role: str | None = None,
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

    # System prompt = CORE (grounding) + STILE (esperto scelto o standard).
    # Il default (agent_id None) usa lo stile standard -> prefisso cache-friendly.
    from app.search.agents import build_system_prompt, is_brief_agent
    prompt = build_system_prompt(agent_id, module=topic_filter, role=user_role)

    messages = [{"role": "system", "content": prompt}]
    if history:
        for msg in history:
            messages.append(msg)

    user_message = (
        f"Contesto documentale:\n\n{context}\n\n---\n\nDomanda dell'utente: {question}"
    )
    # Deep soppresso sugli esperti a risposta breve (es. Virgilio): l'addendum
    # "sii esaustivo" contraddirebbe il cap di lunghezza della persona.
    if deep and not is_brief_agent(agent_id):
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
        )
        if reasoning_effort:
            create_kwargs["reasoning_effort"] = reasoning_effort
            # gpt-oss (harmony): NON sopprimere il reasoning di default — farlo
            # chiude il canale finale in anticipo (finish_reason=stop a metà
            # risposta). Il reasoning arriva in un campo separato e viene scartato
            # (yieldiamo solo delta.content). Soppressione solo se admin opta in.
            if _is_reasoning_suppressed():
                create_kwargs["extra_body"] = {"include_reasoning": False}
            create_kwargs["max_completion_tokens"] = _get_token_limit("max_completion_tokens", 4096)
        else:
            create_kwargs["max_tokens"] = _get_token_limit("max_output_tokens", 2048)

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
        finish_reason = None
        answer_parts: list[str] = []
        async for chunk in stream:
            # Capture usage from the final chunk
            if hasattr(chunk, "usage") and chunk.usage is not None:
                # Extract cached_tokens from prompt_tokens_details (Groq prompt caching)
                cached = 0
                details = getattr(chunk.usage, "prompt_tokens_details", None)
                if details:
                    cached = getattr(details, "cached_tokens", 0) or 0
                # reasoning_tokens: chain-of-thought fatturato DENTRO
                # completion_tokens. Incluso nel log usage per diagnosi (non persistito).
                reasoning = 0
                cdetails = getattr(chunk.usage, "completion_tokens_details", None)
                if cdetails:
                    reasoning = getattr(cdetails, "reasoning_tokens", 0) or 0
                usage_data = {
                    "prompt_tokens": chunk.usage.prompt_tokens,
                    "completion_tokens": chunk.usage.completion_tokens,
                    "reasoning_tokens": reasoning,
                    "cached_tokens": cached,
                    "cost_usd": _calculate_cost(
                        chunk.usage.prompt_tokens,
                        chunk.usage.completion_tokens,
                        config_key,
                        cached_tokens=cached,
                    ),
                    "model": model_id,
                }

            if chunk.choices and chunk.choices[0].finish_reason:
                finish_reason = chunk.choices[0].finish_reason
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                answer_parts.append(delta.content)
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

        # True solo su finish_reason=="length" (cap token raggiunto) → la UI offre
        # "Continua". Raro dopo build 65: gpt-oss ora chiude con finish=stop, non
        # length. Resta come rete di sicurezza per tagli reali da cap. Diagnosi
        # completa in CLAUDE.md → Troubleshooting.
        if usage_data is not None:
            usage_data["truncated"] = (finish_reason == "length")

        # Citation remap: corregge i marcatori [Dn] mal attribuiti dall'LLM
        # confrontando ogni frase citata col contenuto dei chunk recuperati.
        # `corrected_answer` (testo riscritto) viaggia nel meta finale; chat_routes
        # lo usa per salvataggio + evento done, poi lo rimuove da `usage`.
        answer = "".join(answer_parts)
        corrected, cite_remap = remap_citations(answer, docs)
        if usage_data is not None and cite_remap:
            usage_data["citation_remap"] = cite_remap
            print(f"[ask_stream] citation remap applied: {cite_remap}", flush=True)

        # Final yield with usage data (log compatto: corrected_answer escluso)
        print(f"[ask_stream] Stream complete at {_time.monotonic()-_t0:.1f}s finish={finish_reason} usage={usage_data}", flush=True)
        if usage_data is not None and cite_remap and corrected != answer:
            usage_data["corrected_answer"] = corrected
        yield "", [], usage_data

    except Exception as e:
        import traceback
        print(f"[ask_stream] ERROR: {e}", flush=True)
        traceback.print_exc()
        yield f"Errore nella generazione della risposta: {e}", [], None
