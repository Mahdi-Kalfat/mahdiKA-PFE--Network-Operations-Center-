"""
raduce_server.py  —  Fake RaDuce MCP Server
Simulates the ACS write side. Pushes fixes to MongoDB router_states.

Run:
    python raduce_server.py

Port: 8002
"""

import os
import random
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient

load_dotenv()

MONGO_URI   = os.getenv("MONGO_URI",       "mongodb://localhost:27017")
MONGO_DB    = os.getenv("MONGO_DB",        "mypfe")
SERVER_PORT = int(os.getenv("RADUCE_PORT", "8002"))
BASE_URL    = os.getenv("RADUCE_BASE_URL", f"http://localhost:{SERVER_PORT}")

client = MongoClient(MONGO_URI)
db     = client[MONGO_DB]

app = FastAPI(title="RaDuce MCP Server", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── UTCP MANUAL ───────────────────────────────────────────────────────────────

def build_manual(base_url: str) -> dict:
    return {
        "utcp_version": "1.0.0",
        "manual_version": "1.0.0",
        "tools": [
            {
                "name": "restart_ppp",
                "description": (
                    "Restart the PPP session on a router. "
                    "Use when PPPStatus is Disconnected with LastConnectionError = "
                    "AuthenticationFailure or ServerTimeout. "
                    "This resets the PPP credentials and forces reconnection."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "serial": {"type": "string", "description": "Router serial number"}
                    },
                    "required": ["serial"]
                },
                "outputs": {"type": "object"},
                "tags": ["fix", "ppp", "reconnect", "raduce"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/restart_ppp",
                    "content_type": "application/json"
                }
            },
            {
                "name": "set_vlan",
                "description": (
                    "Correct the VLAN configuration on a router. "
                    "Use when ConnectionStatus is Unconfigured and VLANId is wrong. "
                    "Always use vlan_id=100 unless the customer has a special plan."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "serial":  {"type": "string", "description": "Router serial number"},
                        "vlan_id": {"type": "number", "description": "Correct VLAN ID (default: 100)"}
                    },
                    "required": ["serial"]
                },
                "outputs": {"type": "object"},
                "tags": ["fix", "vlan", "configuration", "raduce"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/set_vlan",
                    "content_type": "application/json"
                }
            },
            {
                "name": "set_dns",
                "description": (
                    "Push correct DNS server addresses to a router. "
                    "Use when DNSServer is 0.0.0.0 or DNSStatus is Error. "
                    "Default primary DNS: 8.8.8.8, secondary: 1.1.1.1."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "serial":    {"type": "string", "description": "Router serial number"},
                        "primary":   {"type": "string", "description": "Primary DNS (default: 8.8.8.8)"},
                        "secondary": {"type": "string", "description": "Secondary DNS (default: 1.1.1.1)"}
                    },
                    "required": ["serial"]
                },
                "outputs": {"type": "object"},
                "tags": ["fix", "dns", "configuration", "raduce"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/set_dns",
                    "content_type": "application/json"
                }
            },
            {
                "name": "reboot_router",
                "description": (
                    "Send a remote reboot command to the router. "
                    "Use as a last resort when PPP restart and config fixes haven't worked, "
                    "or when ErrorCount is high but hardware fault isn't confirmed."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "serial": {"type": "string", "description": "Router serial number"}
                    },
                    "required": ["serial"]
                },
                "outputs": {"type": "object"},
                "tags": ["fix", "reboot", "restart", "raduce"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/reboot_router",
                    "content_type": "application/json"
                }
            },
            {
                "name": "escalate_to_technician",
                "description": (
                    "Escalate to a physical technician when the problem cannot be fixed remotely. "
                    "Use when: RXPower is below -27 dBm (weak optical signal), "
                    "ErrorCount is very high (hardware fault), "
                    "or when 2+ remote fix attempts have failed. "
                    "This creates an open ticket visible on the NOC dashboard."
                ),
                "inputs": {
                    "type": "object",
                    "properties": {
                        "serial":     {"type": "string", "description": "Router serial number"},
                        "account_id": {"type": "string", "description": "Customer account ID"},
                        "reason":     {"type": "string", "description": "Reason for escalation"}
                    },
                    "required": ["serial", "reason"]
                },
                "outputs": {"type": "object"},
                "tags": ["escalate", "technician", "ticket", "raduce"],
                "tool_call_template": {
                    "call_template_type": "http",
                    "http_method": "GET",
                    "url": f"{base_url}/tools/escalate_to_technician",
                    "content_type": "application/json"
                }
            }
        ]
    }


# ── HELPERS ───────────────────────────────────────────────────────────────────

def get_state(serial: str):
    return db.router_states.find_one({"serial": serial})

def update_state(serial: str, updates: dict):
    db.router_states.update_one(
        {"serial": serial},
        {"$set": {**updates, "last_updated": datetime.utcnow()}}
    )


# ── ROUTES ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok", "mongodb": "connected"}
    except Exception as e:
        return {"status": "error", "mongodb": str(e)}


@app.get("/tools")
def list_tools(request: Request):
    host   = request.headers.get("host", f"localhost:{SERVER_PORT}")
    scheme = request.headers.get("x-forwarded-proto", "http")
    base   = os.getenv("RADUCE_BASE_URL") or f"{scheme}://{host}"
    return build_manual(base)


@app.get("/tools/restart_ppp")
def restart_ppp(serial: str):
    print(f"[raduce] restart_ppp: serial={serial}")
    state = get_state(serial)
    if not state:
        return {"success": False, "error": f"Router {serial} not found"}

    # Simulate PPP restart: wait ~2s in real life, we just update immediately
    update_state(serial, {
        "status":                          "UP",
        "fault":                           "healthy",
        "parameters.PPPStatus":            "Connected",
        "parameters.LastConnectionError":  "None",
        "parameters.ConnectionStatus":     "Connected",
        "parameters.Uptime":               30,
    })
    return {
        "success":    True,
        "action":     "restart_ppp",
        "serial":     serial,
        "result":     "PPP session restarted successfully. Status: Connected.",
        "new_status": "UP"
    }


@app.get("/tools/set_vlan")
def set_vlan(serial: str, vlan_id: int = 100):
    print(f"[raduce] set_vlan: serial={serial} vlan_id={vlan_id}")
    state = get_state(serial)
    if not state:
        return {"success": False, "error": f"Router {serial} not found"}

    update_state(serial, {
        "status":                       "UP",
        "fault":                        "healthy",
        "parameters.VLANId":            vlan_id,
        "parameters.ConnectionStatus":  "Connected",
        "parameters.PPPStatus":         "Connected",
        "parameters.Uptime":            15,
    })
    return {
        "success":    True,
        "action":     "set_vlan",
        "serial":     serial,
        "result":     f"VLAN corrected to {vlan_id}. Router reconnected.",
        "new_status": "UP"
    }


@app.get("/tools/set_dns")
def set_dns(serial: str, primary: str = "8.8.8.8", secondary: str = "1.1.1.1"):
    print(f"[raduce] set_dns: serial={serial} primary={primary}")
    state = get_state(serial)
    if not state:
        return {"success": False, "error": f"Router {serial} not found"}

    update_state(serial, {
        "status":                    "UP",
        "fault":                     "healthy",
        "parameters.DNSServer":      primary,
        "parameters.DNSSecondary":   secondary,
        "parameters.DNSStatus":      "Active",
    })
    return {
        "success":    True,
        "action":     "set_dns",
        "serial":     serial,
        "result":     f"DNS updated: primary={primary}, secondary={secondary}.",
        "new_status": "UP"
    }


@app.get("/tools/reboot_router")
def reboot_router(serial: str):
    print(f"[raduce] reboot_router: serial={serial}")
    state = get_state(serial)
    if not state:
        return {"success": False, "error": f"Router {serial} not found"}

    update_state(serial, {
        "status":                           "UP",
        "fault":                            "healthy",
        "parameters.PPPStatus":             "Connected",
        "parameters.LastConnectionError":   "None",
        "parameters.ConnectionStatus":      "Connected",
        "parameters.ErrorCount":            0,
        "parameters.Uptime":                10,
        "parameters.Temperature":           round(random.uniform(38.0, 48.0), 2),
    })
    return {
        "success":    True,
        "action":     "reboot_router",
        "serial":     serial,
        "result":     "Remote reboot sent. Router came back online after 45 seconds.",
        "new_status": "UP"
    }


@app.get("/tools/escalate_to_technician")
def escalate_to_technician(serial: str, reason: str, account_id: str = None):
    print(f"[raduce] escalate: serial={serial} reason={reason}")
    state = get_state(serial)
    if not state:
        return {"success": False, "error": f"Router {serial} not found"}

    # Create an open ticket
    ticket = {
        "account_id":           account_id or state.get("account_id"),
        "customer_id":          state.get("customer_id"),
        "created_at":           datetime.utcnow(),
        "resolved_at":          None,
        "problem_description":  reason,
        "fault_detected":       state.get("fault"),
        "fix_applied":          None,
        "status":               "escalated",
        "resolution_time_s":    None,
        "agent_trace":          ["get_router_state", "diagnose_fault", "escalate_to_technician"],
        "escalation_reason":    reason,
    }
    result = db.tickets.insert_one(ticket)
    ticket_id = str(result.inserted_id)

    return {
        "success":    True,
        "action":     "escalate_to_technician",
        "serial":     serial,
        "ticket_id":  ticket_id,
        "result":     f"Ticket #{ticket_id[-6:].upper()} created. A technician will contact the customer within 24h.",
        "status":     "escalated"
    }


if __name__ == "__main__":
    import uvicorn
    print(f"\n  RaDuce MCP Server")
    print(f"  MongoDB : {MONGO_URI} / {MONGO_DB}")
    print(f"  Listen  : http://0.0.0.0:{SERVER_PORT}")
    print(f"  Tools   : http://localhost:{SERVER_PORT}/tools\n")
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
