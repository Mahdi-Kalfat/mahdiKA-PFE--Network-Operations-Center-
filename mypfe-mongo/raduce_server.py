"""
raduce_server.py  —  RaDuce MCP Server (graph-driven remote fixes)

What changed vs v2
------------------
RaDuce no longer flips a flat `PPPStatus` key to "Connected". For every fix it:

    1. Resolves the logical fix to a real TR-069 PATH via the Neo4j knowledge
       graph (mcp_engine.Resolver). The path is vendor-specific:
       Huawei X_HW_VLAN vs TP-Link X_TP_VID vs Nokia VLANIDMark.
    2. Checks the role is on the editable allowlist (the write gate). RaDuce
       refuses to write a non-editable parameter, exactly like a real ACS
       rejecting SetParameterValues on a read-only node.
    3. Writes the new value at that path in MongoDB router_states.parameters
       (path-keyed = a real ACS device tree), plus the physical consequence
       (e.g. correcting the VLAN brings ConnectionStatus to Connected).
    4. Re-derives status from the parameters. The router is UP iff no fault
       condition holds — so a fix that doesn't clear the root cause leaves it
       DOWN and the caller escalates. No fix can fake success.

Endpoints unchanged: MCP at /mcp/, REST at /tools/<name>, UTCP manual at /tools.
Port 8002.
"""

import os
import contextlib
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient

from mcp.server.fastmcp import FastMCP

import mcp_engine as E

load_dotenv()

MONGO_URI   = os.getenv("MONGO_URI",       "mongodb://localhost:27017")
MONGO_DB    = os.getenv("MONGO_DB",        "mypfe")
SERVER_PORT = int(os.getenv("RADUCE_PORT", "8002"))
BASE_URL    = os.getenv("RADUCE_BASE_URL", f"http://localhost:{SERVER_PORT}")
NEO4J_API   = os.getenv("NEO4J_API",       "http://neo4j-agent:8000")

client   = MongoClient(MONGO_URI)
db       = client[MONGO_DB]
resolver = E.Resolver(NEO4J_API)


# -- HELPERS -------------------------------------------------------------------

def get_state(serial: str):
    return db.router_states.find_one({"serial": serial})


def _model_of(state: dict) -> str:
    return state.get("model") or ""


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


def apply_paths(serial: str, model: str, path_values: dict) -> dict:
    """
    Write {path: value} into router_states.parameters, then re-derive status
    and fault from the resulting parameters. Returns the new (status, fault).
    """
    state  = get_state(serial)
    params = flatten_parameters(state.get("parameters", {}))
    params.update(path_values)

    sets = {
        "parameters": params,
        "status": E.derive_status(model, params, resolver),
        "fault": E.diagnose(model, params, resolver)["fault"],
        "last_updated": datetime.utcnow(),
    }
    db.router_states.update_one({"serial": serial}, {"$set": sets})
    return {"status": sets["status"], "fault": sets["fault"]}


def write_role(serial: str, model: str, role: str, value, consequence: dict) -> dict:
    """
    Resolve role -> path, enforce the editable allowlist, write value + the
    physical consequence params, re-derive status. The single code path every
    config fix goes through.
    """
    if not E.is_writable_role(role):
        return {"success": False,
                "error": f"Role '{role}' is read-only — RaDuce cannot write it."}

    path = resolver.resolve(model, role)
    if not path:
        return {"success": False,
                "error": f"No TR-069 path for role '{role}' on model '{model}'."}

    # Resolve the consequence roles to paths too (so we stay path-keyed).
    written = {path: value}
    for c_role, c_val in consequence.items():
        c_path = resolver.resolve(model, c_role)
        if c_path:
            written[c_path] = c_val

    result = apply_paths(serial, model, written)
    return {"success": result["status"] == "UP",
            "tr069_path_written": path,
            "tr069_value": value,
            "new_status": result["status"],
            "new_fault": result["fault"]}


# -- SHARED TOOL LOGIC ---------------------------------------------------------

def logic_restart_ppp(serial: str) -> dict:
    print(f"[raduce] restart_ppp: serial={serial}")
    state = get_state(serial)
    if not state:
        return {"success": False, "error": f"Router {serial} not found"}
    model = _model_of(state)
    # Re-push PPP credentials (write role: ppp_password). Physical consequence:
    # the session reconnects -> Connected, error cleared, uptime resets.
    r = write_role(serial, model, "ppp_password", "******",
                   consequence={"ppp_connection_status": "Connected",
                                "last_connection_error": "None",
                                "uptime": 30})
    if not r.get("success") and "error" in r:
        return r
    r.update({"action": "restart_ppp", "serial": serial,
              "result": ("PPP session restarted (credentials re-pushed). "
                         f"Status: {r.get('new_status')}.")})
    return r


def logic_set_vlan(serial: str, vlan_id: int = E.CORRECT_VLAN) -> dict:
    print(f"[raduce] set_vlan: serial={serial} vlan_id={vlan_id}")
    state = get_state(serial)
    if not state:
        return {"success": False, "error": f"Router {serial} not found"}
    model = _model_of(state)
    r = write_role(serial, model, "vlan_id", int(vlan_id),
                   consequence={"ppp_connection_status": "Connected",
                                "uptime": 15})
    if not r.get("success") and "error" in r:
        return r
    r.update({"action": "set_vlan", "serial": serial,
              "result": f"VLAN corrected to {vlan_id}. Status: {r.get('new_status')}."})
    return r


def logic_set_dns(serial: str, primary: str = "8.8.8.8", secondary: str = "1.1.1.1") -> dict:
    print(f"[raduce] set_dns: serial={serial} primary={primary}")
    state = get_state(serial)
    if not state:
        return {"success": False, "error": f"Router {serial} not found"}
    model = _model_of(state)
    r = write_role(serial, model, "dns_servers", primary, consequence={})
    if not r.get("success") and "error" in r:
        return r
    r.update({"action": "set_dns", "serial": serial,
              "result": f"DNS updated to {primary}. Status: {r.get('new_status')}."})
    return r


def logic_reboot_router(serial: str) -> dict:
    print(f"[raduce] reboot_router: serial={serial}")
    state = get_state(serial)
    if not state:
        return {"success": False, "error": f"Router {serial} not found"}
    model = _model_of(state)
    r = write_role(serial, model, "reboot", "true",
                   consequence={"ppp_connection_status": "Connected",
                                "last_connection_error": "None",
                                "dsl_crc_errors": 0,
                                "uptime": 10})
    if not r.get("success") and "error" in r:
        return r
    r.update({"action": "reboot_router", "serial": serial,
              "result": f"Remote reboot sent. Status: {r.get('new_status')}."})
    return r


def logic_escalate_to_technician(serial: str, reason: str, account_id: str = None) -> dict:
    print(f"[raduce] escalate: serial={serial} reason={reason}")
    state = get_state(serial)
    if not state:
        return {"success": False, "error": f"Router {serial} not found"}
    ticket = {
        "account_id":          account_id or state.get("account_id"),
        "customer_id":         state.get("customer_id"),
        "created_at":          datetime.utcnow(),
        "resolved_at":         None,
        "problem_description": reason,
        "fault_detected":      state.get("fault"),
        "fix_applied":         None,
        "status":              "escalated",
        "resolution_time_s":   None,
        "agent_trace":         ["get_router_state", "diagnose_fault", "escalate_to_technician"],
        "escalation_reason":   reason,
    }
    ticket_id = str(db.tickets.insert_one(ticket).inserted_id)
    return {"success": True, "action": "escalate_to_technician", "serial": serial,
            "ticket_id": ticket_id,
            "result": f"Ticket #{ticket_id[-6:].upper()} created. A technician will contact the customer within 24h.",
            "status": "escalated"}


# -- MCP SERVER ----------------------------------------------------------------

mcp = FastMCP("raduce-acs", stateless_http=True, json_response=True,
              streamable_http_path="/", host="0.0.0.0")


@mcp.tool()
def restart_ppp(serial: str) -> dict:
    """Restart the PPP session by re-pushing credentials (writes the real
    WANPPPConnection Password path). Use when PPP is Disconnected with an
    authentication or timeout error."""
    return logic_restart_ppp(serial)


@mcp.tool()
def set_vlan(serial: str, vlan_id: int = 100) -> dict:
    """Correct the WAN VLAN. Resolves the vendor-specific VLAN path from the
    knowledge graph (Huawei X_HW_VLAN / TP-Link X_TP_VID / Nokia VLANIDMark)
    and writes it. Use when ConnectionStatus is Unconfigured or the VLAN id is
    wrong."""
    return logic_set_vlan(serial, vlan_id)


@mcp.tool()
def set_dns(serial: str, primary: str = "8.8.8.8", secondary: str = "1.1.1.1") -> dict:
    """Push correct DNS servers (writes the real WANPPPConnection DNSServers
    path). Use when the DNS server reads 0.0.0.0."""
    return logic_set_dns(serial, primary, secondary)


@mcp.tool()
def reboot_router(serial: str) -> dict:
    """Send a remote reboot. Use as a last resort when config fixes did not
    resolve the issue."""
    return logic_reboot_router(serial)


@mcp.tool()
def escalate_to_technician(serial: str, reason: str, account_id: str = "") -> dict:
    """Escalate to a physical technician when the fault cannot be fixed
    remotely (weak optical signal, high DSL errors, or a remote fix that did
    not bring the router back UP). Creates an open ticket."""
    return logic_escalate_to_technician(serial, reason, account_id or None)


# -- FASTAPI APP ---------------------------------------------------------------

@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="RaDuce MCP Server", version="3.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/mcp", mcp.streamable_http_app())


def build_manual(base_url: str) -> dict:
    def tool(name, desc, props, required, tags):
        return {"name": name, "description": desc,
                "inputs": {"type": "object", "properties": props, "required": required},
                "outputs": {"type": "object"}, "tags": tags,
                "tool_call_template": {"call_template_type": "http", "http_method": "GET",
                    "url": f"{base_url}/tools/{name}", "content_type": "application/json"}}
    return {
        "utcp_version": "1.0.0", "manual_version": "3.0.0",
        "tools": [
            tool("restart_ppp", "Restart the PPP session (re-push credentials).",
                 {"serial": {"type": "string"}}, ["serial"], ["fix", "ppp", "raduce"]),
            tool("set_vlan", "Correct the VLAN (vendor path resolved from the graph).",
                 {"serial": {"type": "string"}, "vlan_id": {"type": "number"}}, ["serial"],
                 ["fix", "vlan", "raduce"]),
            tool("set_dns", "Push correct DNS servers.",
                 {"serial": {"type": "string"}, "primary": {"type": "string"},
                  "secondary": {"type": "string"}}, ["serial"], ["fix", "dns", "raduce"]),
            tool("reboot_router", "Send a remote reboot.",
                 {"serial": {"type": "string"}}, ["serial"], ["fix", "reboot", "raduce"]),
            tool("escalate_to_technician", "Escalate to a physical technician.",
                 {"serial": {"type": "string"}, "account_id": {"type": "string"},
                  "reason": {"type": "string"}}, ["serial", "reason"],
                 ["escalate", "technician", "raduce"]),
        ],
    }


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
def rest_restart_ppp(serial: str):
    return logic_restart_ppp(serial)


@app.get("/tools/set_vlan")
def rest_set_vlan(serial: str, vlan_id: int = 100):
    return logic_set_vlan(serial, vlan_id)


@app.get("/tools/set_dns")
def rest_set_dns(serial: str, primary: str = "8.8.8.8", secondary: str = "1.1.1.1"):
    return logic_set_dns(serial, primary, secondary)


@app.get("/tools/reboot_router")
def rest_reboot_router(serial: str):
    return logic_reboot_router(serial)


@app.get("/tools/escalate_to_technician")
def rest_escalate(serial: str, reason: str, account_id: str = None):
    return logic_escalate_to_technician(serial, reason, account_id)


if __name__ == "__main__":
    import uvicorn
    print(f"\n  RaDuce MCP Server v3 (graph-driven, path-keyed)")
    print(f"  MongoDB : {MONGO_URI} / {MONGO_DB}")
    print(f"  Neo4j   : {NEO4J_API}  (resolve_path, best-effort + cached fallback)")
    print(f"  MCP     : http://localhost:{SERVER_PORT}/mcp/")
    print(f"  REST    : http://localhost:{SERVER_PORT}/tools/set_vlan\n")
    uvicorn.run(app, host="0.0.0.0", port=SERVER_PORT)
