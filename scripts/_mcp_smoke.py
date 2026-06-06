"""Smoke test M1: in-memory FastMCP client → search/fetch on real search.db.

Run with the project venv:  .venv\\Scripts\\python.exe scripts\\_mcp_smoke.py
Throwaway diagnostic (not part of the app).
"""
import asyncio

from app.config import settings
from app.search.fts import SearchIndex
from app.search import query as query_module
from fastmcp import Client
from app.main import mcp


async def main():
    idx = SearchIndex(settings.db_path, read_only=True)
    query_module.init(idx)
    print(f"index docs: {idx.count()}")
    if settings.hybrid_enabled:
        try:
            from app.search.embeddings import EmbeddingIndex
            emb = EmbeddingIndex(idx, settings.static_model_path)
            query_module.init_embeddings(emb)
            print(f"embeddings: {emb.status}")
        except Exception as e:
            print(f"embeddings skipped: {e}")

    async with Client(mcp) as c:
        tools = await c.list_tools()
        print("TOOLS:", [t.name for t in tools])

        r = await c.call_tool("search", {"query": "anagrafica articolo"})
        data = r.structured_content if r.structured_content is not None else getattr(r, "data", None)
        results = (data or {}).get("results", [])
        print(f"SEARCH -> {len(results)} results")
        for x in results[:3]:
            print("  -", x.get("id"), "|", (x.get("title") or "")[:50], "|", x.get("url"))

        if results:
            fid = results[0]["id"]
            f = await c.call_tool("fetch", {"id": fid})
            fd = f.structured_content if f.structured_content is not None else getattr(f, "data", None)
            print(f"FETCH '{fid}' -> title={ (fd or {}).get('title') }  text_len={ len((fd or {}).get('text') or '') }  meta={ (fd or {}).get('metadata') }")
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
