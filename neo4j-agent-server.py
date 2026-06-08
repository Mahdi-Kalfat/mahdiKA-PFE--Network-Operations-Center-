"""
server.py  v1.2
---------------
UTCP HTTP server — Router TR-069 Parameter Lookup via Neo4j GraphRAG

Key fix in v1.2
---------------
The UTCP SDK never sends a "tool" field in the request body.  It routes
tool calls by URL — whichever URL is declared in the tool's
tool_call_template is the URL that gets hit.  All tools sharing the same
/tools/call endpoint meant the server could never know which tool to run.

Solution: every tool now has its own dedicated endpoint:
  GET  /tools/list_routers
  GET  /tools/get_router_parameters?model=...
  GET  /tools/get_router_by_category?model=...&category=...
  GET  /tools/search_parameter?keyword=...
  GET  /tools/search_by_technology?technology=...

Run:
    python server.py

Requirements:
    pip install fastapi uvicorn neo4j python-dotenv
"""

import os
import time
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from typing import Any, Optional
from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "routers")
SERVER_HOST    = os.getenv("SERVER_HOST",    "0.0.0.0")
SERVER_PORT    = int(os.getenv("SERVER_PORT", "8000"))
BASE_URL       = os.getenv("BASE_URL",       f"http://localhost:{SERVER_PORT}")

# ---------------------------------------------------------------------------
# Neo4j connection
# ---------------------------------------------------------------------------

driver = None


def get_driver():
    global driver
    if driver is None:
        try:
            driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
            driver.verify_connectivity()
            print(f"[server] Connected to Neo4j at {NEO4J_URI}")
        except Exception as e:
            print(f"[server] WARNING: Could not connect to Neo4j: {e}")
            driver = None
    return driver


def db(cypher: str, params: dict = {}) -> list[dict]:
    d = get_driver()
    if d is None:
        raise RuntimeError(
            f"Neo4j is not connected. Make sure Neo4j is running at "
            f"{NEO4J_URI} with user '{NEO4J_USER}'."
        )
    with d.session(database=NEO4J_DATABASE) as session:
        return [record.data() for record in session.run(cypher, params)]


# ---------------------------------------------------------------------------
# Simple TTL cache
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[Any, float]] = {}
CACHE_TTL = 300


def cached_db(key: str, cypher: str, params: dict = {}) -> list[dict]:
    now = time.time()
    if key in _cache:
        value, ts = _cache[key]
        if now - ts < CACHE_TTL:
            return value
    result = db(cypher, params)
    _cache[key] = (result, now)
    return result


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Router MCP Server",
    description="UTCP HTTP server for router TR-069 parameter lookup via Neo4j",
    version="1.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# UTCP Manual — GET /tools
# Each tool declares its OWN URL so the UTCP SDK knows exactly where to call.
# ---------------------------------------------------------------------------

def build_utcp_manual(base_url: str) -> dict:
    return {
        "utcp_version": "1.0.0",
        "manual_version": "1.2.0",
        "tools": [
            {
                "name": "list_routers",
                "description": (
                    "List all router models available in the knowledge graph. "
                    "Returns router ID, product class, vendor, and technology type "
                    "(GPON, ADSL/VDSL, DSL). Call this first to see what routers are available."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {},
                    "required": []
                },
                "outputs": {
                    "type": "object",
                    "properties": {
                        "total": {"type": "number"},
                        "routers": {"type": "array"}
                    }
                },
                "tags": ["router", "inventory", "neo4j", "list"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/list_routers",
                    "content_type": "application/json"
                }
            },
            {
                "name": "get_router_parameters",
                "description": (
                    "Get ALL TR-069 parameters and their exact paths for a given router model. "
                    "Parameters are grouped by category: "
                    "basic_info (serial number, MAC, IP, OUI), "
                    "ppp_info (PPP login, password, connection status, VLAN, IPv4, IPv6), "
                    "voice_info (SIP URI, auth credentials, proxy servers, ports), "
                    "wifi_info (SSID, password, channel, bandwidth, mode), "
                    "diagnostic (RX/TX optical power, temperature, bias current), "
                    "lan (connected hosts). "
                    "Use this when a user asks about router settings or wants to fix a problem. Input parameter is model (not router_id)."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "model": {
                            "type": "string",
                            "description": (
                                "Router model name or product class. Fuzzy matched. "
                                "Examples: 'NOKIA 1425', 'G-1425G-B', 'HG8145V5', "
                                "'VC220-G3v', 'D-Link DSL224', 'Huawei 8145', 'V163'"
                            )
                        }
                    },
                    "required": ["model"]
                },
                "outputs": {
                    "type": "object",
                    "properties": {
                        "router_id":     {"type": "string"},
                        "product_class": {"type": "string"},
                        "vendor":        {"type": "string"},
                        "technology":    {"type": "string"},
                        "sheet_name":    {"type": "string"},
                        "total_params":  {"type": "number"},
                        "categories":    {"type": "object"}
                    }
                },
                "tags": ["router", "parameters", "neo4j", "lookup"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/get_router_parameters",
                    "content_type": "application/json"
                }
            },
            {
                "name": "get_router_by_category",
                "description": (
                    "Get parameters for a specific router filtered by one category only. "
                    "Use when the user asks only about WiFi, VoIP, PPP, or diagnostics. "
                    "Categories: basic_info, ppp_info, voice_info, wifi_info, diagnostic, lan. Input parameter is model (not router_id)."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "model": {
                            "type": "string",
                            "description": "Router model name or product class."
                        },
                        "category": {
                            "type": "string",
                            "enum": ["basic_info", "ppp_info", "voice_info", "wifi_info", "diagnostic", "lan"],
                            "description": "The parameter category to filter by."
                        }
                    },
                    "required": ["model", "category"]
                },
                "outputs": {
                    "type": "object",
                    "properties": {
                        "router_id": {"type": "string"},
                        "category":  {"type": "string"},
                        "total":     {"type": "number"},
                        "params":    {"type": "array"}
                    }
                },
                "tags": ["router", "parameters", "category", "neo4j"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/get_router_by_category",
                    "content_type": "application/json"
                }
            },
            {
                "name": "search_parameter",
                "description": (
                    "Search for a parameter keyword across ALL routers in the database. "
                    "Use when you know a parameter name and want to find which routers "
                    "support it and what the exact TR-069 path is for each router. "
                    "Useful keywords: 'SSID', 'Password', 'RXPower', "
                    "'MACAddress', 'SIP', 'ConnectionStatus', 'Channel'."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "keyword": {
                            "type": "string",
                            "description": "Keyword to search in parameter labels or TR-069 paths."
                        }
                    },
                    "required": ["keyword"]
                },
                "outputs": {
                    "type": "object",
                    "properties": {
                        "keyword":       {"type": "string"},
                        "total_matches": {"type": "number"},
                        "routers_count": {"type": "number"},
                        "results":       {"type": "object"}
                    }
                },
                "tags": ["router", "search", "parameter", "neo4j"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/search_parameter",
                    "content_type": "application/json"
                }
            },
            {
                "name": "search_by_technology",
                "description": (
                    "List all routers filtered by their technology type: GPON, ADSL, VDSL, or DSL. "
                    "Use when the user asks about a specific access technology. "
                    "Technology values (case-insensitive): 'GPON', 'ADSL', 'VDSL', 'DSL'."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "technology": {
                            "type": "string",
                            "description": "Technology type. Examples: 'GPON', 'ADSL', 'VDSL', 'DSL'."
                        }
                    },
                    "required": ["technology"]
                },
                "outputs": {
                    "type": "object",
                    "properties": {
                        "technology": {"type": "string"},
                        "total":      {"type": "number"},
                        "routers":    {"type": "array"}
                    }
                },
                "tags": ["router", "technology", "gpon", "adsl", "vdsl", "neo4j"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/search_by_technology",
                    "content_type": "application/json"
                }
            }
        ]
    }


# ---------------------------------------------------------------------------
# Cypher helpers
# ---------------------------------------------------------------------------

def infer_category(label: str, path: str) -> str:
    label = (label or "").lower()
    path  = (path or "").lower()
    if "ssid" in label or "ssid" in path or "wifi" in label or "wifi" in path:
        return "wifi_info"
    if "ppp" in label or "ppp" in path or "pppoe" in path:
        return "ppp_info"
    if "sip" in label or "voip" in label or "voip" in path or "telephone" in label:
        return "voice_info"
    if "lan" in label or "lan" in path or "host" in label or "host" in path:
        return "lan"
    if any(k in label for k in ("rx", "tx", "power", "temperature", "bias", "signal")):
        return "diagnostic"
    return "basic_info"


CATEGORY_CYPHER = """coalesce(rel.category,
    CASE
        WHEN toLower(rel.label) CONTAINS 'ssid' OR toLower(p.path) CONTAINS 'ssid'
          OR toLower(p.path) CONTAINS 'wifi'                                        THEN 'wifi_info'
        WHEN toLower(rel.label) CONTAINS 'ppp' OR toLower(p.path) CONTAINS 'ppp'
          OR toLower(p.path) CONTAINS 'pppoe'                                       THEN 'ppp_info'
        WHEN toLower(rel.label) CONTAINS 'sip' OR toLower(rel.label) CONTAINS 'voip'
          OR toLower(p.path) CONTAINS 'voip'                                        THEN 'voice_info'
        WHEN toLower(rel.label) CONTAINS 'lan' OR toLower(p.path) CONTAINS 'lan'
          OR toLower(rel.label) CONTAINS 'host'                                     THEN 'lan'
        WHEN toLower(rel.label) CONTAINS 'rx' OR toLower(rel.label) CONTAINS 'tx'
          OR toLower(rel.label) CONTAINS 'power' OR toLower(rel.label) CONTAINS 'temperature'
          OR toLower(rel.label) CONTAINS 'bias'  OR toLower(rel.label) CONTAINS 'signal' THEN 'diagnostic'
        ELSE 'basic_info'
    END
) AS category"""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    d = get_driver()
    if d is None:
        return {"status": "degraded", "neo4j": f"not connected ({NEO4J_URI})"}
    try:
        d.verify_connectivity()
        rows = db("""
            MATCH (r:Router) WITH count(r) AS routers
            MATCH (p:Parameter) WITH routers, count(p) AS params
            MATCH ()-[rel:HAS_PARAM]->()
            RETURN routers, params, count(rel) AS relationships
        """)
        c = rows[0] if rows else {}
        return {
            "status": "ok",
            "neo4j":  "connected",
            "graph": {
                "routers":       c.get("routers", 0),
                "parameters":    c.get("params", 0),
                "relationships": c.get("relationships", 0),
            }
        }
    except Exception as e:
        return {"status": "error", "neo4j": str(e)}


@app.get("/tools")
def list_tools(request: Request):
    """UTCP manual — code-mode-mcp reads this at startup to discover tools."""
    host = request.headers.get("host", f"localhost:{SERVER_PORT}")
    scheme = request.headers.get("x-forwarded-proto", "http")
    effective_base = os.getenv("BASE_URL") or f"{scheme}://{host}"
    manual = build_utcp_manual(effective_base)
    print(f"[server] GET /tools -> {len(manual['tools'])} tools (base={effective_base})")
    return manual


# ---------------------------------------------------------------------------
# Individual tool endpoints
# The UTCP SDK sends tool args as query params for GET requests.
# Each tool has its own URL so routing is unambiguous.
# ---------------------------------------------------------------------------

@app.get("/tools/list_routers")
def api_list_routers():
    print("[server] list_routers called")
    try:
        return tool_list_routers()
    except Exception as e:
        return {"error": str(e)}


@app.get("/tools/get_router_parameters")
def api_get_router_parameters(model: str = Query(..., description="Router model name")):
    print(f"[server] get_router_parameters: model={model!r}")
    try:
        return tool_get_router_parameters(model)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Server error: {e}"}


@app.get("/tools/get_router_by_category")
def api_get_router_by_category(
    model:    str = Query(..., description="Router model name"),
    category: str = Query(..., description="Parameter category")
):
    print(f"[server] get_router_by_category: model={model!r} category={category!r}")
    try:
        return tool_get_router_by_category(model, category)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Server error: {e}"}


@app.get("/tools/search_parameter")
def api_search_parameter(keyword: str = Query(..., description="Search keyword")):
    print(f"[server] search_parameter: keyword={keyword!r}")
    try:
        return tool_search_parameter(keyword)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Server error: {e}"}


@app.get("/tools/search_by_technology")
def api_search_by_technology(technology: str = Query(..., description="Technology type")):
    print(f"[server] search_by_technology: technology={technology!r}")
    try:
        return tool_search_by_technology(technology)
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"Server error: {e}"}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_list_routers() -> dict:
    rows = cached_db("list_routers", """
        MATCH (r:Router)
        RETURN
            r.id     AS router_id,
            r.product_class AS product_class,
            r.vendor        AS vendor,
            r.technology    AS technology,
            r.sheet_name    AS sheet_name
        ORDER BY r.vendor, r.id
    """)
    return {"total": len(rows), "routers": rows}


def tool_get_router_parameters(model: str) -> dict:
    if not model:
        raise ValueError("'model' is required.")

    rows = db(f"""
        MATCH (r:Router)-[rel:HAS_PARAM]->(p:Parameter)
        WHERE toLower(r.id)     CONTAINS toLower($model)
           OR toLower(r.product_class) CONTAINS toLower($model)
           OR toLower(r.sheet_name)    CONTAINS toLower($model)
        RETURN
            r.id     AS router_id,
            r.product_class AS product_class,
            r.vendor        AS vendor,
            r.technology    AS technology,
            r.sheet_name    AS sheet_name,
            {CATEGORY_CYPHER},
            rel.label       AS label,
            rel.band        AS band,
            rel.editable    AS editable,
            p.path          AS path
        ORDER BY category, rel.label
    """, {"model": model})

    if not rows:
        for word in model.replace("-", " ").split():
            if len(word) < 3:
                continue
            rows = db(f"""
                MATCH (r:Router)-[rel:HAS_PARAM]->(p:Parameter)
                WHERE toLower(r.id)     CONTAINS toLower($word)
                   OR toLower(r.product_class) CONTAINS toLower($word)
                RETURN
                    r.id     AS router_id,
                    r.product_class AS product_class,
                    r.vendor        AS vendor,
                    r.technology    AS technology,
                    r.sheet_name    AS sheet_name,
                    {CATEGORY_CYPHER},
                    rel.label       AS label,
                    rel.band        AS band,
                    rel.editable    AS editable,
                    p.path          AS path
                ORDER BY category, rel.label
            """, {"word": word})
            if rows:
                break

    if not rows:
        raise ValueError(
            f"No router found matching '{model}'. "
            f"Call list_routers to see all available models."
        )

    meta = rows[0]
    categories: dict[str, list] = {}
    for row in rows:
        cat = row["category"] or infer_category(row.get("label", ""), row.get("path", ""))
        categories.setdefault(cat, []).append({
            "label":    row["label"],
            "path":     row["path"],
            "band":     row["band"] or "all",
            "editable": row["editable"] or "unknown",
        })

    return {
        "router_id":     meta["router_id"],
        "product_class": meta["product_class"],
        "vendor":        meta["vendor"],
        "technology":    meta["technology"],
        "sheet_name":    meta["sheet_name"],
        "total_params":  len(rows),
        "categories":    categories,
    }


def tool_get_router_by_category(model: str, category: str) -> dict:
    if not model:
        raise ValueError("'model' is required.")
    if not category:
        raise ValueError("'category' is required.")

    rows = db(f"""
        MATCH (r:Router)-[rel:HAS_PARAM]->(p:Parameter)
        WHERE (
            toLower(r.id)     CONTAINS toLower($model)
         OR toLower(r.product_class) CONTAINS toLower($model)
         OR toLower(r.sheet_name)    CONTAINS toLower($model)
        )
        WITH r, rel, p,
            {CATEGORY_CYPHER}
        WHERE toLower(category) = toLower($category)
        RETURN
            r.id  AS router_id,
            category,
            rel.label    AS label,
            rel.band     AS band,
            rel.editable AS editable,
            p.path       AS path
        ORDER BY rel.label
    """, {"model": model, "category": category})

    if not rows:
        raise ValueError(
            f"No parameters found for model '{model}' in category '{category}'. "
            f"Valid: basic_info, ppp_info, voice_info, wifi_info, diagnostic, lan."
        )

    return {
        "router_id": rows[0]["router_id"],
        "category":  category,
        "total":     len(rows),
        "params": [
            {
                "label":    r["label"],
                "path":     r["path"],
                "band":     r["band"] or "all",
                "editable": r["editable"] or "unknown",
            }
            for r in rows
        ]
    }


def tool_search_parameter(keyword: str) -> dict:
    if not keyword:
        raise ValueError("'keyword' is required.")

    rows = db("""
        MATCH (r:Router)-[rel:HAS_PARAM]->(p:Parameter)
        WHERE toLower(coalesce(p.path, ""))    CONTAINS toLower($keyword)
           OR toLower(coalesce(rel.label, "")) CONTAINS toLower($keyword)
        RETURN
            r.id     AS router_id,
            r.product_class AS product_class,
            r.sheet_name    AS sheet_name,
            r.vendor        AS vendor,
            rel.category    AS category,
            rel.label       AS label,
            rel.band        AS band,
            p.path          AS path
        ORDER BY r.id, rel.category
    """, {"keyword": keyword})

    if not rows:
        raise ValueError(f"No parameters found matching '{keyword}'.")

    by_router: dict[str, list] = {}
    for row in rows:
        key = (row.get("router_id") or row.get("product_class")
               or row.get("sheet_name") or row.get("vendor") or "unknown")
        cat = row.get("category") or infer_category(row.get("label", ""), row.get("path", ""))
        by_router.setdefault(key, []).append({
            "label":    row["label"],
            "path":     row["path"],
            "band":     row["band"] or "all",
            "category": cat,
            "vendor":   row["vendor"],
        })

    return {
        "keyword":       keyword,
        "total_matches": len(rows),
        "routers_count": len(by_router),
        "results":       by_router,
    }


def tool_search_by_technology(technology: str) -> dict:
    if not technology:
        raise ValueError("'technology' is required. Examples: 'GPON', 'ADSL', 'VDSL', 'DSL'.")

    rows = cached_db(f"tech_{technology.upper()}", """
        MATCH (r:Router)
        WHERE toLower(r.technology) CONTAINS toLower($technology)
        RETURN
            r.id     AS router_id,
            r.product_class AS product_class,
            r.vendor        AS vendor,
            r.technology    AS technology,
            r.sheet_name    AS sheet_name
        ORDER BY r.vendor, r.id
    """, {"technology": technology})

    if not rows:
        raise ValueError(
            f"No routers found with technology '{technology}'. "
            f"Call list_routers to see all available routers."
        )

    return {
        "technology": technology,
        "total":      len(rows),
        "routers":    rows,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"\n  Router MCP Server v1.2")
    print(f"  Neo4j  : {NEO4J_URI}")
    print(f"  Listen : http://{SERVER_HOST}:{SERVER_PORT}")
    print(f"  Docs   : http://localhost:{SERVER_PORT}/docs")
    print(f"  Tools  : http://localhost:{SERVER_PORT}/tools\n")
    get_driver()
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)