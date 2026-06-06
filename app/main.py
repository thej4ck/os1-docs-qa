"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.search.fts import SearchIndex
from app.search import query as query_module
from app import db as app_db

from fastmcp.utilities.lifespan import combine_lifespans
from app.mcp.server import build_mcp
from app.mcp.auth import build_mcp_auth


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open the pre-built search index in read-only mode
    db_path = settings.db_path
    if not Path(db_path).exists():
        raise RuntimeError(f"Search database not found: {db_path}. Run scripts/build_index.py first.")

    index = SearchIndex(db_path, read_only=True)
    query_module.init(index)
    print(f"Search index loaded: {index.count()} documents")

    # Semantic index (model2vec) for hybrid retrieval. Optional: degrades to
    # BM25-only if the model dir or embeddings are missing.
    if settings.hybrid_enabled:
        from app.search.embeddings import EmbeddingIndex
        emb = EmbeddingIndex(index, settings.static_model_path)
        query_module.init_embeddings(emb)
        print(f"Embedding index: {emb.status}")
    else:
        print("Embedding index: disabled (hybrid_enabled=False)")

    # Open app database (users, conversations, usage)
    app_db.init(settings.app_db_path)
    print(f"App database loaded: {settings.app_db_path}")

    yield

    app_db.close()
    index.close()


class _MCPMasterGate:
    """Pure-ASGI master switch for MCP. When the admin setting `mcp_enabled` is
    off, `/mcp*` and `/mcp-login` return 503 (LIVE, no restart needed). Plain
    pass-through otherwise — must NOT buffer (the MCP transport streams)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/") or path.startswith("/mcp-login"):
                from app.models.settings import get_setting
                default = "1" if settings.production else "0"
                if get_setting("mcp_enabled", default) != "1":
                    from starlette.responses import JSONResponse as _JR
                    await _JR({"error": "MCP non abilitato"}, status_code=503)(scope, receive, send)
                    return
        await self.app(scope, receive, send)


# MCP server (retrieval-only). http_app(path="/") mounted under /mcp; its
# session-manager lifespan is combined with the app lifespan so it initializes.
mcp_auth = build_mcp_auth()  # OAuth provider (M3) | bearer verifier (M2) | None (dev)
mcp = build_mcp(auth=mcp_auth)
mcp_app = mcp.http_app(path="/")

app = FastAPI(title="OS1 Docs Q&A", lifespan=combine_lifespans(lifespan, mcp_app.lifespan))
app.mount("/mcp", mcp_app)  # Streamable HTTP endpoint at /mcp

# OAuth discovery (.well-known) must live at the DOMAIN ROOT (RFC 8414/9728);
# the /mcp mount can't host it. Mount the provider's well-known routes at root.
from fastmcp.server.auth import OAuthProvider as _OAuthProvider  # noqa: E402
if isinstance(mcp_auth, _OAuthProvider):
    for _wk in mcp_auth.get_well_known_routes():
        app.router.routes.insert(0, _wk)

# Master switch (admin-toggleable at runtime) gating the MCP endpoints.
app.add_middleware(_MCPMasterGate)

# Static files
static_dir = Path(__file__).parent.parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Help files (images) — try bundled help-files/ first, then docs repo
help_dir_bundled = Path(__file__).parent.parent / "help-files"
help_dir_repo = Path(settings.docs_repo_path).resolve() / "sources" / "help"
help_dir = help_dir_bundled if help_dir_bundled.is_dir() else help_dir_repo
if help_dir.is_dir():
    app.mount("/help-files", StaticFiles(directory=str(help_dir)), name="help-files")
    print(f"Help files mounted: {help_dir}")

# Templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Import and include routes
from app.routes.chat_routes import router as chat_router
from app.routes.auth_routes import router as auth_router
from app.routes.admin_routes import router as admin_router
from app.routes.signup_routes import router as signup_router
from app.routes.mcp_auth_routes import router as mcp_auth_router

app.include_router(auth_router)
app.include_router(signup_router)
app.include_router(chat_router)
app.include_router(admin_router)
app.include_router(mcp_auth_router)


@app.get("/healthz")
async def healthcheck():
    """Railway healthcheck endpoint."""
    from app.version import VERSION, BUILD
    return JSONResponse({"status": "ok", "version": VERSION, "build": BUILD})
