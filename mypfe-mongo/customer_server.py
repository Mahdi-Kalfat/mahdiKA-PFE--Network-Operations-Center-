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
import random
import re
import string
import contextlib
from datetime import datetime, timedelta, timezone

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
    if not isinstance(value, datetime):
        return value
    # Values are stored via datetime.utcnow(), which is naive — isoformat() on
    # a naive datetime omits the UTC suffix, so the browser's `new Date(...)`
    # parses it as local time instead of UTC and renders it an hour (or more)
    # off. Stamp it as UTC before formatting so the client converts correctly.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


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
    cust.pop("email_verify_code", None)
    cust.pop("email_verify_expires", None)
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


def _clean_notification(doc: dict) -> dict:
    doc = dict(doc)
    _id = doc.pop("_id", None)
    if _id is not None:
        doc["id"] = str(_id)
    doc["created_at"] = _iso(doc.get("created_at"))
    return doc


def logic_create_notification(account_id: str, customer_name: str, fault: str,
                              fault_label_en: str, fault_label_fr: str) -> dict:
    """Persist a proactive fault notification for a customer account."""
    doc = {
        "account_id":     account_id,
        "customer_name":  customer_name,
        "fault":          fault,
        "fault_label_en": fault_label_en,
        "fault_label_fr": fault_label_fr,
        "read":           False,
        "created_at":     datetime.utcnow(),
    }
    res = db.notifications.insert_one(doc)
    doc["_id"] = res.inserted_id
    return {"success": True, "notification": _clean_notification(doc)}


def logic_get_notifications(account_id: str) -> dict:
    """List a customer's notifications, newest first."""
    items = [_clean_notification(n) for n in
             db.notifications.find({"account_id": account_id})
             .sort("created_at", -1).limit(50)]
    return {"account_id": account_id, "total": len(items),
            "unread": sum(1 for n in items if not n["read"]),
            "notifications": items}


def logic_mark_notification_read(notification_id: str) -> dict:
    from bson import ObjectId
    from bson.errors import InvalidId
    try:
        oid = ObjectId(notification_id)
    except InvalidId:
        return {"success": False, "error": "Invalid notification id"}
    res = db.notifications.update_one({"_id": oid}, {"$set": {"read": True}})
    return {"success": res.matched_count > 0}


def logic_delete_notification(notification_id: str, account_id: str) -> dict:
    from bson import ObjectId
    from bson.errors import InvalidId
    try:
        oid = ObjectId(notification_id)
    except InvalidId:
        return {"success": False, "error": "Invalid notification id"}
    res = db.notifications.delete_one({"_id": oid, "account_id": account_id})
    return {"success": res.deleted_count > 0}


def logic_get_customer_profile(account_id: str) -> dict:
    """Minimal profile lookup (name + verified email) for server-to-server use,
    e.g. app.py deciding whether to email a proactive fault notification."""
    cust = db.customers.find_one({"account_id": account_id},
                                  {"_id": 0, "name": 1, "email": 1, "email_verified": 1})
    if not cust:
        return {"error": "Customer not found"}
    return {
        "account_id":     account_id,
        "name":           cust.get("name", ""),
        "email":          cust.get("email"),
        "email_verified": bool(cust.get("email_verified")),
    }


def _gen_email_code() -> str:
    return "".join(random.choices(string.digits, k=6))


def logic_set_customer_email(account_id: str, email: str) -> dict:
    """Save a customer's email and issue a fresh 10-minute verification code."""
    cust = db.customers.find_one({"account_id": account_id})
    if not cust:
        return {"success": False, "error": "Customer not found"}
    code = _gen_email_code()
    db.customers.update_one({"account_id": account_id}, {"$set": {
        "email":               email,
        "email_verified":      False,
        "email_verify_code":   code,
        "email_verify_expires": datetime.utcnow() + timedelta(minutes=10),
    }})
    return {"success": True, "email": email, "code": code}


def logic_verify_customer_email(account_id: str, code: str) -> dict:
    """Confirm the verification code sent to a customer's email."""
    cust = db.customers.find_one({"account_id": account_id})
    if not cust:
        return {"success": False, "error": "Customer not found"}
    if not cust.get("email"):
        return {"success": False, "error": "No email on file"}
    expires = cust.get("email_verify_expires")
    if not expires or datetime.utcnow() > expires:
        return {"success": False, "error": "Code expired — please resend"}
    if str(cust.get("email_verify_code") or "") != str(code or "").strip():
        return {"success": False, "error": "Incorrect code"}
    db.customers.update_one({"account_id": account_id}, {
        "$set":   {"email_verified": True},
        "$unset": {"email_verify_code": "", "email_verify_expires": ""},
    })
    return {"success": True}


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


def logic_search_customers(q: str = None, limit: int = 50) -> dict:
    query = {}
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        query["$or"] = [{"name": rx}, {"phone": rx}, {"email": rx}, {"account_id": rx}]
    customers = list(db.customers.find(query, {"_id": 0, "pin_hash": 0}).limit(int(limit)))
    return {"total": len(customers), "customers": customers}


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


# -- EMAIL VERIFICATION ROUTES (mandatory first-login gate — REST) -------------

@app.get("/customer/profile")
def rest_get_customer_profile(account_id: str):
    return logic_get_customer_profile(account_id)


@app.post("/customer/email")
async def rest_set_customer_email(request: Request):
    body = await request.json()
    return logic_set_customer_email(body.get("account_id", ""), (body.get("email") or "").strip())


@app.post("/customer/email/verify")
async def rest_verify_customer_email(request: Request):
    body = await request.json()
    return logic_verify_customer_email(body.get("account_id", ""), body.get("code", ""))


# -- NOTIFICATION ROUTES (customer proactive-fault bell — REST) ----------------

@app.post("/notifications/create")
async def rest_create_notification(request: Request):
    body = await request.json()
    return logic_create_notification(
        body.get("account_id", ""), body.get("customer_name", ""),
        body.get("fault", ""), body.get("fault_label_en", ""),
        body.get("fault_label_fr", ""),
    )


@app.get("/notifications")
def rest_get_notifications(account_id: str):
    return logic_get_notifications(account_id)


@app.post("/notifications/{notification_id}/read")
def rest_mark_notification_read(notification_id: str):
    return logic_mark_notification_read(notification_id)


@app.delete("/notifications/{notification_id}")
def rest_delete_notification(notification_id: str, account_id: str):
    return logic_delete_notification(notification_id, account_id)


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


@app.get("/admin/customers/search")
def admin_customers_search(q: str = None, limit: int = 50):
    return logic_search_customers(q, limit)


if __name__ == "__main__":
    import uvicorn
    print(f"\n  Customer MCP Server (real MCP)")
    print(f"  MongoDB : {MONGO_URI} / {MONGO_DB}")
    print(f"  Listen  : http://0.0.0.0:{SERVER_PORT}")
    print(f"  MCP     : http://localhost:{SERVER_PORT}/mcp/   (tools/list, tools/call)")
    print(f"  REST    : http://localhost:{SERVER_PORT}/tools/authenticate_customer")
    print(f"  Admin   : http://localhost:{SERVER_PORT}/admin/tickets\n")
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
