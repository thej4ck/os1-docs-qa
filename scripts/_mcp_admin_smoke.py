"""M5 smoke: admin MCP config + per-domain gate + master switch (in-process).

Isolated temp app.db. Run with the project venv (PYTHONPATH=project root).
Throwaway diagnostic.
"""
import os
import tempfile

tmp = tempfile.mkdtemp(prefix="os1mcpadmin_")
os.environ["APP_DB_PATH"] = os.path.join(tmp, "app.db")
os.environ["GROQ_API_KEY"] = "test-dummy"
os.environ["ADMIN_EMAILS"] = "*@aiwonder.it"
os.environ["MCP_OAUTH_ENABLED"] = "true"      # mount oauth provider at import
os.environ["BASE_URL"] = "http://127.0.0.1:9000"
os.environ.pop("PRODUCTION", None)            # dev → login bypass, master default off

from fastapi.testclient import TestClient
from app.main import app


def check(label, cond):
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
    assert cond, label


with TestClient(app) as client:
    from app.models.domain import (
        add_domain, get_domain_for_email, is_mcp_enabled_for_email, set_domain_mcp,
    )
    from app.models.settings import set_setting, get_setting, get_bool_setting
    from app.mcp.auth import resolve_mcp_auth_mode

    add_domain("*@aiwonder.it", tier="BASE")

    print("Unit — settings / auth mode:")
    set_setting("mcp_auth_mode", "oauth"); check("resolve=oauth", resolve_mcp_auth_mode() == "oauth")
    set_setting("mcp_auth_mode", "bearer"); check("resolve=bearer", resolve_mcp_auth_mode() == "bearer")
    set_setting("mcp_auth_mode", "off"); check("resolve=off", resolve_mcp_auth_mode() == "off")

    print("Unit — per-domain gate:")
    check("aiwonder mcp on (default)", is_mcp_enabled_for_email("u@aiwonder.it") is True)
    d = get_domain_for_email("u@aiwonder.it")
    set_domain_mcp(d["id"], False); check("after disable → False", is_mcp_enabled_for_email("u@aiwonder.it") is False)
    set_domain_mcp(d["id"], True); check("after re-enable → True", is_mcp_enabled_for_email("u@aiwonder.it") is True)

    print("Admin page (dev-bypass login as admin@aiwonder.it):")
    client.post("/login", data={"email": "admin@aiwonder.it"}, follow_redirects=False)
    pg = client.get("/admin/mcp")
    check(f"GET /admin/mcp -> {pg.status_code}", pg.status_code == 200 and "Server MCP" in pg.text)

    print("Admin save → master OFF:")
    client.post("/admin/mcp", data={"auth_mode": "bearer"}, follow_redirects=False)  # mcp_enabled unchecked
    check("mcp_enabled persisted '0'", get_setting("mcp_enabled") == "0")
    g_off = client.get("/mcp")
    check(f"/mcp master off -> {g_off.status_code} (503)", g_off.status_code == 503)

    print("Admin save → master ON:")
    client.post("/admin/mcp", data={"mcp_enabled": "on", "auth_mode": "bearer"}, follow_redirects=False)
    check("mcp_enabled persisted '1'", get_setting("mcp_enabled") == "1")
    g_on = client.get("/mcp")
    check(f"/mcp master on -> {g_on.status_code} (not 503; auth/redirect)", g_on.status_code != 503)

print("M5 ADMIN OK")
