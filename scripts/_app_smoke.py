"""Regression smoke: every template page renders (no 500) under Starlette 1.x.

Refactor-puro guard for the Starlette 0.52.1 -> 1.x migration. The breaking change
was Jinja2Templates.TemplateResponse(name, ctx) -> (request, name, ctx); this script
proves all 18 migrated call sites render through real HTTP requests.

Run from the project root with the venv python (so search.db/app.db paths resolve):
    .venv\\Scripts\\python.exe scripts\\_app_smoke.py
Uses an ISOLATED temp app.db (APP_DB_PATH) — never touches data/app.db. TestClient as a
context manager runs the full lifespan (search.db + embeddings + temp app.db).
Throwaway diagnostic (not part of the app).
"""
import os
import tempfile
from pathlib import Path

# Env MUST be set before importing app.* (pydantic Settings reads it at import).
ADMIN = "smoke-admin@scao.it"
_tmp_db = os.path.join(tempfile.mkdtemp(prefix="os1smoke_"), "smoke_app.db")
os.environ["GROQ_API_KEY"] = "test-dummy"
os.environ["APP_DB_PATH"] = _tmp_db
os.environ["SECRET_KEY"] = "smoke-secret"
os.environ["ADMIN_EMAILS"] = "*@scao.it"   # seeded user becomes admin on creation
os.environ.pop("PRODUCTION", None)         # dev: http cookies, no Secure flag

import starlette
from fastapi.testclient import TestClient

from app.main import app
from app.auth.session import _get_serializer, COOKIE_NAME
from app.models.user import get_or_create_user
from app.models.conversation import create_conversation, add_message
from app.models.domain import add_domain

print(f"starlette=={starlette.__version__}")
assert starlette.__version__.startswith("1."), (
    f"Expected Starlette 1.x, got {starlette.__version__}"
)

failures: list[str] = []


def check(client, method: str, path: str, expected: set[int], **kw) -> "object | None":
    """Hit a route, assert status in `expected` (and never 500). Returns the response."""
    kw.setdefault("follow_redirects", False)
    resp = client.request(method, path, **kw)
    ok = resp.status_code in expected
    flag = "ok " if ok else "FAIL"
    print(f"  [{flag}] {method:4} {path:42} -> {resp.status_code} (exp {sorted(expected)})")
    if not ok:
        snippet = resp.text[:300].replace("\n", " ")
        failures.append(f"{method} {path} -> {resp.status_code}: {snippet}")
    return resp


with TestClient(app) as client:  # __enter__ runs the startup lifespan
    # ── Seed the isolated app.db ──
    user = get_or_create_user(ADMIN)
    assert user["is_admin"], "seeded user is not admin (check ADMIN_EMAILS)"
    try:
        add_domain("*@scao.it", tier="BASE")  # makes /admin/domains render a row + clean ask() gates
    except Exception as e:
        print(f"  (domain seed skipped: {e})")
    conv_id = create_conversation(user["id"], "Smoke conversation")
    # >= max_messages user turns so POST /api/ask hits the per-conversation gate (no Groq).
    for i in range(21):
        add_message(conv_id, "user", f"domanda smoke {i}")
    add_message(conv_id, "assistant", "risposta smoke", sources=[{"id": "doc/x", "title": "Doc X"}])

    # ── Phase A: anonymous renders (no session cookie) ──
    print("Phase A — anonymous pages:")
    check(client, "GET", "/healthz", {200})
    check(client, "GET", "/login", {200})            # login.html
    check(client, "GET", "/signup", {200})           # signup.html
    check(client, "POST", "/signup", {200},          # signup.html (gdpr-off validation error render)
          data={"first_name": "Test", "last_name": "User", "email": "tester@corp-smoke.it",
                "company_name": "Corp", "gdpr": ""})
    check(client, "POST", "/verify", {200},          # verify.html (bad code render)
          data={"email": "tester@scao.it", "code": "000000"})
    check(client, "POST", "/signup/verify", {200},   # signup_verify.html (bad code render)
          data={"first_name": "Test", "last_name": "User", "email": "tester@corp-smoke.it",
                "company_name": "Corp", "code": "000000"})
    check(client, "GET", "/", {302})                 # -> /login when anonymous

    # static asset is served by StaticFiles under 1.x
    static_root = Path(__file__).resolve().parent.parent / "static"
    asset = next((p for p in static_root.rglob("*") if p.is_file()), None)
    if asset:
        rel = asset.relative_to(static_root).as_posix()
        check(client, "GET", f"/static/{rel}", {200})
    else:
        print("  (no static asset found to probe)")

    # ── Phase B: authenticated admin renders ──
    client.cookies.set(COOKIE_NAME, _get_serializer().dumps({"email": ADMIN}))
    print("Phase B — authenticated (admin) pages:")
    check(client, "GET", "/", {302})                                  # -> /chat when logged in
    check(client, "GET", "/chat", {200})                              # chat.html (empty)
    check(client, "GET", f"/chat?c={conv_id}", {200})                 # chat.html (with messages)
    check(client, "GET", "/admin", {200})                             # dashboard.html
    check(client, "GET", "/admin/users", {200})                       # users.html
    check(client, "GET", f"/admin/users/{ADMIN}", {200})              # user_detail.html
    check(client, "GET", "/admin/usage", {200})                       # usage.html
    check(client, "GET", "/admin/costs", {200})                       # costs.html
    check(client, "GET", f"/admin/conversations/{conv_id}", {200})    # conversation.html
    check(client, "GET", "/admin/domains", {200})                     # domains.html
    check(client, "GET", "/admin/feedback", {200})                    # feedback.html
    check(client, "GET", "/admin/announcement", {200})                # announcement.html

    # ── SSE: EventSourceResponse (sse-starlette) under Starlette 1.x, no Groq ──
    print("Phase C — SSE gate (no Groq):")
    sse = check(client, "POST", "/api/ask", {200},
                data={"question": "x", "conversation_id": conv_id})
    if sse is not None and sse.status_code == 200:
        ct = sse.headers.get("content-type", "")
        ok = ct.startswith("text/event-stream")
        print(f"  [{'ok ' if ok else 'FAIL'}] content-type = {ct!r}")
        if not ok:
            failures.append(f"/api/ask content-type not SSE: {ct!r}")

if failures:
    print(f"\nSMOKE FAILED ({len(failures)} issue(s)):")
    for f in failures:
        print(f"  - {f}")
    raise SystemExit(1)

print("\nPAGES RENDER OK — all templates render under Starlette 1.x, SSE serves.")
