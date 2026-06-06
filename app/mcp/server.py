"""FastMCP server factory for OS1 Virgilio (retrieval-only).

Exposes the `search`/`fetch` tools. Mounted at /mcp by app.main via
`mcp.http_app(path="/")` + `combine_lifespans`. Auth is injected by the
caller: `None` for local dev (M1); a TokenVerifier (M2, dev bearer) or an
autonomous OAuthProvider (M3, claude.ai + ChatGPT) later.
"""

from fastmcp import FastMCP

from app.mcp.tools import register_tools


def build_mcp(auth=None) -> FastMCP:
    mcp = FastMCP(
        "OS1 Virgilio",
        instructions=(
            "Strumenti di ricerca sulla documentazione dell'ERP OS1 (OSItalia). "
            "Usa `search` per trovare i documenti rilevanti, poi `fetch` con l'id "
            "restituito per leggerne il contenuto completo."
        ),
        auth=auth,
    )
    register_tools(mcp)
    return mcp
