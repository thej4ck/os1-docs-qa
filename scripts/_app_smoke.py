"""Regression smoke: app boots + key routes respond under the new starlette.

Run with the project venv (PYTHONPATH=project root):
    .venv\\Scripts\\python.exe scripts\\_app_smoke.py
Throwaway diagnostic (not part of the app). Uses TestClient → runs lifespan
(loads search.db + embeddings + app.db), so it exercises startup end-to-end.
"""
import os

os.environ.setdefault("GROQ_API_KEY", "test-dummy")

import starlette
import fastmcp
from fastapi.testclient import TestClient
from app.main import app

print(f"starlette={starlette.__version__}  fastmcp={fastmcp.__version__}")

with TestClient(app) as client:  # __enter__ triggers the startup lifespan
    h = client.get("/healthz")
    print(f"GET /healthz -> {h.status_code} {h.json()}")

    root = client.get("/", follow_redirects=False)
    print(f"GET / -> {root.status_code} (len={len(root.text)})")

    # MCP mounted? Streamable-HTTP wants POST+headers; a bare GET should be a
    # 4xx (mounted, rejecting the request) — NOT 404 (not mounted) or 5xx (crash).
    m = client.get("/mcp")
    print(f"GET /mcp -> {m.status_code}")

    assert h.status_code == 200, "healthz failed"
    assert root.status_code in (200, 302, 303, 307), f"landing unexpected {root.status_code}"
    assert m.status_code != 404 and m.status_code < 500, f"/mcp not mounted or crashed: {m.status_code}"

print("APP REGRESSION OK")
