"""
genie_server.py  —  GenieACS MCP Server (path-keyed, graph-aligned)

Read side of the simulated ACS. What changed vs v2:

* Router state in Mongo is keyed by REAL TR-069 paths. get_router_state now
  returns `parameters` (path-keyed, the real device tree) AND a convenience
  `parameters_named` (role -> value) so dashboards stay human-readable and the
  jury can see the path<->role mapping live.
* status is re-derived from the parameters on every read (UP iff no fault).
* NOC fault injection / clear write the role-based fault overlay expanded to
  this model's real paths (mcp_engine), so injected faults live at real paths
  too — the same ones RaDuce will later resolve and fix.

Endpoints unchanged: MCP /mcp/, REST /tools/<name>, NOC /noc/..., UTCP /tools.
Port 8001.
"""

import os
import contextlib
import random
from datetime import datetime

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient

from mcp.server.fastmcp import FastMCP

import mcp_engine as E

load_dotenv()

MONGO_URI   = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB    = os.getenv("MONGO_DB",  "mypfe")
SERVER_PORT = int(os.getenv("GENIE_PORT", "8001"))
BASE_URL    = os.getenv("GENIE_BASE_URL", f"http://localhost:{SERVER_PORT}")
NEO4J_API   = os.getenv("NEO4J_API",   "http://neo4j-agent:8000")

client   = MongoClient(MONGO_URI)
db       = client[MONGO_DB]
resolver = E.Resolver(NEO4J_API)

_router_meta_cache: dict[str, dict] = {}

# Faults a NOC operator may inject from the dashboard.
INJECTABLE_FAULTS = ["ppp_auth_failure", "wrong_vlan", "dns_failure",
                     "random_disconnect", "weak_signal"]


# -- SHARED HELPERS ------------------------------------------------------------

def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _named_view(model: str, params: dict) -> dict:
    """role -> value, derived from the path-keyed params (display convenience)."""
    out = {}
    for role, path in resolver.roles_for(model).items():
        if path in params:
            out[role] = params[path]
    return out


def _lookup_router_meta(model: str) -> dict:
    model = (model or "").strip()
    if not model:
        return {}
    if model in _router_meta_cache:
        return _router_meta_cache[model]
    try:
        response = requests.get(f"{NEO4J_API}/tools/list_routers", timeout=10)
        response.raise_for_status()
        routers = (response.json() or {}).get("routers") or []
        needle = model.lower()
        for router in routers:
            values = [str(router.get("id", "")).lower(),
                      str(router.get("product_class", "")).lower(),
                      str(router.get("sheet_name", "")).lower()]
            if any(needle in value for value in values):
                _router_meta_cache[model] = router
                return router
    except Exception:
        pass
    return {}


def flatten_parameters(parameters: dict, prefix: str = "") -> dict:
    """Flatten nested parameter dicts into a flat path-keyed map."""
    flat = {}
    for key, value in parameters.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(flatten_parameters(value, path))
        else:
            flat[path] = value
    return flat


def _lookup_router_serial(account_id: str) -> str:
    if not account_id:
        return ""
    state = db.router_states.find_one({"account_id": account_id}, {"_id": 0, "serial": 1})
    return (state or {}).get("serial", "")


def _enrich_customer_profile(cust: dict) -> dict:
    profile = dict(cust)
    if "plan" in profile and "subscription_plan" not in profile:
        profile["subscription_plan"] = profile.pop("plan")
    profile["subscription"]  = profile.get("subscription_plan", "")
    profile["router_serial"] = _lookup_router_serial(profile.get("account_id", ""))
    router_meta = _lookup_router_meta(profile.get("router_model", ""))
    if router_meta:
        profile["vendor"]     = router_meta.get("vendor", profile.get("vendor", ""))
        profile["technology"] = router_meta.get("technology", profile.get("technology", ""))
    profile["graph_rag_log"] = {
        "source": "Neo4j GraphRAG",
        "router_model": profile.get("router_model", ""),
        "router_meta": {
            "id": router_meta.get("id"),
            "product_class": router_meta.get("product_class"),
            "vendor": router_meta.get("vendor"),
            "technology": router_meta.get("technology"),
            "sheet_name": router_meta.get("sheet_name"),
        } if router_meta else {},
        "router_serial": profile.get("router_serial", ""),
        "subscription_plan": profile.get("subscription_plan", ""),
    }
    return profile


# -- TOOL LOGIC ----------------------------------------------------------------

def logic_get_router_state(serial: str = "", account_id: str = "") -> dict:
    """Read live TR-069 state (path-keyed) + a readable role view + derived status."""
    if not serial and not account_id:
        return {"error": "Provide serial or account_id"}
    query = {"serial": serial} if serial else {"account_id": account_id}
    state = db.router_states.find_one(query, {"_id": 0, "customer_id": 0})
    if not state:
        return {"error": f"Router not found for {query}"}
    model  = state.get("model", "")
    params = flatten_parameters(state.get("parameters", {}))
    # Re-derive so status is always consistent with the parameters.
    state["status"]            = E.derive_status(model, params, resolver)
    state["fault"]             = E.diagnose(model, params, resolver)["fault"]
    state["parameters_named"]  = _named_view(model, params)
    state["last_updated"]      = _iso(state.get("last_updated"))
    state["parameters"]        = params
    return state


def logic_get_customer_by_phone(phone: str) -> dict:
    cust = db.customers.find_one({"phone": phone}, {"_id": 0, "pin_hash": 0})
    if not cust:
        return {"error": f"No customer found with phone {phone}"}
    cust["created_at"] = _iso(cust.get("created_at"))
    return _enrich_customer_profile(cust)


def logic_get_fault_history(account_id: str) -> dict:
    cust = db.customers.find_one({"account_id": account_id}, {"_id": 0, "pin_hash": 0})
    if not cust:
        return {"error": f"No customer found with account_id {account_id}"}
    tickets = list(db.tickets.find({"account_id": account_id}, {"_id": 0})
                   .sort("created_at", -1).limit(10))
    for t in tickets:
        t["created_at"]  = _iso(t.get("created_at"))
        t["resolved_at"] = _iso(t.get("resolved_at"))
    return {"account_id": account_id, "total": len(tickets), "tickets": tickets}


# -- MCP SERVER ----------------------------------------------------------------

mcp = FastMCP("genie-acs", stateless_http=True, json_response=True,
              streamable_http_path="/", host="0.0.0.0")


@mcp.tool()
def get_router_state(serial: str = "", account_id: str = "") -> dict:
    """Read the current live TR-069 state of a customer's router from the ACS.
    Returns parameters keyed by real TR-069 path, a readable role view, and the
    derived UP/DOWN status. Provide a serial OR an account_id."""
    return logic_get_router_state(serial, account_id)


@mcp.tool()
def get_customer_by_phone(phone: str) -> dict:
    """Look up a customer profile by phone number (name, account, router model,
    serial, technology, plan)."""
    return logic_get_customer_by_phone(phone)


@mcp.tool()
def get_fault_history(account_id: str) -> dict:
    """Get the history of past faults and resolutions for a customer's router."""
    return logic_get_fault_history(account_id)


# -- FASTAPI APP ---------------------------------------------------------------

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="GenieACS MCP Server", version="3.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/mcp", mcp.streamable_http_app())


def build_manual(base_url: str) -> dict:
    return {
        "utcp_version": "1.0.0", "manual_version": "3.0.0",
        "tools": [
            {"name": "get_router_state",
             "description": "Read the current live state of a customer's router from the ACS.",
             "inputs": {"type": "object", "properties": {
                 "serial": {"type": "string"}, "account_id": {"type": "string"}}, "required": []},
             "outputs": {"type": "object"}, "tags": ["router", "state", "genie", "acs"],
             "tool_call_template": {"call_template_type": "http", "http_method": "GET",
                 "url": f"{base_url}/tools/get_router_state", "content_type": "application/json"}},
            {"name": "get_customer_by_phone",
             "description": "Look up a customer profile by phone number.",
             "inputs": {"type": "object", "properties": {
                 "phone": {"type": "string"}}, "required": ["phone"]},
             "outputs": {"type": "object"}, "tags": ["customer", "genie"],
             "tool_call_template": {"call_template_type": "http", "http_method": "GET",
                 "url": f"{base_url}/tools/get_customer_by_phone", "content_type": "application/json"}},
            {"name": "get_fault_history",
             "description": "Get the history of past faults for a customer's router.",
             "inputs": {"type": "object", "properties": {
                 "account_id": {"type": "string"}}, "required": ["account_id"]},
             "outputs": {"type": "object"}, "tags": ["history", "genie"],
             "tool_call_template": {"call_template_type": "http", "http_method": "GET",
                 "url": f"{base_url}/tools/get_fault_history", "content_type": "application/json"}},
        ],
    }


@app.get("/health")
def health():
    try:
        db.command("ping")
        routers = db.router_states.count_documents({})
        down    = db.router_states.count_documents({"status": "DOWN"})
        return {"status": "ok", "mongodb": "connected",
                "routers": routers, "down": down, "up": routers - down}
    except Exception as e:
        return {"status": "error", "mongodb": str(e)}


@app.get("/tools")
def list_tools(request: Request):
    host   = request.headers.get("host", f"localhost:{SERVER_PORT}")
    scheme = request.headers.get("x-forwarded-proto", "http")
    base   = os.getenv("GENIE_BASE_URL") or f"{scheme}://{host}"
    return build_manual(base)


@app.get("/tools/get_router_state")
def rest_get_router_state(serial: str = None, account_id: str = None):
    return logic_get_router_state(serial or "", account_id or "")


@app.get("/tools/get_customer_by_phone")
def rest_get_customer_by_phone(phone: str):
    return logic_get_customer_by_phone(phone)


@app.get("/tools/get_fault_history")
def rest_get_fault_history(account_id: str):
    return logic_get_fault_history(account_id)


# -- NOC admin (fault injection writes path-keyed state) -----------------------

@app.post("/noc/inject_fault")
async def inject_fault(request: Request):
    body   = await request.json()
    serial = body.get("serial")
    fault  = body.get("fault") or random.choice(INJECTABLE_FAULTS)
    if fault not in E.FAULTS:
        return {"error": f"Unknown fault: {fault}. Options: {list(E.FAULTS.keys())}"}
    state = db.router_states.find_one({"serial": serial})
    if not state:
        return {"error": f"Router {serial} not found"}
    model  = state.get("model", "")
    params = E.expand_state(model, fault, resolver)   # path-keyed overlay
    status = E.derive_status(model, params, resolver)
    db.router_states.update_one({"serial": serial}, {"$set": {
        "status": status, "fault": fault,
        "last_updated": datetime.utcnow(), "parameters": params,
    }})
    return {"success": True, "serial": serial, "fault_injected": fault, "status": status}


@app.post("/noc/clear_fault")
async def clear_fault(request: Request):
    body   = await request.json()
    serial = body.get("serial")
    state  = db.router_states.find_one({"serial": serial})
    if not state:
        return {"error": f"Router {serial} not found"}
    model  = state.get("model", "")
    params = E.expand_state(model, "healthy", resolver)
    db.router_states.update_one({"serial": serial}, {"$set": {
        "status": "UP", "fault": "healthy",
        "last_updated": datetime.utcnow(), "parameters": params,
    }})
    return {"success": True, "serial": serial, "status": "UP"}


@app.get("/noc/fleet")
def get_fleet():
    pipeline = [
        {"$lookup": {"from": "customers", "localField": "customer_id",
                     "foreignField": "_id", "as": "customer"}},
        {"$unwind": "$customer"},
        {"$project": {"_id": 0, "serial": 1, "model": 1, "status": 1, "fault": 1,
                      "last_updated": 1, "customer_name": "$customer.name",
                      "customer_phone": "$customer.phone", "account_id": "$customer.account_id"}},
    ]
    fleet = list(db.router_states.aggregate(pipeline))
    for r in fleet:
        r["last_updated"] = _iso(r.get("last_updated"))
    up   = sum(1 for r in fleet if r["status"] == "UP")
    down = sum(1 for r in fleet if r["status"] == "DOWN")
    return {"total": len(fleet), "up": up, "down": down, "routers": fleet}


if __name__ == "__main__":
    import uvicorn
    print(f"\n  GenieACS MCP Server v3 (path-keyed, graph-aligned)")
    print(f"  MongoDB : {MONGO_URI} / {MONGO_DB}")
    print(f"  Listen  : http://0.0.0.0:{SERVER_PORT}\n")
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
