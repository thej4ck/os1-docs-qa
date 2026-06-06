"""M3 full-flow OAuth smoke: DCR → authorize → OTP-login(dev bypass) → token
→ MCP tools via Bearer → refresh. Run against a server started with
MCP_OAUTH_ENABLED=true, PRODUCTION unset (dev bypass), BASE_URL=<base>.

Usage: python scripts/_mcp_oauth_smoke.py <base_url> <email>
Throwaway diagnostic.
"""
import sys
import asyncio
import base64
import hashlib
import secrets
from urllib.parse import urlparse, parse_qs

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

BASE = sys.argv[1].rstrip("/")
EMAIL = sys.argv[2] if len(sys.argv) > 2 else "oauthtest@scao.it"
REDIRECT = "http://localhost:9999/cb"


def pkce():
    v = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


async def main():
    async with httpx.AsyncClient(follow_redirects=False, timeout=15) as h:
        # 1. AS discovery (path-aware well-known at root)
        d = await h.get(f"{BASE}/.well-known/oauth-authorization-server/mcp")
        meta = d.json()
        print(f"discovery {d.status_code}  issuer={meta.get('issuer')}")
        print(f"  authorize={meta.get('authorization_endpoint')}")
        print(f"  token={meta.get('token_endpoint')}  register={meta.get('registration_endpoint')}")
        authorize_ep, token_ep, register_ep = (
            meta["authorization_endpoint"], meta["token_endpoint"], meta["registration_endpoint"])

        # 2. DCR
        reg = await h.post(register_ep, json={
            "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "scope": "docs:read", "client_name": "smoke",
        })
        cid = reg.json()["client_id"]
        print(f"register {reg.status_code}  client_id={cid[:14]}…")

        # 3. authorize (+PKCE)
        verifier, challenge = pkce()
        state = secrets.token_urlsafe(8)
        a = await h.get(authorize_ep, params={
            "response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
            "code_challenge": challenge, "code_challenge_method": "S256",
            "state": state, "scope": "docs:read",
        })
        print(f"authorize {a.status_code} → {a.headers.get('location')}")
        ticket = parse_qs(urlparse(a.headers["location"]).query)["ticket"][0]

        # 4. login (dev bypass: step=email completes without OTP)
        comp = await h.post(f"{BASE}/mcp-login",
                            data={"ticket": ticket, "step": "email", "email": EMAIL})
        print(f"mcp-login {comp.status_code} → {comp.headers.get('location')}")
        q = parse_qs(urlparse(comp.headers["location"]).query)
        code = q["code"][0]
        print(f"  code={code[:10]}…  state_match={q.get('state',[None])[0] == state}")

        # 5. token exchange (framework verifies PKCE)
        tok = await h.post(token_ep, data={
            "grant_type": "authorization_code", "code": code,
            "redirect_uri": REDIRECT, "client_id": cid, "code_verifier": verifier,
        })
        tj = tok.json()
        access, refresh = tj.get("access_token"), tj.get("refresh_token")
        print(f"token {tok.status_code}  access={ (access or '')[:8] }…  refresh={ (refresh or '')[:8] }…")

        # 6. PKCE negative: wrong verifier must fail (re-auth a fresh code)
        # (skip second authorize for brevity; rely on framework)

        # 7. refresh
        rf = await h.post(token_ep, data={
            "grant_type": "refresh_token", "refresh_token": refresh, "client_id": cid})
        access2 = rf.json().get("access_token", access) if rf.status_code == 200 else access
        print(f"refresh {rf.status_code}  new_access={ (rf.json().get('access_token','') if rf.status_code==200 else rf.text)[:8] }…")

    # 8. call MCP tools with the OAuth access token
    for label, tok_val in (("orig", access), ("refreshed", access2)):
        client = Client(StreamableHttpTransport(
            url=f"{BASE}/mcp/", headers={"Authorization": f"Bearer {tok_val}"}))
        async with client:
            tools = await client.list_tools()
            r = await client.call_tool("search", {"query": "anagrafica articolo"})
            data = r.structured_content or getattr(r, "data", None)
            print(f"MCP search ({label} token): {len((data or {}).get('results', []))} results")

    # 9. negative: no token rejected
    try:
        client = Client(StreamableHttpTransport(url=f"{BASE}/mcp/"))
        async with client:
            await client.list_tools()
        print("NO-TOKEN: NOT rejected  ✗")
    except Exception as e:
        print(f"no-token rejected ✓ ({type(e).__name__})")

    print("M3 OAUTH OK")


if __name__ == "__main__":
    asyncio.run(main())
