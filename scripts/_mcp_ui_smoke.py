"""M-UI smoke: landing MCP section + chat connect button/modal, gated. Throwaway."""
import os
import tempfile

os.environ["APP_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="os1ui_"), "app.db")
os.environ["GROQ_API_KEY"] = "test-dummy"
os.environ["ADMIN_EMAILS"] = "*@aiwonder.it"
os.environ["BASE_URL"] = "https://os1.ai.scao.it"
os.environ.pop("PRODUCTION", None)

from fastapi.testclient import TestClient
from app.main import app


def check(label, cond):
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
    assert cond, label


with TestClient(app) as client:
    from app.models.domain import add_domain
    from app.models.settings import set_setting
    add_domain("*@aiwonder.it", tier="BASE")

    print("Landing (logged out):")
    set_setting("mcp_enabled", "1")
    r = client.get("/login")
    check("section present", r.status_code == 200 and "Portalo dentro Claude" in r.text)
    check("url NOT advertised on splash", "os1.ai.scao.it/mcp" not in r.text)
    set_setting("mcp_enabled", "0")
    r = client.get("/login")
    check("section hidden when off", "Portalo dentro Claude" not in r.text)

    print("Chat (admin logged in):")
    client.post("/login", data={"email": "admin@aiwonder.it"}, follow_redirects=False)
    set_setting("mcp_enabled", "1")
    c = client.get("/chat")
    check("button + modal present", c.status_code == 200 and "mcp-connect-btn" in c.text and "Collega OS1 a un assistente AI" in c.text)
    set_setting("mcp_enabled", "0")
    c = client.get("/chat")
    check("button hidden when off", "mcp-connect-btn" not in c.text)

    print("Self-revoke (/api/mcp/revoke-mine):")
    from app.models import oauth as ost
    ost.store_token("uitok-access", "access", "cid-test", "admin@aiwonder.it", ["docs:read"], None)
    check("token active before", ost.active_count_for_subject("admin@aiwonder.it") >= 1)
    rr = client.post("/api/mcp/revoke-mine")
    check("endpoint 200 + revoked>=1", rr.status_code == 200 and rr.json().get("revoked", 0) >= 1)
    check("none active after", ost.active_count_for_subject("admin@aiwonder.it") == 0)

print("M-UI OK")
