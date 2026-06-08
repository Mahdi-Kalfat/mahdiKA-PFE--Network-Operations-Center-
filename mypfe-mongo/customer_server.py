"""
customer_server.py  —  Customer MCP Server
Handles authentication (phone + PIN) and ticket creation/update.

Run:
    python customer_server.py

Port: 8003
"""

import os
import bcrypt
from datetime import datetime
from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient

load_dotenv()

MONGO_URI   = os.getenv("MONGO_URI",          "mongodb://localhost:27017")
MONGO_DB    = os.getenv("MONGO_DB",           "mypfe")
SERVER_PORT = int(os.getenv("CUSTOMER_PORT",  "8003"))
BASE_URL    = os.getenv("CUSTOMER_BASE_URL",  f"http://localhost:{SERVER_PORT}")

client = MongoClient(MONGO_URI)
db     = client[MONGO_DB]

app = FastAPI(title="Customer MCP Server", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── UTCP MANUAL ───────────────────────────────────────────────────────────────

def build_manual(base_url: str) -> dict:
    return {
        "utcp_version": "1.0.0",
        "manual_version": "1.0.0",
        "tools": [
            {
                "name": "authenticate_customer",
                "description": (
                    "Verify a customer's identity using their phone number and PIN. "
                    "Call this first in every session before accessing any router data. "
                    "Returns customer profile if authentication succeeds, error if not."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "phone": {"type": "string", "description": "Customer phone number"},
                        "pin":   {"type": "string", "description": "4-digit PIN code"}
                    },
                    "required": ["phone", "pin"]
                },
                "outputs": {"type": "object"},
                "tags": ["auth", "customer", "login", "security"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/authenticate_customer",
                    "content_type": "application/json"
                }
            },
            {
                "name": "create_ticket",
                "description": (
                    "Create a support ticket after resolving or escalating a customer issue. "
                    "Always call this at the END of a support session. "
                    "Include the full agent trace (list of steps taken) and the final status."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "account_id":           {"type": "string"},
                        "problem_description":  {"type": "string"},
                        "fault_detected":       {"type": "string"},
                        "fix_applied":          {"type": "string"},
                        "status":               {"type": "string", "enum": ["resolved", "escalated", "open"]},
                        "agent_trace":          {"type": "array", "items": {"type": "string"}},
                        "escalation_reason":    {"type": "string"}
                    },
                    "required": ["account_id", "problem_description", "fault_detected", "status"]
                },
                "outputs": {"type": "object"},
                "tags": ["ticket", "create", "log", "customer"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/create_ticket",
                    "content_type": "application/json"
                }
            },
            {
                "name": "get_customer_tickets",
                "description": "Get ticket history for a customer.",
                "inputs": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "Customer account ID"}
                    },
                    "required": ["account_id"]
                },
                "outputs": {"type": "object"},
                "tags": ["ticket", "history", "customer"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/get_customer_tickets",
                    "content_type": "application/json"
                }
            }
        ]
    }


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok", "mongodb": "connected",
                "customers": db.customers.count_documents({})}
    except Exception as e:
        return {"status": "error", "mongodb": str(e)}


@app.get("/tools")
def list_tools(request: Request):
    host   = request.headers.get("host", f"localhost:{SERVER_PORT}")
    scheme = request.headers.get("x-forwarded-proto", "http")
    base   = os.getenv("CUSTOMER_BASE_URL") or f"{scheme}://{host}"
    return build_manual(base)


@app.get("/tools/authenticate_customer")
def authenticate_customer(phone: str, pin: str):
    print(f"[customer] authenticate: phone={phone}")
    cust = db.customers.find_one({"phone": phone})
    if not cust:
        return {"authenticated": False, "error": "Phone number not found"}
    try:
        ok = bcrypt.checkpw(pin.encode(), cust["pin_hash"].encode())
    except Exception:
        ok = False
    if not ok:
        return {"authenticated": False, "error": "Incorrect PIN"}
    return {
        "authenticated":   True,
        "name":            cust["name"],
        "phone":           cust["phone"],
        "account_id":      cust["account_id"],
        "router_model":    cust["router_model"],
        "router_serial":   cust["router_serial"],
        "vendor":          cust["vendor"],
        "technology":      cust["technology"],
        "subscription":    cust["subscription_plan"],
        "city":            cust["city"],
    }


@app.api_route("/tools/create_ticket", methods=["GET","POST"])
async def create_ticket(request: Request):
    # Accept both GET (simple) and POST (full log)
    if request.method == "POST":
        body = await request.json()
        account_id          = body.get("account_id","")
        problem_description = body.get("problem_description","")
        fault_detected      = body.get("fault_detected","")
        status              = body.get("status","open")
        fix_applied         = body.get("fix_applied")
        escalation_reason   = body.get("escalation_reason")
        full_log            = body.get("full_log", [])
        router_state_before = body.get("router_state_before")
        router_state_after  = body.get("router_state_after")
        resolution_time_s   = body.get("resolution_time_s")
        customer_name       = body.get("customer_name","")
        router_model        = body.get("router_model","")
        router_serial       = body.get("router_serial","")
    else:
        account_id          = request.query_params.get("account_id","")
        problem_description = request.query_params.get("problem_description","")
        fault_detected      = request.query_params.get("fault_detected","")
        status              = request.query_params.get("status","open")
        fix_applied         = request.query_params.get("fix_applied")
        escalation_reason   = request.query_params.get("escalation_reason")
        full_log            = []
        router_state_before = None
        router_state_after  = None
        resolution_time_s   = None
        customer_name       = ""
        router_model        = ""
        router_serial       = ""

    print(f"[customer] create_ticket: account={account_id} fault={fault_detected} status={status}")
    cust = db.customers.find_one({"account_id": account_id})
    if not cust:
        return {"success": False, "error": f"Customer {account_id} not found"}

    now = datetime.utcnow()
    ticket = {
        "customer_id":          cust["_id"],
        "account_id":           account_id,
        "customer_name":        customer_name or cust.get("name",""),
        "router_model":         router_model or cust.get("router_model",""),
        "router_serial":        router_serial or cust.get("router_serial",""),
        "created_at":           now,
        "resolved_at":          now if status == "resolved" else None,
        "problem_description":  problem_description,
        "fault_detected":       fault_detected,
        "fix_applied":          fix_applied,
        "status":               status,
        "resolution_time_s":    resolution_time_s,
        "agent_trace":          [s["tool"] for s in full_log] if full_log else [],
        "full_log":             full_log,
        "router_state_before":  router_state_before,
        "router_state_after":   router_state_after,
        "escalation_reason":    escalation_reason,
    }

    result    = db.tickets.insert_one(ticket)
    ticket_id = str(result.inserted_id)
    short_id  = ticket_id[-6:].upper()

    return {
        "success":   True,
        "ticket_id": ticket_id,
        "short_id":  short_id,
        "status":    status,
        "message":   f"Ticket #{short_id} created."
    }


@app.get("/tools/get_customer_tickets")
def get_customer_tickets(account_id: str):
    tickets = list(db.tickets.find(
        {"account_id": account_id},
        {"_id": 0, "customer_id": 0}
    ).sort("created_at", -1).limit(10))
    for t in tickets:
        if t.get("created_at"):  t["created_at"]  = t["created_at"].isoformat()
        if t.get("resolved_at"): t["resolved_at"] = t["resolved_at"].isoformat()
    return {"account_id": account_id, "total": len(tickets), "tickets": tickets}


# ── Admin: auth ───────────────────────────────────────────────────────────────

@app.get("/admin/authenticate")
def authenticate_admin(username: str, password: str):
    admin = db.admins.find_one({"username": username})
    if not admin:
        return {"authenticated": False, "error": "Username not found"}
    try:
        ok = bcrypt.checkpw(password.encode(), admin["password_hash"].encode())
    except Exception:
        ok = False
    if not ok:
        return {"authenticated": False, "error": "Incorrect password"}
    db.admins.update_one({"username": username}, {"$set": {"last_login": datetime.utcnow()}})
    return {
        "authenticated": True,
        "username":      admin["username"],
        "name":          admin["name"],
        "role":          admin["role"],
    }


@app.get("/admin/tickets")
def get_all_tickets(status: str = None, fault: str = None, account: str = None, limit: int = 100):
    query = {}
    if status:  query["status"]         = status
    if fault:   query["fault_detected"] = {"$regex": fault, "$options": "i"}
    if account: query["account_id"]     = {"$regex": account, "$options": "i"}
    tickets = list(db.tickets.find(query, {
        "customer_id": 0, "full_log": 0,
        "router_state_before": 0, "router_state_after": 0
    }).sort("created_at", -1).limit(limit))
    def ser(t):
        t["ticket_id"] = str(t.pop("_id"))
        if t.get("created_at"):  t["created_at"]  = t["created_at"].isoformat()
        if t.get("resolved_at"): t["resolved_at"] = t["resolved_at"].isoformat()
        return t
    tickets = [ser(t) for t in tickets]
    return {"total": len(tickets), "tickets": tickets}

@app.get("/admin/ticket/{ticket_id}")
def get_ticket_detail(ticket_id: str):
    from bson import ObjectId
    try:
        oid = ObjectId(ticket_id)
    except Exception:
        return {"error": "Invalid ticket ID"}
    t = db.tickets.find_one({"_id": oid}, {"customer_id": 0})
    if not t:
        return {"error": "Ticket not found"}

    def ser(v):
        if hasattr(v, 'isoformat'): return v.isoformat()
        if isinstance(v, dict):     return {k: ser(vv) for k, vv in v.items()}
        if isinstance(v, list):     return [ser(i) for i in v]
        return v

    t["_id"] = str(t["_id"])
    return ser(t)


if __name__ == "__main__":
    import uvicorn
    print(f"\n  Customer MCP Server")
    print(f"  MongoDB : {MONGO_URI} / {MONGO_DB}")
    print(f"  Listen  : http://0.0.0.0:{SERVER_PORT}")
    print(f"  Tools   : http://localhost:{SERVER_PORT}/tools\n")
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
