"""MCP tools: retrieval-only (search + fetch) over the OS1 documentation.

Schema = OpenAI ChatGPT Deep Research connector (verified against
developers.openai.com/api/docs/mcp), also accepted as-is by Claude:
  search(query) -> {"results": [{"id", "title", "url"}, ...]}
  fetch(id)     -> {"id", "title", "text", "url", "metadata"}

FastMCP serializes the returned dict into both `structuredContent` and a
JSON-encoded `content[]` block automatically. `id` is the document's
source_file (stable key shared by both tools).
"""

from urllib.parse import quote

from app.config import settings
from app.search import query as query_module


def _doc_url(source_file: str) -> str:
    """Public, citable URL for a document. Falls back to a relative path."""
    sf = (source_file or "").replace("\\", "/")
    base = settings.base_url.rstrip("/") if settings.base_url else ""
    return f"{base}/api/doc?file={quote(sf, safe='')}"


def register_tools(mcp) -> None:
    @mcp.tool
    async def search(query: str) -> dict:
        """Cerca nella documentazione dell'ERP OS1.

        Restituisce i documenti più rilevanti (id, titolo, url). Usa poi
        `fetch` con l'`id` per leggere il contenuto completo.
        """
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
    async def fetch(id: str) -> dict:
        """Recupera il contenuto completo di un documento OS1 dato il suo `id`
        (il source_file restituito da `search`)."""
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
