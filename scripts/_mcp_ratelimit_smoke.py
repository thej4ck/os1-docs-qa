"""M4 smoke: rate-limit util + MCP per-IP gate buckets (in-process). Throwaway."""
import os
import tempfile

os.environ["APP_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="os1rl_"), "app.db")
os.environ["GROQ_API_KEY"] = "test-dummy"
os.environ["MCP_OAUTH_ENABLED"] = "true"
os.environ["BASE_URL"] = "http://127.0.0.1:9000"
os.environ.pop("PRODUCTION", None)

from fastapi.testclient import TestClient
from app.main import app
from app.util.ratelimit import allow


def check(label, cond):
    print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
    assert cond, label


print("Unit — allow():")
check("3/3 allowed", all(allow("t", 3, 60) for _ in range(3)))
check("4th blocked", allow("t", 3, 60) is False)
check("other key independent", allow("t2", 3, 60) is True)

with TestClient(app) as client:
    from app.models.settings import set_setting
    set_setting("mcp_enabled", "1")  # master on (dev default off)

    print("MCP /mcp/register bucket (10/h):")
    reg = [client.post("/mcp/register", json={}).status_code for _ in range(11)]
    print("   codes:", reg)
    check("first not 429", reg[0] != 429)
    check("11th == 429", reg[-1] == 429)

    print("MCP /mcp-login bucket (20/min, independent):")
    log = [client.get("/mcp-login?ticket=x").status_code for _ in range(21)]
    print("   first:", log[0], "21st:", log[-1])
    check("first not 429 (separate bucket)", log[0] != 429)
    check("21st == 429", log[-1] == 429)

print("M4 RATELIMIT OK")
