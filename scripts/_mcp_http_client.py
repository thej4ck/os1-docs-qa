"""M2 HTTP integration: real FastMCP client over Streamable HTTP + Bearer.

Usage: python scripts/_mcp_http_client.py <base_url> <token>
Tests bearer gate via a real httpx-based client (unlike PowerShell IWR, httpx
preserves the Authorization header across same-origin 307 redirects).
Throwaway diagnostic.
"""
import sys
import asyncio

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

BASE = sys.argv[1]
TOK = sys.argv[2]


async def attempt(url, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    client = Client(StreamableHttpTransport(url=url, headers=headers))
    async with client:
        tools = await client.list_tools()
        r = await client.call_tool("search", {"query": "anagrafica articolo"})
        data = r.structured_content or getattr(r, "data", None)
        return [t.name for t in tools], len((data or {}).get("results", []))


async def case(label, url, token):
    try:
        tools, n = await attempt(url, token)
        print(f"{label}: OK  tools={tools} results={n}")
    except Exception as e:
        print(f"{label}: REJECTED  {type(e).__name__}: {str(e)[:140]}")


async def main():
    await case("valid /mcp ", BASE + "/mcp", TOK)
    await case("valid /mcp/", BASE + "/mcp/", TOK)
    await case("no-token   ", BASE + "/mcp/", None)


if __name__ == "__main__":
    asyncio.run(main())
