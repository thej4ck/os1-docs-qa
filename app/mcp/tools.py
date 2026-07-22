"""MCP tools: retrieval-only (search + fetch) over the OS1 documentation.

Schema = OpenAI ChatGPT Deep Research connector (verified against
developers.openai.com/api/docs/mcp), also accepted as-is by Claude:
  search(query) -> {"results": [{"id", "title", "url"}, ...]}
  fetch(id)     -> {"id", "title", "text", "url", "metadata"}

FastMCP serializes the returned dict into both `structuredContent` and a
JSON-encoded `content[]` block automatically. `id` is the document's
source_file (stable key shared by both tools).
"""

import re
from typing import Annotated
from urllib.parse import quote

from pydantic import Field

from app.config import settings
from app.search import query as query_module
from app.util.markers import SCREENSHOT_RE


def _doc_url(source_file: str) -> str:
    """Public, citable URL for a document. Falls back to a relative path."""
    sf = (source_file or "").replace("\\", "/")
    base = settings.base_url.rstrip("/") if settings.base_url else ""
    return f"{base}/api/doc?file={quote(sf, safe='')}"


def _absolutize_screenshots(text: str, source_file: str | None = None) -> tuple[str, list[dict]]:
    """Riscrive i marcatori `[Screenshot: cap | /help-files/...]` in markdown con
    URL assoluti pubblici (`![cap](https://host/help-files/...)`), saltando i logo.

    Arricchimento VLM (se `image_descriptions` è popolata): l'alt-text diventa la
    **caption VLM** (non più l'heading povero) e le immagini di contenuto dei doc
    **help** (che NON hanno marcatori nel testo) vengono **iniettate** come link
    markdown. Le icone/decorazioni (vec NULL) restano escluse.

    Ritorna `(text, images)` dove `images` = `[{url assoluto, caption}]` nell'ordine
    di emissione — così il chiamante non deve re-parsare il markdown appena generato."""
    base = settings.base_url.rstrip("/") if settings.base_url else ""
    idx = query_module._index
    imgs = idx.get_doc_images_for_fetch(source_file) if (idx and source_file) else []
    cap_by_url = {im["url"]: im["caption"] for im in imgs}
    used: set[str] = set()
    images: list[dict] = []  # {url, caption} in ordine di apparizione nel testo

    def _abs(url: str) -> str:
        return f"{base}{url}" if url.startswith("/") else url

    def _sub(m: re.Match) -> str:
        cap, url = m.group(1), m.group(2)
        if query_module._is_logo(url):
            return ""
        used.add(url)
        alt = cap_by_url.get(url) or cap  # caption VLM se disponibile, altrimenti heading
        abs_url = _abs(url)
        images.append({"url": abs_url, "caption": alt})
        return f"![{alt}]({abs_url})"

    out = SCREENSHOT_RE.sub(_sub, text)

    # Inietta le immagini di contenuto del doc non già presenti (tipico dei doc help).
    extra = []
    for im in imgs:
        if im["url"] in used:
            continue
        abs_url = _abs(im["url"])
        images.append({"url": abs_url, "caption": im["caption"]})
        extra.append(f"![{im['caption']}]({abs_url})")
    if extra:
        out = f"{out}\n\n{chr(10).join(extra)}"
    return out, images


def _track_request() -> None:
    """Conta la richiesta MCP (search/fetch) per l'utente autenticato.

    Risoluzione robusta del subject (= email utente):
    1. `at.subject` se presente (caso ideale);
    2. altrimenti lookup nel NOSTRO store via token grezzo — `get_access_token()`
       di FastMCP, convertendo i token dell'AS OAuth, SCARTA `subject`, e per
       OAuth `client_id` è l'UUID del client DCR (non l'email);
    3. fallback `client_id` (modalità bearer: client_id == email utente).
    Best-effort: dev/no-auth o errore → no-op (logga solo l'errore)."""
    try:
        from fastmcp.server.dependencies import get_access_token
        from app.models import oauth as oauth_store
        at = get_access_token()
        if at is None:
            return
        subject = getattr(at, "subject", None)
        if not subject:
            raw = getattr(at, "token", None)
            rec = oauth_store.get_token(raw, "access") if raw else None
            subject = rec["subject"] if rec else getattr(at, "client_id", None)
        if subject:
            oauth_store.bump_mcp_usage(subject)
    except Exception as e:  # noqa: BLE001
        print(f"[mcp usage] track failed: {e}", flush=True)


def register_tools(mcp) -> None:
    @mcp.tool
    async def search(
        query: Annotated[str, Field(
            description="Parole chiave o domanda breve in italiano sull'uso o la "
                        "configurazione di OS1."
        )],
    ) -> dict:
        """Cerca nella documentazione dell'ERP OS1 (OSItalia).

        Usa questo strumento quando la domanda riguarda OS1: funzionalità, moduli,
        tabelle del database, procedure/schede operative, messaggi di errore,
        configurazioni. La documentazione è in ITALIANO → formula `query` in
        italiano. Ritorna i documenti più rilevanti (id, titolo, url); poi chiama
        `fetch` con l'`id` per leggere il contenuto completo e citare la fonte.
        """
        _track_request()
        q = (query or "").strip()
        if not q:
            return {"results": []}
        docs = await query_module.mcp_search(q, limit=10)
        return {
            "results": [
                {
                    "id": (d.get("source_file") or "").replace("\\", "/"),
                    "title": d.get("title") or d.get("source_file") or "",
                    "url": _doc_url(d.get("source_file") or ""),
                }
                for d in docs
            ]
        }

    @mcp.tool
    async def fetch(
        id: Annotated[str, Field(description="L'`id` del documento restituito da search.")],
    ) -> dict:
        """Recupera il testo integrale di un documento OS1 dato l'`id` restituito da
        `search`. Usalo dopo `search` per leggere il contenuto completo e citarlo
        (campo `url`)."""
        _track_request()
        doc = query_module.mcp_fetch(id)
        if doc is None:
            raise ValueError(f"Document not found: {id}")
        raw_sf = doc.get("source_file") or id
        sf = raw_sf.replace("\\", "/")
        # text arricchito + `images` strutturato ({url assoluto, caption} in ordine di
        # apparizione) costruiti in un colpo solo — richiesta (1) dei client MCP.
        text, images = _absolutize_screenshots(doc.get("content") or "", raw_sf)
        return {
            "id": sf,
            "title": doc.get("title") or sf,
            "text": text,
            "url": _doc_url(sf),
            "metadata": {
                "module": doc.get("module"),
                "doc_type": doc.get("doc_type"),
                "images": images,
            },
        }
