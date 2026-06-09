"""MCP tools: retrieval-only (search + fetch) over the OS1 documentation.

Schema = OpenAI ChatGPT Deep Research connector (verified against
developers.openai.com/api/docs/mcp), also accepted as-is by Claude:
  search(query) -> {"results": [{"id", "title", "url"}, ...]}
  fetch(id)     -> {"id", "title", "text", "url", "metadata"}

FastMCP serializes the returned dict into both `structuredContent` and a
JSON-encoded `content[]` block automatically. `id` is the document's
source_file (stable key shared by both tools).
"""

from typing import Annotated
from urllib.parse import quote

from pydantic import Field

from app.config import settings
from app.search import query as query_module


def _doc_url(source_file: str) -> str:
    """Public, citable URL for a document. Falls back to a relative path."""
    sf = (source_file or "").replace("\\", "/")
    base = settings.base_url.rstrip("/") if settings.base_url else ""
    return f"{base}/api/doc?file={quote(sf, safe='')}"


def _track_request() -> None:
    """Conta la richiesta MCP per l'utente autenticato (per la dashboard admin).
    Best-effort: senza auth (dev/no-auth) o subject non risolvibile, no-op."""
    try:
        from fastmcp.server.dependencies import get_access_token
        from app.mcp.auth import subject_of
        from app.models import oauth as oauth_store
        subject = subject_of(get_access_token())
        if subject:
            oauth_store.bump_mcp_usage(subject)
    except Exception:
        pass


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
        sf = (doc.get("source_file") or id).replace("\\", "/")
        return {
            "id": sf,
            "title": doc.get("title") or sf,
            "text": doc.get("content") or "",
            "url": _doc_url(sf),
            "metadata": {
                "module": doc.get("module"),
                "doc_type": doc.get("doc_type"),
            },
        }
