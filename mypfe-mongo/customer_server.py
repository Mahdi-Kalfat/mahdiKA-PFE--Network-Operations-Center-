"""
customer_server.py  —  Customer MCP Server (real Model Context Protocol)

Customer authentication (phone + PIN), support tickets, and NOC admin auth.
Exposes the customer-facing operations as REAL MCP tools (JSON-RPC over the
streamable-HTTP transport) and mirrors them on REST for backward compatibility.
Admin endpoints stay REST (they are NOC dashboard operations, not agent tools).

Endpoints
    MCP   : http://<host>:8003/mcp/          <- genuine MCP (tools/list, tools/call)
    REST  : http://<host>:8003/tools/<name>  <- backward-compat for mypfe-app
    ADMIN : http://<host>:8003/admin/...      <- NOC auth + ticket browse (REST)
    UTCP  : http://<host>:8003/tools          <- legacy UTCP manual

Run:
    python customer_server.py
Port: 8003
"""

import os
import contextlib
from datetime import datetime

import bcrypt
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient

from mcp.server.fastmcp import FastMCP

load_dotenv()

MONGO_URI   = os.getenv("MONGO_URI",         "mongodb://localhost:27017")
MONGO_DB    = os.getenv("MONGO_DB",          "mypfe")
SERVER_PORT = int(os.getenv("CUSTOMER_PORT", "8003"))
BASE_URL    = os.getenv("CUSTOMER_BASE_URL", f"http://localhost:{SERVER_PORT}")
NEO4J_API   = os.getenv("NEO4J_API",         "http://localhost:8000")

client = MongoClient(MONGO_URI)
db     = client[MONGO_DB]

_router_meta_cache: dict[str, dict] = {}


# -- HELPERS -------------------------------------------------------------------

def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def _check_pw(plain: str, hashed) -> bool:
    if not hashed:
        return False
    if isinstance(hashed, str):
        hashed = hashed.encode()
    try:
        return bcrypt.checkpw(plain.encode(), hashed)
    except Exception:
        return False


def _clean_ticket(doc: dict) -> dict:
    doc = dict(doc)
    _id = doc.pop("_id", None)
    if _id is not None:
        doc["id"] = str(_id)
    doc.pop("customer_id", None)
    doc["created_at"]  = _iso(doc.get("created_at"))
    doc["resolved_at"] = _iso(doc.get("resolved_at"))
    return doc


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
            values = [
                str(router.get("id", "")).lower(),
                str(router.get("router_id", "")).lower(),
                str(router.get("product_class", "")).lower(),
                str(router.get("sheet_name", "")).lower(),
            ]
            if any(needle in value for value in values):
                normalized = dict(router)
                normalized["id"] = (
                    router.get("id")
                    or router.get("router_id")
                    or router.get("product_class")
                    or router.get("sheet_name")
                    or model
                )
                _router_meta_cache[model] = normalized
                return normalized
    except Exception:
        pass

    return {}


def _lookup_router_serial(account_id: str) -> str:
    if not account_id:
        return ""
    state = db.router_states.find_one({"account_id": account_id}, {"_id": 0, "serial": 1})
    return (state or {}).get("serial", "")


def _enrich_customer_profile(cust: dict) -> dict:
    profile = dict(cust)

    subscription_plan = profile.get("subscription_plan") or profile.pop("plan", None)
    if subscription_plan:
        profile["subscription_plan"] = subscription_plan
        profile["subscription"] = subscription_plan

    profile["router_serial"] = _lookup_router_serial(profile.get("account_id", ""))

    profile["graph_rag_log"] = {
        "source": "Neo4j GraphRAG",
        "router_model": profile.get("router_model", ""),
        "router_meta": {
            "id": _lookup_router_meta(profile.get("router_model", "")).get("id"),
            "product_class": _lookup_router_meta(profile.get("router_model", "")).get("product_class"),
            "sheet_name": _lookup_router_meta(profile.get("router_model", "")).get("sheet_name"),
        },
        "router_serial": profile.get("router_serial", ""),
        "subscription_plan": profile.get("subscription_plan", ""),
    }
    return profile


# -- SHARED TOOL LOGIC (single source of truth for MCP + REST) -----------------

def logic_authenticate_customer(phone: str, pin: str) -> dict:
    """Authenticate a customer by phone + PIN. Returns the profile on success."""
    cust = db.customers.find_one({"phone": phone})
    if not cust:
        return {"authenticated": False, "error": "No account found for this phone number"}
    if not _check_pw(pin, cust.get("pin_hash")):
        return {"authenticated": False, "error": "Incorrect phone or PIN"}
    cust.pop("_id", None)
    cust.pop("pin_hash", None)
    cust["created_at"] = _iso(cust.get("created_at"))
    cust["authenticated"] = True
    return _enrich_customer_profile(cust)


def logic_create_ticket(account_id: str, problem_description: str,
                        fault_detected: str, status: str, **extra) -> dict:
    """Create a support ticket. Returns ticket_id + short_id."""
    cust = db.customers.find_one({"account_id": account_id})
    doc = {
        "account_id":          account_id,
        "customer_id":         cust["_id"] if cust else None,
        "problem_description": problem_description,
        "fault_detected":      fault_detected,
        "status":              status,
        "created_at":          datetime.utcnow(),
        "resolved_at":         datetime.utcnow() if status == "resolved" else None,
    }
    doc.update({k: v for k, v in extra.items() if v not in (None, "", 0)})
    res = db.tickets.insert_one(doc)
    short_id = str(res.inserted_id)[-6:].upper()
    db.tickets.update_one({"_id": res.inserted_id}, {"$set": {"short_id": short_id}})
    return {"success": True, "ticket_id": str(res.inserted_id),
            "short_id": short_id, "status": status}


def logic_get_customer_tickets(account_id: str) -> dict:
    """List recent tickets for a customer."""
    tickets = list(
        db.tickets.find({"account_id": account_id})
        .sort("created_at", -1)
        .limit(50)
    )
    return {"account_id": account_id, "total": len(tickets),
            "tickets": [_clean_ticket(t) for t in tickets]}


def logic_admin_authenticate(username: str, password: str) -> dict:
    adm = db.admins.find_one({"username": username})
    if not adm or not _check_pw(password, adm.get("password_hash")):
        return {"authenticated": False, "error": "Invalid username or password"}
    return {"authenticated": True, "username": username, "role": adm.get("role", "admin")}


def logic_admin_tickets(status: str = None, fault: str = None,
                        account: str = None, limit: int = 50) -> dict:
    query = {}
    if status:  query["status"]         = status
    if fault:   query["fault_detected"] = fault
    if account: query["account_id"]     = account
    tickets = list(db.tickets.find(query).sort("created_at", -1).limit(int(limit)))
    return {"total": len(tickets), "tickets": [_clean_ticket(t) for t in tickets]}


def logic_admin_ticket_detail(ticket_id: str) -> dict:
    from bson import ObjectId
    doc = None
    try:
        doc = db.tickets.find_one({"_id": ObjectId(ticket_id)})
    except Exception:
        doc = None
    if not doc:
        doc = db.tickets.find_one({"short_id": ticket_id})
    if not doc:
        return {"error": f"Ticket {ticket_id} not found"}
    return _clean_ticket(doc)


# -- MCP SERVER ----------------------------------------------------------------

mcp = FastMCP(
    "customer",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    host="0.0.0.0",
)


@mcp.tool()
def authenticate_customer(phone: str, pin: str) -> dict:
    """Authenticate a customer by phone number and PIN. On success returns the
    customer profile (name, account_id, router_model, router_serial, technology,
    plan) with authenticated=true; on failure returns authenticated=false."""
    return logic_authenticate_customer(phone, pin)


@mcp.tool()
def get_customer_tickets(account_id: str) -> dict:
    """List the recent support tickets for a customer account."""
    return logic_get_customer_tickets(account_id)


@mcp.tool()
def create_ticket(account_id: str, problem_description: str, fault_detected: str,
                  status: str, fix_applied: str = "", escalation_reason: str = "",
                  resolution_time_s: float = 0, customer_name: str = "",
                  router_model: str = "", router_serial: str = "") -> dict:
    """Create a support ticket for a customer. Returns the ticket id and a short
    human-friendly id. Status is typically open, resolved, or escalated."""
    return logic_create_ticket(
        account_id, problem_description, fault_detected, status,
        fix_applied=fix_applied, escalation_reason=escalation_reason,
        resolution_time_s=resolution_time_s, customer_name=customer_name,
        router_model=router_model, router_serial=router_serial,
    )


# -- FASTAPI APP (REST + admin + mounts the MCP app) ---------------------------

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Customer MCP Server", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/mcp", mcp.streamable_http_app())


# -- Legacy UTCP manual --------------------------------------------------------

def build_manual(base_url: str) -> dict:
    def tool(name, desc, props, required, tags, method="GET"):
        return {"name": name, "description": desc,
                "inputs": {"type": "object", "properties": props, "required": required},
                "outputs": {"type": "object"}, "tags": tags,
                "tool_call_template": {"call_template_type": "http", "http_method": method,
                    "url": f"{base_url}/tools/{name}", "content_type": "application/json"}}
    return {
        "utcp_version": "1.0.0",
        "manual_version": "2.0.0",
        "tools": [
            tool("authenticate_customer", "Authenticate a customer by phone + PIN.",
                 {"phone": {"type": "string"}, "pin": {"type": "string"}},
                 ["phone", "pin"], ["customer", "auth", "login"]),
            tool("get_customer_tickets", "List recent tickets for a customer.",
                 {"account_id": {"type": "string"}}, ["account_id"],
                 ["customer", "tickets", "history"]),
            tool("create_ticket", "Create a support ticket.",
                 {"account_id": {"type": "string"}, "problem_description": {"type": "string"},
                  "fault_detected": {"type": "string"}, "status": {"type": "string"}},
                 ["account_id", "problem_description", "fault_detected", "status"],
                 ["customer", "ticket", "create"], method="POST"),
        ],
    }


# -- REST ROUTES (unchanged contract for mypfe-app) ----------------------------

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok", "mongodb": "connected",
                "customers": db.customers.count_documents({}),
                "tickets": db.tickets.count_documents({})}
    except Exception as e:
        return {"status": "error", "mongodb": str(e)}


@app.get("/tools")
def list_tools(request: Request):
    host   = request.headers.get("host", f"localhost:{SERVER_PORT}")
    scheme = request.headers.get("x-forwarded-proto", "http")
    base   = os.getenv("CUSTOMER_BASE_URL") or f"{scheme}://{host}"
    return build_manual(base)


@app.get("/tools/authenticate_customer")
def rest_authenticate_customer(phone: str, pin: str):
    return logic_authenticate_customer(phone, pin)


@app.get("/tools/get_customer_tickets")
def rest_get_customer_tickets(account_id: str):
    return logic_get_customer_tickets(account_id)


@app.post("/tools/create_ticket")
async def rest_create_ticket(request: Request):
    body = await request.json()
    account_id = body.pop("account_id", None)
    problem    = body.pop("problem_description", "")
    fault      = body.pop("fault_detected", "")
    status     = body.pop("status", "open")
    return logic_create_ticket(account_id, problem, fault, status, **body)


# -- ADMIN ROUTES (NOC dashboard — REST) ---------------------------------------

@app.get("/admin/authenticate")
def admin_authenticate(username: str, password: str):
    return logic_admin_authenticate(username, password)


@app.get("/admin/tickets")
def admin_tickets(status: str = None, fault: str = None,
                  account: str = None, limit: int = 50):
    return logic_admin_tickets(status, fault, account, limit)


@app.get("/admin/ticket/{ticket_id}")
def admin_ticket_detail(ticket_id: str):
    return logic_admin_ticket_detail(ticket_id)


if __name__ == "__main__":
    import uvicorn
    print(f"\n  Customer MCP Server (real MCP)")
    print(f"  MongoDB : {MONGO_URI} / {MONGO_DB}")
    print(f"  Listen  : http://0.0.0.0:{SERVER_PORT}")
    print(f"  MCP     : http://localhost:{SERVER_PORT}/mcp/   (tools/list, tools/call)")
    print(f"  REST    : http://localhost:{SERVER_PORT}/tools/authenticate_customer")
    print(f"  Admin   : http://localhost:{SERVER_PORT}/admin/tickets\n")
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
