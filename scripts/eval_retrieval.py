"""Retrieval regression check: does the context sent to the LLM contain the right doc?

Runs the real pipeline (query.trace_retrieve: BM25 ∪ semantic ∪ image → RRF →
signals → budget trim) over scripts/eval_queries.json. No LLM calls, no cost.
A case hits when any `expect` substring matches (case-insensitive) the title or
source_file of a doc in the final selected context.

    python scripts/eval_retrieval.py [--cases scripts/eval_queries.json]

Run before and after a retrieval change and diff the output.
"""

import argparse
import asyncio
import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.search import query as q  # noqa: E402
from app.search.embeddings import EmbeddingIndex  # noqa: E402
from app.search.fts import SearchIndex  # noqa: E402


def _norm(s: str) -> str:
    # Accent-insensitive: "funzionalità" matches "funzionalita" (source paths are mixed).
    s = unicodedata.normalize("NFKD", s.replace("\\", "/").lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def first_hit(rows: list[dict], expect: list[str]) -> int | None:
    keys = [_norm(e) for e in expect]
    for r in rows:
        hay = _norm(f"{r['title']} {r['source']}")
        if any(k in hay for k in keys):
            return r["rank"]
    return None


async def main(cases_path: str) -> int:
    idx = SearchIndex(settings.db_path, read_only=True)
    q.init(idx)
    if settings.hybrid_enabled:
        q.init_embeddings(EmbeddingIndex(idx, settings.static_model_path))
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))

    hits, rr = 0, 0.0
    for c in cases:
        tr = await q.trace_retrieve(c["q"], topic_filter=c.get("topic"))
        rank = first_hit(tr.get("selected", []), c["expect"])
        hits += rank is not None
        rr += 1 / rank if rank else 0
        print(f"{'OK ' if rank else 'MISS'} rank={str(rank):4} n={tr.get('selected_count', 0):2}  {c['q']}")
    n = len(cases)
    print(f"\nhit@context {hits}/{n}   MRR {rr / n:.3f}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default=str(Path(__file__).with_name("eval_queries.json")))
    sys.exit(asyncio.run(main(ap.parse_args().cases)))
