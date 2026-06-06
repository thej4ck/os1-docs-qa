"""Smoke test M2: bearer verifier reuses the user's access_token.

Run with the project venv (PYTHONPATH=project root):
    .venv\\Scripts\\python.exe scripts\\_mcp_auth_smoke.py
Throwaway diagnostic. Creates/uses a test user in data/app.db.
"""
import os

os.environ.setdefault("GROQ_API_KEY", "test-dummy")
os.environ["MCP_AUTH_ENABLED"] = "true"

import asyncio

from app.config import settings
from app import db as app_db


async def main():
    app_db.init(settings.app_db_path)
    from app.models.user import get_or_create_user
    from app.mcp.auth import OS1TokenVerifier, build_mcp_auth

    user = get_or_create_user("mcptest@scao.it")
    tok = user["access_token"]
    print(f"test token: {tok[:8]}…")

    v = OS1TokenVerifier()
    good = await v.verify_token(tok)
    bad = await v.verify_token("not-a-real-token-xyz")
    empty = await v.verify_token("")

    print("valid  ->", (good.client_id, good.scopes) if good else None)
    print("bogus  ->", bad)
    print("empty  ->", empty)
    print("build_mcp_auth(flag on) ->", type(build_mcp_auth()).__name__)

    assert good and good.client_id == "mcptest@scao.it" and "docs:read" in good.scopes
    assert bad is None and empty is None
    assert build_mcp_auth() is not None

    # flag off -> no auth
    settings.mcp_auth_enabled = False
    print("build_mcp_auth(flag off) ->", build_mcp_auth())
    assert build_mcp_auth() is None

    print("M2 AUTH OK")


if __name__ == "__main__":
    asyncio.run(main())
