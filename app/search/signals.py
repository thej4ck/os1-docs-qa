"""Semble-style deterministic signal reranking, adapted to OS1 ERP docs.

Runs on CPU in microseconds. Operates on the RRF-fused candidate list and
nudges it with domain signals:

- adaptive weighting: symbol/identifier queries (table names, CamelCase,
  ALLCAPS module codes) lean lexical; natural-language Italian stays balanced
- definition boost: a table-def / schema chunk that DEFINES the queried term
  outranks chunks that merely mention it
- identifier stems: Italian stemming so "fatturazione" matches "fattura"
- file/module coherence: when several candidates share a file/module, that
  cluster is boosted (broad relevance)
- noise penalty: very short chunks, and schema-census chunks on operational
  questions, are down-ranked
"""

from __future__ import annotations

import re

# Reuse the FTS stopword list for natural-language detection / stemming.
from app.search.fts import ITALIAN_STOPWORDS

_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÿ0-9_]+")
_CAMEL_RE = re.compile(r"[A-Z][a-z]+|[A-Z]+(?![a-z])|[a-z]+|[0-9]+")

# Lazily-built Italian Snowball stemmer (optional dependency).
_stemmer = None
_stemmer_tried = False


def _get_stemmer():
    global _stemmer, _stemmer_tried
    if _stemmer_tried:
        return _stemmer
    _stemmer_tried = True
    try:
        import snowballstemmer

        _stemmer = snowballstemmer.stemmer("italian")
    except Exception:
        _stemmer = None
    return _stemmer


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


def _looks_technical(tok: str) -> bool:
    """True if a token looks like a DB table / field / module code."""
    if len(tok) < 3:
        return False
    if tok.isupper() and tok.isalpha():            # ALLCAPS module code: BBAS
        return True
    if "_" in tok and any(c.isalpha() for c in tok):  # snake_case
        return True
    if re.search(r"[a-z][A-Z]", tok):              # CamelCase: MovMagazzino
        return True
    if re.search(r"[A-Za-z]", tok) and re.search(r"\d", tok):  # mixed alnum
        return True
    return False


def split_identifier(tok: str) -> list[str]:
    """Break CamelCase / snake_case into lowercased parts."""
    parts = []
    for chunk in tok.split("_"):
        parts.extend(m.group(0).lower() for m in _CAMEL_RE.finditer(chunk))
    return [p for p in parts if p]


def is_technical_query(query: str) -> bool:
    return any(_looks_technical(t) for t in _tokens(query))


def adaptive_weights(query: str) -> tuple[float, float]:
    """Return (w_lexical, w_semantic) for RRF.

    Symbol-like queries reward exact lexical matches; natural-language Italian
    questions lean slightly semantic to absorb vocabulary mismatch.
    """
    toks = _tokens(query)
    if not toks:
        return 1.0, 1.0
    tech = sum(1 for t in toks if _looks_technical(t))
    ratio = tech / len(toks)
    if ratio >= 0.34:
        return 1.6, 0.7      # clearly technical → favor BM25
    if tech:
        return 1.2, 1.0      # mixed
    return 0.9, 1.15         # natural language → favor semantic


def _stem_words(words: list[str], stemmer) -> set[str]:
    """Drop short/stopword tokens, then stem (or pass through if no stemmer)."""
    ws = [w for w in words if len(w) >= 3 and w not in ITALIAN_STOPWORDS]
    return set(stemmer.stemWords(ws)) if stemmer else set(ws)


def _content_stems(text: str, stemmer) -> set[str]:
    """Stem set of free text (title/body). Stopwords dropped."""
    return _stem_words([w.lower() for w in _tokens(text)], stemmer)


def identifier_stems(query: str, stemmer=None) -> set[str]:
    """Content-word stems of the query (stopwords dropped, identifiers split)."""
    if stemmer is None:
        stemmer = _get_stemmer()
    raw: list[str] = []
    for t in _tokens(query):
        if _looks_technical(t):
            raw.extend(split_identifier(t))
        else:
            raw.append(t.lower())
    return _stem_words(raw, stemmer)


_OPERATIONAL_HINTS = {
    "come", "creare", "crea", "inserire", "modificare", "stampare",
    "configurare", "procedura", "passaggi", "fare", "gestire", "emettere",
}


def rescore(query: str, docs: list[dict]) -> list[dict]:
    """Re-order RRF-fused docs with domain signals. Stable for ties.

    `docs` are dict rows (id, source_file, module, doc_type, title, content)
    already in fused order (best first). Returns a new ordered list.
    """
    if not docs:
        return docs

    stemmer = _get_stemmer()
    q_stems = identifier_stems(query, stemmer)
    q_tokens = {t.lower() for t in _tokens(query)}
    technical = is_technical_query(query)
    q_lower = query.lower()
    operational = any(h in q_lower for h in _OPERATIONAL_HINTS)

    n = len(docs)
    # Module / file frequency among candidates → coherence boost.
    mod_freq: dict[str, int] = {}
    file_freq: dict[str, int] = {}
    for d in docs:
        m = (d.get("module") or "").strip()
        if m:
            mod_freq[m] = mod_freq.get(m, 0) + 1
        f = (d.get("source_file") or "").split("#")[0]
        if f:
            file_freq[f] = file_freq.get(f, 0) + 1

    scored = []
    for pos, d in enumerate(docs):
        # Base: preserve fused order (monotonic decreasing in [0,1]).
        score = (n - pos) / n
        delta = 0.0

        title = (d.get("title") or "")
        content = d.get("content") or ""
        doc_type = (d.get("doc_type") or "")

        # ── Definition boost ──
        if doc_type in ("table-def", "schema"):
            title_toks = {t.lower() for t in _tokens(title)}
            if q_tokens & title_toks:
                delta += 0.35 if technical else 0.18
            # explicit "**Tabella:** `Name`" definition that matches a query token
            m = re.search(r"\*\*Tabella:\*\*\s*`(\w+)`", content)
            if m and m.group(1).lower() in q_tokens:
                delta += 0.40

        # ── Identifier-stem overlap (title weighs more) ──
        if q_stems:
            t_stems = _content_stems(title, stemmer)
            c_stems = _content_stems(content[:1500], stemmer)
            delta += 0.15 * len(q_stems & t_stems)
            delta += 0.04 * len(q_stems & c_stems)

        # ── File / module coherence ──
        f = (d.get("source_file") or "").split("#")[0]
        if f and file_freq.get(f, 0) > 1:
            delta += 0.05 * min(file_freq[f] - 1, 4)
        m = (d.get("module") or "").strip()
        if m and mod_freq.get(m, 0) > 1:
            delta += 0.03 * min(mod_freq[m] - 1, 4)

        # ── Noise penalties ──
        wc = len(content.split())
        if wc < 40:
            delta -= 0.30
        elif wc < 90:
            delta -= 0.12
        if doc_type == "schema" and operational and not technical:
            delta -= 0.25  # census tables aren't operational answers

        scored.append((score + delta, pos, d))

    scored.sort(key=lambda x: (-x[0], x[1]))  # stable on original fused order
    return [d for _, _, d in scored]
