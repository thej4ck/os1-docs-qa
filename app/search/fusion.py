"""Reciprocal Rank Fusion of lexical (BM25) and dense (semantic) result lists.

RRF combines rankings without needing comparable raw scores: each list
contributes weight 1/(k + rank). Robust default k=60 (Cormack et al. 2009).
Per-list weights let the caller bias toward lexical for symbol/identifier
queries (see app.search.signals.adaptive_weights).
"""

from __future__ import annotations


def rrf_fuse(
    bm25_ids: list[int],
    dense_ids: list[int],
    k: int = 60,
    limit: int = 50,
    w_lexical: float = 1.0,
    w_semantic: float = 1.0,
) -> list[int]:
    """Fuse two ranked id lists into one ordered id list (best first).

    bm25_ids / dense_ids: doc ids ordered best→worst from each retriever.
    Returns up to `limit` unique doc ids, highest fused score first.
    """
    scores: dict[int, float] = {}

    for rank, doc_id in enumerate(bm25_ids):
        scores[doc_id] = scores.get(doc_id, 0.0) + w_lexical / (k + rank + 1)

    for rank, doc_id in enumerate(dense_ids):
        scores[doc_id] = scores.get(doc_id, 0.0) + w_semantic / (k + rank + 1)

    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [doc_id for doc_id, _ in ordered[:limit]]
