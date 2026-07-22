"""One-time VLM enrichment of OS1 documentation images.

Runs LOCALLY pre-push (like distill_model.py) — never on Railway. Describes each
content image once with a VLM (default Claude Sonnet 5 via OpenRouter), embeds the
description with the same model2vec model used for docs, and stores everything in
`search.db` (baked into the image). Production does zero VLM calls: it only reads
the pre-computed rows.

Incremental & idempotent: keyed by url, guarded by the file's sha1. A re-run
describes ONLY new or changed images. Icons/spacers are filtered out for free (no
VLM call). `image_fts` (FTS5 over caption+ocr_text) is rebuilt from the table each
run — cheap, so only the VLM stays incremental.

Usage:
    export OPENROUTER_API_KEY=sk-or-...
    python scripts/describe_images.py                 # full incremental run
    python scripts/describe_images.py --module Ambiente --limit 30   # pilot
    python scripts/describe_images.py --model anthropic/claude-opus-4.8
    python scripts/describe_images.py --force         # re-describe everything
    python scripts/describe_images.py --prune         # drop rows for missing files
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "searchdata" / "search.db"
DEFAULT_MODEL_DIR = ROOT / "searchdata" / "static_model"
HELP_ROOT = ROOT / "help-files"
DEFAULT_VLM = "anthropic/claude-sonnet-5"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Images referenced by docs: schede markers `[Screenshot: cap | url]` in content,
# and `<img src=url>` in html_content.
_MARKER_RE = re.compile(r"\[Screenshot:\s*(.+?)\s*\|\s*(/help-files/.+?)\s*\]")
_IMG_SRC_RE = re.compile(r'<img[^>]+src="(/help-files/[^"]+)"', re.I)

# Pre-filter: names that are chrome/decoration, never content.
_ICON_NAME_RE = re.compile(r"^(b_|os1_ico)", re.I)
_DECO_NAMES = {"sfondo", "loading"}

KIND_ENUM = "screenshot|report|dialog|form|diagram|icon|decoration|other"

PROMPT = (
    "Sei un catalogatore di screenshot del gestionale ERP OS1 (OSItalia), in italiano. "
    "Ti do anche il CONTESTO testuale della documentazione dove appare l'immagine: usalo SOLO "
    "come indizio per nominare moduli/funzioni. Descrivi ciò che VEDI davvero; se il contenuto "
    "visivo differisce dal contesto, fidati dell'immagine. Rispondi SOLO con JSON valido:\n"
    '{"kind":"<' + KIND_ENUM + '>",'
    '"caption":"<descrizione ricca in italiano, 1-2 frasi: cosa mostra e a cosa serve>",'
    '"ocr_text":"<SOLO le etichette principali a schermo: titolo finestra, nomi dei tab, '
    'label dei campi/pulsanti e intestazioni di colonna. MAX ~40 parole. NON trascrivere '
    'righe di tabelle, elenchi di dati, numeri o testo ripetitivo. Vuoto se icona/decorazione>"}'
    "\n\nCONTESTO:\n"
)


# ── Pre-filter (no VLM) ──────────────────────────────────────────────────────

def classify_prefilter(name: str, w: int | None, h: int | None, nbytes: int) -> str | None:
    """Return a `kind` for chrome/decoration images that skip the VLM, else None.

    Pure and cheap — this is the free noise filter (icons, spacers, tiny images).
    """
    stem = Path(name).stem.lower()
    if _ICON_NAME_RE.match(name):           # b_*, os1_ico* → toolbar/chrome icons
        return "icon"
    if stem in _DECO_NAMES:                  # sfondo, loading → decorazioni
        return "decoration"
    if nbytes < 2048:                        # spacer/1x1/tiny → decorazione
        return "decoration"
    if w is not None and w < 200:            # stessa soglia 200px del build
        return "icon"
    return None


def _self_check() -> None:
    assert classify_prefilter("b_Salva.webp", 20, 20, 300) == "icon"
    assert classify_prefilter("sfondo.webp", 1, 1, 100) == "decoration"
    assert classify_prefilter("ambiente_02.webp", 449, 500, 21000) is None
    assert classify_prefilter("img_012.webp", 120, 900, 5000) == "icon"  # narrow → icon
    assert classify_prefilter("tiny.webp", 800, 600, 500) == "decoration"  # <2KB


# ── Inventory ────────────────────────────────────────────────────────────────

def build_inventory(conn: sqlite3.Connection, module: str | None) -> dict[str, dict]:
    """Map every doc-referenced image url → {source_file, doc_type, context}.

    First doc referencing an url wins as its owner. Context = a clean text window
    (title + module + text around the image reference, style/script stripped).
    """
    inv: dict[str, dict] = {}
    rows = conn.execute(
        "SELECT source_file, doc_type, module, title, content, html_content FROM documents"
    ).fetchall()
    for sf, doc_type, mod, title, content, html in rows:
        if module and not (
            (mod or "").lower() == module.lower()
            or f"/{module.lower()}/" in (sf or "").replace("\\", "/").lower()
        ):
            continue
        # schede markers (content) + help img src (html_content)
        found: list[tuple[str, str]] = []  # (url, source_text_for_context)
        for m in _MARKER_RE.finditer(content or ""):
            found.append((m.group(2), content))
        for m in _IMG_SRC_RE.finditer(html or ""):
            found.append((m.group(1), html))
        for url, src in found:
            if url in inv:
                continue
            inv[url] = {
                "source_file": sf,
                "doc_type": doc_type or ("pdf" if "schede-operative" in url else "help"),
                "context": _context_window(title, mod, src, url),
            }
    return inv


def _context_window(title: str, module: str, src: str, url: str) -> str:
    idx = src.find(url)
    if idx < 0:
        idx = src.find(Path(url).name)
    raw = src[max(0, idx - 800):idx + 400] if idx >= 0 else src[:800]
    raw = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", raw)
    txt = re.sub(r"\s+", " ", txt).strip()
    head = f"Titolo doc: {title or '?'} | Modulo: {module or '?'}"
    return (head + "\nSezione: " + txt)[:700]


def _disk_path(url: str) -> Path:
    return ROOT / url.lstrip("/")


# ── VLM (OpenRouter) ─────────────────────────────────────────────────────────

async def describe_one(client, model: str, path: Path, context: str, sem) -> dict | None:
    """One VLM call → {kind, caption, ocr_text}. None on failure (left for retry).

    Rate-limit/timeout handling is delegated to the client's built-in retry
    (max_retries + exponential backoff honoring Retry-After); one extra outer
    try covers transient parse/connection edge cases.
    """
    b64 = base64.b64encode(path.read_bytes()).decode()
    async with sem:
        try:
            r = await client.chat.completions.create(
                model=model,
                temperature=0,
                max_tokens=1024,  # headroom: caption+ocr verbosi troncavano a 500 (finish=length)
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": PROMPT + context},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/webp;base64,{b64}"}},
                ]}],
            )
        except Exception as e:  # noqa: BLE001  — eccezioni API (429/5xx/timeout dopo i retry del client)
            print(f"  ! VLM EXC {path.name}: {repr(e)[:120]}", flush=True)
            return None
    content = r.choices[0].message.content
    parsed = _parse_json(content)
    if parsed is None:  # risposta non-JSON o troncata → logga il perché (non silenzioso)
        fr = r.choices[0].finish_reason
        print(f"  ! parse-fail {path.name} finish={fr} content={(content or '')[:80]!r}", flush=True)
    return parsed


def _parse_json(text: str) -> dict | None:
    # L'estrazione {…} sussume qualunque fence ```json```: prende dal primo { all'ultimo }.
    s = (text or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j < 0:
        return None
    try:
        d = json.loads(s[i:j + 1])
    except json.JSONDecodeError:
        return None
    kind = str(d.get("kind") or "other").strip().lower()
    if kind not in KIND_ENUM.split("|"):
        kind = "other"
    return {"kind": kind, "caption": (d.get("caption") or "").strip(),
            "ocr_text": (d.get("ocr_text") or "").strip()}


# ── DB ───────────────────────────────────────────────────────────────────────

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS image_descriptions (
            url         TEXT PRIMARY KEY,
            source_file TEXT,
            doc_type    TEXT,
            width       INTEGER,
            height      INTEGER,
            kind        TEXT,
            caption     TEXT,
            ocr_text    TEXT,
            vec         BLOB,
            sha1        TEXT,
            model       TEXT,
            built_at    TIMESTAMP
        );
    """)
    conn.commit()


def rebuild_image_fts(conn: sqlite3.Connection) -> int:
    """Rebuild the FTS5 index over described images (cheap, derived from the table)."""
    conn.executescript("""
        DROP TABLE IF EXISTS image_fts;
        CREATE VIRTUAL TABLE image_fts USING fts5(
            url UNINDEXED, source_file UNINDEXED, caption, ocr_text,
            tokenize='unicode61 remove_diacritics 2'
        );
    """)
    conn.execute(
        "INSERT INTO image_fts (url, source_file, caption, ocr_text) "
        "SELECT url, source_file, caption, ocr_text FROM image_descriptions "
        "WHERE vec IS NOT NULL"
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM image_fts").fetchone()[0]


# ── Main ─────────────────────────────────────────────────────────────────────

async def _run_vlm(described, client, model, concurrency):
    sem = asyncio.Semaphore(concurrency)
    tasks = [describe_one(client, model, item["path"], item["meta"]["context"], sem)
             for item in described]
    return await asyncio.gather(*tasks)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--static-model", default=str(DEFAULT_MODEL_DIR))
    ap.add_argument("--model", default=DEFAULT_VLM, help="OpenRouter model id")
    ap.add_argument("--module", default=None, help="Restringi a un modulo (pilota)")
    ap.add_argument("--limit", type=int, default=None, help="Max immagini da descrivere (pilota)")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--force", action="store_true", help="Ri-descrivi anche se sha1 invariato")
    ap.add_argument("--prune", action="store_true", help="Elimina righe per file mancanti")
    ap.add_argument("--dry-run", action="store_true", help="Non chiama il VLM, mostra solo il piano")
    args = ap.parse_args()

    _self_check()

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_schema(conn)

    existing = {r["url"]: r["sha1"]
                for r in conn.execute("SELECT url, sha1 FROM image_descriptions")}

    inv = build_inventory(conn, args.module)
    print(f"Immagini referenziate dai doc: {len(inv)}")

    prefilter_rows: list[tuple] = []   # icons/decorations (no VLM)
    described: list[dict] = []         # need VLM
    missing = skipped = 0

    import io
    from PIL import Image
    for url, meta in inv.items():
        path = _disk_path(url)
        if not path.exists():
            missing += 1
            continue
        # Leggi i byte UNA volta (sha1 + dimensioni). Sha1 prima dello skip così un
        # re-run no-op non apre neppure le immagini invariate.
        try:
            data = path.read_bytes()
        except OSError:
            missing += 1
            continue
        sha1 = hashlib.sha1(data).hexdigest()
        if not args.force and existing.get(url) == sha1:
            skipped += 1
            continue

        nbytes = len(data)
        try:
            with Image.open(io.BytesIO(data)) as im:
                w, h = im.size
        except Exception:
            w = h = None

        kind = classify_prefilter(path.name, w, h, nbytes)
        if kind:  # chrome/decoration → store without VLM, no vec
            prefilter_rows.append(
                (url, meta["source_file"], meta["doc_type"], w, h, kind,
                 "", "", None, sha1, "prefilter", _now()))
        else:
            described.append({"url": url, "path": path, "w": w, "h": h,
                              "sha1": sha1, "meta": meta})

    if args.limit is not None:
        described = described[:args.limit]

    print(f"  skip (sha1 invariato): {skipped} | mancanti su disco: {missing}")
    print(f"  pre-filtro icone/deco (no VLM): {len(prefilter_rows)}")
    print(f"  da descrivere col VLM ({args.model}): {len(described)}")

    if args.dry_run:
        print("DRY-RUN: nessuna chiamata VLM.")
        conn.close()
        return

    # Pre-filter rows first (free, always safe).
    _upsert(conn, prefilter_rows)

    if described:
        import os
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            print("ERRORE: OPENROUTER_API_KEY non impostata (serve per il VLM).")
            sys.exit(1)
        from openai import AsyncOpenAI
        # Built-in retry assorbe i 429/5xx/timeout con backoff esponenziale + Retry-After.
        client = AsyncOpenAI(api_key=key, base_url=OPENROUTER_BASE,
                             max_retries=6, timeout=90.0)
        model2vec = _load_static_model(args.static_model)

        # Salvataggio a BLOCCHI: commit ogni CHUNK → crash-safe (l'incrementale sha1
        # riprende da qui) + osservabile. Un'interruzione perde al più un blocco.
        CHUNK = 150
        t0 = time.monotonic()
        total_ok = 0
        for start in range(0, len(described), CHUNK):
            batch = described[start:start + CHUNK]
            results = asyncio.run(_run_vlm(batch, client, args.model, args.concurrency))
            ok = [(it, res) for it, res in zip(batch, results) if res is not None]
            vecs = _embed(model2vec, [f"{r['caption']} {r['ocr_text']}".strip() for _, r in ok])
            rows = []
            for (it, res), vec in zip(ok, vecs):
                # vec solo per il contenuto: NULL per icone/decorazioni (le esclude ovunque)
                vec_blob = vec.tobytes() if res["kind"] not in ("icon", "decoration") else None
                rows.append((
                    it["url"], it["meta"]["source_file"], it["meta"]["doc_type"],
                    it["w"], it["h"], res["kind"], res["caption"], res["ocr_text"],
                    vec_blob, it["sha1"], args.model, _now()))
            _upsert(conn, rows)  # commit del blocco
            total_ok += len(ok)
            print(f"  blocco {start // CHUNK + 1}: {len(ok)}/{len(batch)} ok "
                  f"| totale {total_ok}/{len(described)} | {time.monotonic()-t0:.0f}s", flush=True)

    if args.prune:
        cur_urls = set(inv.keys())
        gone = [u for u in existing if u not in cur_urls or not _disk_path(u).exists()]
        for u in gone:
            conn.execute("DELETE FROM image_descriptions WHERE url = ?", (u,))
        conn.commit()
        print(f"  prune: rimosse {len(gone)} righe orfane")

    n_fts = rebuild_image_fts(conn)
    _summary(conn)
    print(f"image_fts (contenuto cercabile): {n_fts} righe")
    conn.close()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _upsert(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    if not rows:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO image_descriptions "
        "(url, source_file, doc_type, width, height, kind, caption, ocr_text, vec, sha1, model, built_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def _load_static_model(model_path: str):
    from model2vec import StaticModel
    if not Path(model_path).is_dir():
        print(f"ERRORE: modello static non trovato: {model_path}")
        sys.exit(1)
    return StaticModel.from_pretrained(model_path, normalize=True)


def _embed(model, texts: list[str]):
    import numpy as np
    vecs = model.encode(texts or [""], use_multiprocessing=False).astype(np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def _summary(conn: sqlite3.Connection) -> None:
    print("Riepilogo image_descriptions per kind:")
    for kind, n in conn.execute(
        "SELECT kind, COUNT(*) FROM image_descriptions GROUP BY kind ORDER BY 2 DESC"
    ):
        print(f"  {kind or '?':12s} {n}")


if __name__ == "__main__":
    main()
