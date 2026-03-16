"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.search.fts import SearchIndex
from app.search import query as query_module
from app import db as app_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open the pre-built search index in read-only mode
    db_path = settings.db_path
    if not Path(db_path).exists():
        raise RuntimeError(f"Search database not found: {db_path}. Run scripts/build_index.py first.")

    index = SearchIndex(db_path, read_only=True)
    query_module.init(index)
    print(f"Search index loaded: {index.count()} documents")

    # Open app database (users, conversations, usage)
    app_db.init(settings.app_db_path)
    print(f"App database loaded: {settings.app_db_path}")

    yield

    app_db.close()
    index.close()


app = FastAPI(title="OS1 Docs Q&A", lifespan=lifespan)

# Static files
static_dir = Path(__file__).parent.parent / "static"
if static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Help files (images from docs repo)
help_dir = Path(settings.docs_repo_path).resolve() / "sources" / "help"
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

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(admin_router)
