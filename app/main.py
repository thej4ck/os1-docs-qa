"""FastAPI application entry point."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.search.fts import SearchIndex
from app.search import query as query_module


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Open the pre-built search index in read-only mode
    db_path = settings.db_path
    if not Path(db_path).exists():
        raise RuntimeError(f"Search database not found: {db_path}. Run scripts/build_index.py first.")

    index = SearchIndex(db_path, read_only=True)
    query_module.init(index)
    print(f"Search index loaded: {index.count()} documents")

    yield

    index.close()


app = FastAPI(title="OS1 Docs Q&A", lifespan=lifespan)

# Templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

# Import and include routes
from app.routes.chat_routes import router as chat_router
from app.routes.auth_routes import router as auth_router

app.include_router(auth_router)
app.include_router(chat_router)
