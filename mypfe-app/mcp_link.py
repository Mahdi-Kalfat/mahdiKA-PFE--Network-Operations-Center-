"""
mcp_link.py  —  MCP client bridge for the Flask app.

The Customer Portal / NOC app talks to the GenieACS and RaDuce services as a
real MCP client (JSON-RPC tools/call over the streamable-HTTP transport),
instead of plain REST. Each helper returns the same (data, err) tuple shape the
rest of app.py already expects, so it is a drop-in replacement for the old
`api("get", ...)` calls.

Env:
    GENIE_MCP   default  http://genie:8001/mcp/
    RADUCE_MCP  default  http://raduce:8002/mcp/

Note: the trailing slash on /mcp/ matters — a request to /mcp 307-redirects to
/mcp/, which MCP clients follow automatically, but pointing straight at /mcp/
avoids the extra round trip.
"""

import os
import json
import asyncio

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

GENIE_MCP  = os.getenv("GENIE_MCP",  "http://genie:8001/mcp/")
RADUCE_MCP = os.getenv("RADUCE_MCP", "http://raduce:8002/mcp/")
CUSTOMER_MCP = os.getenv("CUSTOMER_MCP", "http://customer:8003/mcp/")


def _extract(result) -> dict:
    """Turn an MCP CallToolResult into the plain dict the app expects."""
    # 1) Prefer structured content when present (FastMCP returns it for dicts).
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        # FastMCP wraps a bare value under {"result": ...}; unwrap that case.
        if set(sc.keys()) == {"result"} and isinstance(sc["result"], dict):
            return sc["result"]
        return sc
    # 2) Fall back to the first text block, parsed as JSON.
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except Exception:
                return {"result": text}
    return {}


async def _acall(url: str, tool: str, args: dict):
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args or {})
            data = _extract(result)
            # A tool that *ran* but returned {"error": ...} is NOT a transport
            # error — keep the old REST behaviour (data with err=None) so the
            # existing `.get("error")` checks downstream still work.
            return data, None


def call(url: str, tool: str, args: dict = None):
    """Synchronous wrapper used from Flask request handlers."""
    try:
        return asyncio.run(_acall(url, tool, args or {}))
    except Exception as e:
        host = url.split("/")[2] if "//" in url else url
        return None, f"Cannot reach {host} via MCP — {e}"


def genie(tool: str, args: dict = None):
    return call(GENIE_MCP, tool, args)


def raduce(tool: str, args: dict = None):
    return call(RADUCE_MCP, tool, args)


def customer(tool: str, args: dict = None):
    return call(CUSTOMER_MCP, tool, args)
