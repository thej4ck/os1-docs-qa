"""LLM-based reranking for search results."""

import json

from openai import AsyncOpenAI

RERANK_MODEL = "llama-3.1-8b-instant"
RERANK_MAX_CANDIDATES = 20

RERANK_PROMPT = """\
Sei un sistema di ranking per documentazione ERP OS1.
Data una domanda utente e una lista di documenti candidati, \
assegna a ciascuno un punteggio di rilevanza da 0 a 10.

Criteri:
- 10 = risponde direttamente alla domanda
- 7-9 = molto pertinente, contiene informazioni utili
- 4-6 = parzialmente pertinente
- 1-3 = tangenzialmente correlato
- 0 = completamente irrilevante

Rispondi SOLO con un JSON object: {"scores": [{"id": <num>, "score": <num>}, ...]}
Nessun altro testo. Solo il JSON."""


async def rerank(
    question: str,
    candidates: list[dict],
    client: AsyncOpenAI,
    min_score: float = 4.0,
) -> tuple[list[dict], dict | None]:
    """Rerank candidates using LLM.

    Returns (filtered+reordered list, usage_info dict or None).
    usage_info contains rerank_tokens, rerank_cost_usd, rerank_model.
    """
    if not candidates:
        return [], None

    to_rank = candidates[:RERANK_MAX_CANDIDATES]

    # Build candidate summaries (truncated to save tokens)
    candidate_texts = []
    for i, doc in enumerate(to_rank):
        title = doc.get("title", "")
        snippet = doc["content"][:300].replace("\n", " ")
        candidate_texts.append(f"[{i}] Titolo: {title}\nContenuto: {snippet}...")

    user_msg = (
        f"Domanda: {question}\n\n"
        f"Documenti candidati:\n\n" + "\n\n".join(candidate_texts)
    )

    try:
        response = await client.chat.completions.create(
            model=RERANK_MODEL,
            messages=[
                {"role": "system", "content": RERANK_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
            max_tokens=1024,
        )

        # Extract usage info
        usage_info = None
        if response.usage:
            total_tokens = (response.usage.prompt_tokens or 0) + (response.usage.completion_tokens or 0)
            # llama-3.1-8b-instant pricing
            cost = (
                (response.usage.prompt_tokens or 0) * 0.05 / 1_000_000
                + (response.usage.completion_tokens or 0) * 0.08 / 1_000_000
            )
            usage_info = {
                "rerank_tokens": total_tokens,
                "rerank_cost_usd": cost,
                "rerank_model": RERANK_MODEL,
            }

        raw = response.choices[0].message.content.strip()
        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        scores = json.loads(raw)

        # Handle {"scores": [...]} or direct [...]
        if isinstance(scores, dict):
            scores = scores.get("scores", scores.get("results", []))

        # Build score map
        score_map = {}
        for item in scores:
            idx = item.get("id", item.get("index", -1))
            score = item.get("score", 0)
            if 0 <= idx < len(to_rank):
                score_map[idx] = score

        # Filter by min_score and sort descending
        reranked = []
        for i, doc in enumerate(to_rank):
            score = score_map.get(i, 0)
            if score >= min_score:
                doc["rerank_score"] = score
                reranked.append(doc)

        reranked.sort(key=lambda d: d.get("rerank_score", 0), reverse=True)

        # Safety fallback: if everything was filtered out, return top 5 originals
        if not reranked:
            return to_rank[:5], usage_info

        return reranked, usage_info

    except Exception as e:
        print(f"[rerank] failed: {e}", flush=True)
        return candidates, None
