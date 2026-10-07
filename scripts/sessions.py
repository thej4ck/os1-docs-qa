"""Production session review: read-only pull, quality report, retrieval replay.

    python scripts/sessions.py pull [--since 2026-10-01] [--base https://os1.ai.scao.it]
    python scripts/sessions.py report [--selftest]
    python scripts/sessions.py replay [--flagged-only]

pull   — GET /admin/api/sessions with `Authorization: Bearer $EXPORT_TOKEN` (.env) →
         outputs/sessions/sessions.json (full overwrite). Users arrive pseudonymized
         (server-side HMAC), only the email domain is kept.
report — offline metrics + flags per question→answer pair → outputs/sessions/review.md
         (the flagged pairs, readable, for manual/Claude review).
replay — re-runs the current retrieval (query.trace_retrieve, zero LLM, zero cost) on the
         logged questions and compares the selected docs with what prod used →
         outputs/sessions/replay.json.

Disambiguation shown → the conversation has no messages (the clarifying question is not
persisted). Stream error → a user message with no assistant reply (`no_answer`).
"""

import argparse
import asyncio
import json
import sys
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.search.query import _CITE_MARKER_RE  # noqa: E402

OUT = ROOT / "outputs" / "sessions"
DATA = OUT / "sessions.json"
REFUSAL = "la documentazione disponibile non copre questo aspetto"  # CORE prompt, query.py
FLAG_ORDER = ["negative_feedback", "no_answer", "truncated", "refusal", "bad_citation", "no_sources",
              "uncited", "repeat", "short_answer"]


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def flags_for(q: dict, a: dict | None, prev_q: str | None) -> list[str]:
    f = ["repeat"] if prev_q == _norm(q["content"]) else []
    if a is None:
        return ["no_answer"] + f
    text, src = a["content"], a["sources"]
    cites = {int(n) for n in _CITE_MARKER_RE.findall(text)}
    if a.get("finish_reason") == "length":  # persisted from build 109
        f.append("truncated")
    if a["feedback"] and a["feedback"]["rating"] < 0:
        f.append("negative_feedback")
    if REFUSAL in _norm(text):
        f.append("refusal")
    if not src:
        f.append("no_sources")
    elif not cites:
        f.append("uncited")
    if any(n < 1 or n > len(src) for n in cites):
        f.append("bad_citation")
    if len(text.split()) < 40 and "refusal" not in f:
        f.append("short_answer")
    return sorted(f, key=FLAG_ORDER.index)


def pairs(data: dict):
    """Yield (conv, question, answer|None, flags): each user message + the assistant reply right after it."""
    for c in data["conversations"]:
        msgs, prev_q = c["messages"], None
        for i, m in enumerate(msgs):
            if m["role"] != "user":
                continue
            nxt = msgs[i + 1] if i + 1 < len(msgs) else None
            a = nxt if nxt and nxt["role"] == "assistant" else None
            yield c, m, a, flags_for(m, a, prev_q)
            prev_q = _norm(m["content"])


def _load() -> dict:
    if not DATA.exists():
        sys.exit(f"{DATA} missing — run `python scripts/sessions.py pull` first")
    return json.loads(DATA.read_text(encoding="utf-8"))


def cmd_pull(args) -> None:
    if not settings.export_token:
        sys.exit("EXPORT_TOKEN missing in .env (same value as the Railway variable)")
    url = f"{args.base.rstrip('/')}/admin/api/sessions" + (f"?since={args.since}" if args.since else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {settings.export_token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    OUT.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    n_msg = sum(len(c["messages"]) for c in data["conversations"])
    print(f"{len(data['conversations'])} conversations, {n_msg} messages → {DATA}  config={data['config']}")


def analyze(data: dict) -> tuple[list[str], list[tuple]]:
    """Return (summary lines, flagged pairs)."""
    convs = data["conversations"]
    rows = list(pairs(data))
    answers = [a for _, _, a, _ in rows if a]
    fb = [a["feedback"] for a in answers if a["feedback"]]
    flag_count = Counter(f for *_, fl in rows for f in fl)
    cost = sum(a["cost_usd"] or 0 for a in answers)
    dates = sorted(c["created_at"][:10] for c in convs if c["created_at"])
    pct = lambda n, d: f"{n} ({100 * n / d:.0f}%)" if d else str(n)  # noqa: E731

    out = [
        f"Periodo: {dates[0] if dates else '-'} → {dates[-1] if dates else '-'}   export {data.get('exported_at', '?')}",
        f"Config prod: { {k: v for k, v in data.get('config', {}).items() if k != 'prompt_overrides'} }"
        f"   prompt sovrascritti da admin: {sorted(data.get('config', {}).get('prompt_overrides', {})) or 'nessuno'}",
        f"Conversazioni: {len(convs)}  (vuote/disambiguazione abbandonata: "
        f"{sum(1 for c in convs if not c['messages'])})   utenti: {len({c['user'] for c in convs})}",
        f"Domande: {len(rows)}   risposte: {len(answers)}   costo totale ${cost:.3f}"
        f"  (medio ${cost / len(answers) if answers else 0:.4f})",
        "Per dominio: " + ", ".join(f"{k} {v}" for k, v in Counter(c["domain"] for c, *_ in rows).most_common(10)),
        "Per esperto: " + ", ".join(f"{k or 'standard'} {v}" for k, v in Counter(a["agent"] for a in answers).most_common()),
        "Per modello: " + ", ".join(f"{k} {v}" for k, v in Counter(a["model"] for a in answers).most_common()),
        "Per giorno (ultimi 14): " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(
            q["created_at"][:10] for _, q, _, _ in rows if q["created_at"]).items())[-14:]),
        f"Feedback: {len(fb)} su {len(answers)} risposte  👍 {sum(f['rating'] > 0 for f in fb)}  👎 "
        f"{sum(f['rating'] < 0 for f in fb)}   categorie 👎: "
        + ", ".join(f"{k} {v}" for k, v in Counter(f["category"] for f in fb if f["rating"] < 0).most_common()),
        "Flag: " + ", ".join(f"{k} {pct(flag_count[k], len(rows))}" for k in FLAG_ORDER if flag_count[k]),
        f"Domande senza flag: {pct(sum(1 for *_, fl in rows if not fl), len(rows))}",
    ]
    flagged = sorted((r for r in rows if r[3]), key=lambda r: FLAG_ORDER.index(r[3][0]))
    return out, flagged


def _review_md(summary: list[str], flagged: list[tuple]) -> str:
    md = ["# Review sessioni di produzione", "", *[f"- {s}" for s in summary], ""]
    for c, q, a, fl in flagged:
        md += [f"## {' · '.join(fl)} — `{c['id'][:8]}#{q['id']}` {c['domain']} {q['created_at']}", "",
               f"**Domanda:** {q['content']}", ""]
        if a:
            md.append(f"*esperto {a['agent'] or 'standard'} · {a['model']} · {len(a['content'].split())} parole*")
            if a["feedback"]:
                f = a["feedback"]
                md.append(f"**Feedback:** {'👍' if f['rating'] > 0 else '👎'} {f['category'] or ''} — {f['comment'] or ''}")
            md += ["", "**Risposta:**", "", "> " + a["content"].replace("\n", "\n> "), "",
                   "**Fonti:** " + "; ".join(f"[D{i}] {s.get('title')} ({s.get('source_file')})"
                                             for i, s in enumerate(a["sources"], 1))]
        md.append("")
    return "\n".join(md)


def cmd_report(args) -> None:
    if args.selftest:
        return selftest()
    summary, flagged = analyze(_load())
    print("\n".join(summary))
    (OUT / "review.md").write_text(_review_md(summary, flagged), encoding="utf-8")
    print(f"\n{len(flagged)} coppie flaggate → {OUT / 'review.md'}")


async def _replay(args) -> None:
    from app.search import query as q
    from app.search.embeddings import EmbeddingIndex
    from app.search.fts import SearchIndex

    data = _load()
    preset = data.get("config", {}).get("context_preset", "normal")
    # Same budget as prod: without an initialized app.db the module silently falls back to "normal".
    q._get_context_budget = lambda deep=False: q.CONTEXT_PRESETS.get(preset, q.CONTEXT_PRESETS["normal"])
    idx = SearchIndex(settings.db_path, read_only=True)
    q.init(idx)
    if settings.hybrid_enabled:
        q.init_embeddings(EmbeddingIndex(idx, settings.static_model_path))
    print(f"Replay preset={preset}. Limiti: niente rerank LLM, deep/topic ignoti, search.db attuale "
          "(forse più nuovo di quello usato in prod), follow-up senza history.\n")

    results, seen = [], set()
    for c, qm, a, fl in pairs(data):
        if args.flagged_only and not fl:
            continue
        key = _norm(qm["content"])
        if key in seen:
            continue
        seen.add(key)
        tr = await q.trace_retrieve(qm["content"])
        path = lambda p: (p or "").replace("\\", "/").lower()  # noqa: E731 — source_file mixes separators
        now = [path(r["source"]) for r in tr.get("selected", [])]
        prod = [path(s.get("source_file")) for s in (a["sources"] if a else [])]
        overlap = len(set(now) & set(prod)) / len(prod) if prod else None
        results.append({"ref": f"{c['id'][:8]}#{qm['id']}", "q": qm["content"], "flags": fl,
                        "overlap": overlap, "prod": prod,
                        "now_top": [f"{r['title']} ({r['source']})" for r in tr.get("selected", [])[:8]]})
        ov = "  -  " if overlap is None else f"{overlap:4.0%}"
        print(f"{ov}  n={len(now):2}  {','.join(fl) or 'ok':28.28}  {qm['content'][:80]}")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "replay.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    low = [r for r in results if r["overlap"] is not None and r["overlap"] < 0.5]
    print(f"\n{len(results)} domande rigiocate, {len(low)} con overlap < 50% → {OUT / 'replay.json'}")


def selftest() -> None:
    def msg(i, role, content, sources=(), rating=None):
        return {"id": i, "role": role, "content": content, "sources": list(sources), "model": "m",
                "agent": None, "cost_usd": 0.001, "created_at": "2026-10-01T10:00:00Z",
                "feedback": {"rating": rating, "category": "wrong", "comment": ""} if rating else None}
    src = [{"title": "A", "source_file": "a.md"}]
    long = " parola" * 50
    data = {"conversations": [{"id": "c1", "user": "u", "domain": "x.it", "created_at": "2026-10-01",
                               "messages": [
        msg(1, "user", "q1"), msg(2, "assistant", "ok [D1]" + long, src),               # clean
        msg(3, "user", "q2"), msg(4, "assistant", "La documentazione disponibile non copre questo aspetto."),
        msg(5, "user", "q3"), msg(6, "assistant", "vedi [D3]" + long, src, rating=-1),  # bad cite + 👎
        msg(7, "user", "q4"), msg(8, "assistant", "niente citazioni" + long, src),       # uncited
        msg(9, "user", "q5"), msg(10, "user", "q5"),                                     # no_answer, repeat
    ]}]}
    got = [fl for *_, fl in pairs(data)]
    assert got == [[], ["refusal", "no_sources"], ["negative_feedback", "bad_citation"], ["uncited"],
                   ["no_answer"], ["no_answer", "repeat"]], got
    summary, flagged = analyze(data)
    assert len(flagged) == 5 and flagged[0][3][0] == "negative_feedback", flagged
    print("selftest ok")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows console is cp1252: →, 👍 would crash print
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull")
    p.add_argument("--since")
    p.add_argument("--base", default="https://os1.ai.scao.it")
    sub.add_parser("report").add_argument("--selftest", action="store_true")
    sub.add_parser("replay").add_argument("--flagged-only", action="store_true")
    a = ap.parse_args()
    if a.cmd == "replay":
        asyncio.run(_replay(a))
    else:
        {"pull": cmd_pull, "report": cmd_report}[a.cmd](a)
