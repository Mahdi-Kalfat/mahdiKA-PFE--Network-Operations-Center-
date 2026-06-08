"""
genie_server.py  —  Fake GenieACS MCP Server
Simulates the ACS read side. Reads live router state from MongoDB.
Also exposes a fault injection endpoint for the NOC dashboard.

Run:
    python genie_server.py

Port: 8001
"""

import os
import random
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient

load_dotenv()

MONGO_URI   = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB    = os.getenv("MONGO_DB",  "mypfe")
SERVER_PORT = int(os.getenv("GENIE_PORT", "8001"))
BASE_URL    = os.getenv("GENIE_BASE_URL", f"http://localhost:{SERVER_PORT}")

client = MongoClient(MONGO_URI)
db     = client[MONGO_DB]

app = FastAPI(title="GenieACS MCP Server", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── FAULT CATALOG ─────────────────────────────────────────────────────────────

FAULT_CATALOG = {
    "ppp_auth_failure": {
        "status": "DOWN",
        "params": {
            "PPPStatus": "Disconnected",
            "LastConnectionError": "AuthenticationFailure",
            "Uptime": 0,
            "ConnectionStatus": "Disconnected",
        }
    },
    "wrong_vlan": {
        "status": "DOWN",
        "params": {
            "PPPStatus": "Disconnected",
            "ConnectionStatus": "Unconfigured",
            "VLANId": 999,
            "Uptime": 0,
        }
    },
    "dns_failure": {
        "status": "DOWN",
        "params": {
            "DNSServer": "0.0.0.0",
            "DNSStatus": "Error",
            "PPPStatus": "Connected",
        }
    },
    "weak_signal": {
        "status": "DOWN",
        "params": {
            "RXPower": -30.5,
            "SignalLoss": True,
            "PPPStatus": "Disconnected",
            "Uptime": 0,
        }
    },
    "random_disconnect": {
        "status": "DOWN",
        "params": {
            "PPPStatus": "Disconnected",
            "LastConnectionError": "ServerTimeout",
            "Uptime": 0,
        }
    },
    "hardware_fault": {
        "status": "DOWN",
        "params": {
            "ErrorCount": 847,
            "PPPStatus": "Disconnected",
            "ConnectionStatus": "Error",
            "Temperature": 92.4,
            "Uptime": 0,
        }
    },
}

INJECTABLE_FAULTS = [f for f in FAULT_CATALOG if f != "hardware_fault"]

# ── UTCP MANUAL ───────────────────────────────────────────────────────────────

def build_manual(base_url: str) -> dict:
    return {
        "utcp_version": "1.0.0",
        "manual_version": "1.0.0",
        "tools": [
            {
                "name": "get_router_state",
                "description": (
                    "Read the current live state of a customer's router from the ACS. "
                    "Returns all TR-069 parameter values: PPP status, RX optical power, "
                    "VLAN, DNS, uptime, temperature, WiFi, SIP. "
                    "Use this FIRST when diagnosing any connectivity problem. "
                    "Input: serial number OR account_id."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "serial":     {"type": "string", "description": "Router serial number"},
                        "account_id": {"type": "string", "description": "Customer account ID (alternative to serial)"}
                    },
                    "required": []
                },
                "outputs": {"type": "object"},
                "tags": ["router", "state", "genie", "acs", "live"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/get_router_state",
                    "content_type": "application/json"
                }
            },
            {
                "name": "get_customer_by_phone",
                "description": (
                    "Look up a customer profile by phone number. "
                    "Returns name, account ID, router model, serial number, "
                    "technology type, and subscription plan. "
                    "Call this after authentication to get the router serial for state reads."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "phone": {"type": "string", "description": "Customer phone number e.g. 0661234567"}
                    },
                    "required": ["phone"]
                },
                "outputs": {"type": "object"},
                "tags": ["customer", "profile", "lookup", "genie"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/get_customer_by_phone",
                    "content_type": "application/json"
                }
            },
            {
                "name": "get_fault_history",
                "description": (
                    "Get the history of past faults and resolutions for a customer's router. "
                    "Useful for identifying recurring problems."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "account_id": {"type": "string", "description": "Customer account ID"}
                    },
                    "required": ["account_id"]
                },
                "outputs": {"type": "object"},
                "tags": ["history", "tickets", "faults", "genie"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/get_fault_history",
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
def get_router_state(serial: str = None, account_id: str = None):
    if not serial and not account_id:
        return {"error": "Provide serial or account_id"}
    query = {"serial": serial} if serial else {"account_id": account_id}
    state = db.router_states.find_one(query, {"_id": 0, "customer_id": 0})
    if not state:
        return {"error": f"Router not found for {query}"}
    # Convert all non-JSON-serializable types
    if state.get("last_updated"):
        state["last_updated"] = state["last_updated"].isoformat()
    return state


@app.get("/tools/get_customer_by_phone")
def get_customer_by_phone(phone: str):
    cust = db.customers.find_one({"phone": phone}, {"_id": 0, "pin_hash": 0})
    if not cust:
        return {"error": f"No customer found with phone {phone}"}
    cust["created_at"] = cust["created_at"].isoformat() if cust.get("created_at") else None
    return cust


@app.get("/tools/get_fault_history")
def get_fault_history(account_id: str):
    cust = db.customers.find_one({"account_id": account_id}, {"_id": 0, "pin_hash": 0})
    if not cust:
        return {"error": f"No customer found with account_id {account_id}"}
    tickets = list(db.tickets.find(
        {"account_id": account_id},
        {"_id": 0}
    ).sort("created_at", -1).limit(10))
    for t in tickets:
        if t.get("created_at"):  t["created_at"]  = t["created_at"].isoformat()
        if t.get("resolved_at"): t["resolved_at"] = t["resolved_at"].isoformat()
    return {"account_id": account_id, "total": len(tickets), "tickets": tickets}


# ── NOC: Fault injection ──────────────────────────────────────────────────────

@app.post("/noc/inject_fault")
async def inject_fault(request: Request):
    """NOC Dashboard — inject a random fault into a router."""
    body   = await request.json()
    serial = body.get("serial")
    fault  = body.get("fault") or random.choice(INJECTABLE_FAULTS)

    if fault not in FAULT_CATALOG:
        return {"error": f"Unknown fault: {fault}. Options: {list(FAULT_CATALOG.keys())}"}

    state = db.router_states.find_one({"serial": serial})
    if not state:
        return {"error": f"Router {serial} not found"}

    fault_def = FAULT_CATALOG[fault]
    update    = {"$set": {
        "status":       fault_def["status"],
        "fault":        fault,
        "last_updated": datetime.utcnow(),
        **{f"parameters.{k}": v for k, v in fault_def["params"].items()}
    }}
    db.router_states.update_one({"serial": serial}, update)
    return {"success": True, "serial": serial, "fault_injected": fault,
            "status": fault_def["status"]}


@app.post("/noc/clear_fault")
async def clear_fault(request: Request):
    """NOC Dashboard — manually clear a fault and restore healthy state."""
    body   = await request.json()
    serial = body.get("serial")
    state  = db.router_states.find_one({"serial": serial})
    if not state:
        return {"error": f"Router {serial} not found"}

    correct_vlan = state.get("correct_vlan", 100)
    update = {"$set": {
        "status":  "UP",
        "fault":   "healthy",
        "last_updated": datetime.utcnow(),
        "parameters.PPPStatus":           "Connected",
        "parameters.LastConnectionError": "None",
        "parameters.ConnectionStatus":    "Connected",
        "parameters.VLANId":              correct_vlan,
        "parameters.DNSServer":           "8.8.8.8",
        "parameters.DNSStatus":           "Active",
        "parameters.RXPower":             round(random.uniform(-18.0, -12.0), 2),
        "parameters.SignalLoss":          False,
        "parameters.ErrorCount":          0,
        "parameters.Temperature":         round(random.uniform(38.0, 55.0), 2),
        "parameters.Uptime":              60,
    }}
    db.router_states.update_one({"serial": serial}, update)
    return {"success": True, "serial": serial, "status": "UP"}


@app.get("/noc/fleet")
def get_fleet():
    """NOC Dashboard — full fleet overview."""
    pipeline = [
        {"$lookup": {
            "from": "customers",
            "localField": "customer_id",
            "foreignField": "_id",
            "as": "customer"
        }},
        {"$unwind": "$customer"},
        {"$project": {
            "_id": 0,
            "serial": 1,
            "model": 1,
            "vendor": 1,
            "technology": 1,
            "status": 1,
            "fault": 1,
            "last_updated": 1,
            "customer_name":  "$customer.name",
            "customer_phone": "$customer.phone",
            "account_id":     "$customer.account_id",
        }}
    ]
    fleet = list(db.router_states.aggregate(pipeline))
    for r in fleet:
        if r.get("last_updated"):
            r["last_updated"] = r["last_updated"].isoformat()
    up   = sum(1 for r in fleet if r["status"] == "UP")
    down = sum(1 for r in fleet if r["status"] == "DOWN")
    return {"total": len(fleet), "up": up, "down": down, "routers": fleet}


if __name__ == "__main__":
    import uvicorn
    print(f"\n  GenieACS MCP Server")
    print(f"  MongoDB : {MONGO_URI} / {MONGO_DB}")
    print(f"  Listen  : http://0.0.0.0:{SERVER_PORT}")
    print(f"  Tools   : http://localhost:{SERVER_PORT}/tools")
    print(f"  Fleet   : http://localhost:{SERVER_PORT}/noc/fleet\n")
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)