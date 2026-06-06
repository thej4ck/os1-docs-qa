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
            "Documentazione ufficiale di OS1, il gestionale ERP di OSItalia. "
            "Usa questo connettore quando l'utente chiede di funzionalità, moduli, "
            "tabelle del database, procedure operative, messaggi di errore o "
            "configurazioni di OS1. Flusso: `search` (parole chiave in italiano) per "
            "trovare i documenti, poi `fetch` con l'`id` restituito per leggerne il "
            "testo completo, infine cita la fonte (campo `url`)."
        ),
        auth=auth,
    )
    register_tools(mcp)
    return mcp
