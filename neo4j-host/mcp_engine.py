"""
mcp_engine.py  —  Role / Path / Fault engine (single source of truth)

This is the heart of the GraphRAG-driven upgrade. It introduces a thin
"role" layer that decouples the simulator's logical parameters from each
router's real TR-069 path:

    role  (e.g. "vlan_id")  --resolve(model, role)-->  real TR-069 path
                                                        (vendor-specific)

Why it matters
--------------
* The same logical fix ("set the VLAN") maps to a DIFFERENT real path per
  vendor — Huawei  ...WANPPPConnection.1.X_HW_VLAN,
            TP-Link ...WANPTMLinkConfig.X_TP_VID,
            Nokia   ...X_CT-COM_WANGponLinkConfig.VLANIDMark.
  The Neo4j knowledge graph is what knows which path a given model uses, so
  RaDuce literally cannot write the fix without the graph (or this cached
  mirror of it). That makes the graph load-bearing, not decorative.

* Router state in MongoDB is stored keyed by the REAL path, exactly like a
  real ACS device tree. RaDuce writes by path = a real SetParameterValues.

* `status` is DERIVED from the parameters (UP iff no fault condition holds),
  not a flag a fix flips to UP. So a fix that doesn't actually clear the root
  cause leaves the router DOWN and the escalation branch fires for real.

Resolution order in `Resolver.resolve()`:
    1. Ask the neo4j-agent REST API (live graph)         — best effort
    2. Fall back to PATHS below (a cached mirror of the graph dump)
The fallback means the whole system works immediately, and "upgrades" to live
graph resolution once the resolve endpoint + migration are deployed.

Place a copy of this file in BOTH service folders that import it:
    mypfe-mongo/mcp_engine.py     (seed.py, genie_server.py, raduce_server.py)
    mypfe-app/mcp_engine.py       (app.py)
"""

from __future__ import annotations

import os
import requests

NEO4J_API = os.getenv("NEO4J_API", "http://localhost:8000")


# ---------------------------------------------------------------------------
# 1) CANONICAL ROLES
# ---------------------------------------------------------------------------
# Read roles  — diagnosis reads their live value (by path) from Mongo.
# Write roles — RaDuce may push a new value (by path) for these only.

READ_ROLES = [
    "ppp_connection_status",   # Connected / Disconnected / Unconfigured / Error
    "last_connection_error",   # None / AuthenticationFailure / ServerTimeout
    "vlan_id",                 # integer VLAN id
    "dns_servers",             # primary DNS, "0.0.0.0" when broken
    "rx_optical_power",        # dBm, GPON only
    "dsl_crc_errors",          # CRC error count, DSL only
    "uptime",                  # seconds
]

# RaDuce is ONLY allowed to write these roles (the editable allowlist).
# Note these are config levers, not status fields — exactly like real TR-069:
# you write config, the status follows.
WRITE_ROLES = [
    "vlan_id",
    "ppp_password",            # re-push credentials == "restart PPP"
    "dns_servers",
    "reboot",                  # Reboot action
]


# ---------------------------------------------------------------------------
# 2) PATHS — cached mirror of the Neo4j graph (from graph_dump.json)
# ---------------------------------------------------------------------------
# Keyed by the router id as it appears in the graph (== router_model in Mongo
# after the seed update). last_connection_error / dns_servers are standard
# TR-098 paths derived from the PPP-connection prefix; the migration adds them
# to the graph too so live resolution returns the same value.

PATHS = {
    "D-Link_DSL224": {
        "ppp_connection_status": "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "ppp_password":          "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        "vlan_id":               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.X_CT-COM_WANGponLinkConfig.VLANIDMark",
        "uptime":                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Uptime",
        "dsl_crc_errors":        "InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.Stats.Total.CRCErrors",
        "reboot":                "Reboot",
    },
    "G-1426G-D": {
        "ppp_connection_status": "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "ppp_password":          "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        "vlan_id":               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.X_CT-COM_WANGponLinkConfig.VLANIDMark",
        "rx_optical_power":      "InternetGatewayDevice.X_ALU_OntOpticalParam.RXPower",
        "uptime":                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Uptime",
        "reboot":                "Reboot",
    },
    "HG8145X7-10": {
        "ppp_connection_status": "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "ppp_password":          "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        "vlan_id":               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.X_HW_VLAN",
        "rx_optical_power":      "InternetGatewayDevice.WANDevice.1.X_GponInterafceConfig.RXPower",
        "reboot":                "Reboot",
    },
    "Huawei_8145": {
        "ppp_connection_status": "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "ppp_password":          "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        "vlan_id":               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.X_HW_VLAN",
        "rx_optical_power":      "InternetGatewayDevice.WANDevice.1.X_GponInterafceConfig.RXPower",
        "reboot":                "Reboot",
    },
    "Huawei_HG8245H5": {
        "ppp_connection_status": "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "ppp_password":          "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        "vlan_id":               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.X_HW_VLAN",
        "rx_optical_power":      "InternetGatewayDevice.WANDevice.1.X_GponInterafceConfig.RXPower",
        "reboot":                "Reboot",
    },
    "Huawei_V163": {
        "ppp_connection_status": "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "ppp_password":          "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        "vlan_id":               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.X_HW_VLAN",
        "rx_optical_power":      "InternetGatewayDevice.WANDevice.1.X_GponInterafceConfig.RXPower",
        "reboot":                "Reboot",
    },
    "NOKIA_1425": {
        "ppp_connection_status": "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "ppp_password":          "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        "vlan_id":               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.X_CT-COM_WANGponLinkConfig.VLANIDMark",
        "rx_optical_power":      "InternetGatewayDevice.X_ALU_OntOpticalParam.RXPower",
        "uptime":                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Uptime",
        "reboot":                "Reboot",
    },
    "NOKIA_2425": {
        "ppp_connection_status": "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "ppp_password":          "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        "vlan_id":               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.X_CT-COM_WANGponLinkConfig.VLANIDMark",
        "rx_optical_power":      "InternetGatewayDevice.X_ALU_OntOpticalParam.RXPower",
        "uptime":                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Uptime",
        "reboot":                "Reboot",
    },
    "NOKIA_G-240W-A": {
        "ppp_connection_status": "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "ppp_password":          "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        "vlan_id":               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.X_CT-COM_WANGponLinkConfig.VLANIDMark",
        "rx_optical_power":      "InternetGatewayDevice.X_ALU_OntOpticalParam.RXPower",
        "uptime":                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Uptime",
        "reboot":                "Reboot",
    },
    "NOKIA_G-240W-F": {
        "ppp_connection_status": "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "ppp_password":          "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        "vlan_id":               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.X_CT-COM_WANGponLinkConfig.VLANIDMark",
        "rx_optical_power":      "InternetGatewayDevice.X_ALU_OntOpticalParam.RXPower",
        "uptime":                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Uptime",
        "reboot":                "Reboot",
    },
    "TP-LINK_VC220-G3v": {
        "ppp_connection_status": "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2.WANPPPConnection.1.ConnectionStatus",
        "ppp_password":          "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2.WANPPPConnection.1.Password",
        "vlan_id":               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2.WANPTMLinkConfig.X_TP_VID",
        "uptime":                "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.2.WANPPPConnection.1.Uptime",
        "dsl_crc_errors":        "InternetGatewayDevice.WANDevice.1.WANDSLInterfaceConfig.Stats.Total.CRCErrors",
        "reboot":                "Reboot",
    },
    "V166a-20": {
        "ppp_connection_status": "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.ConnectionStatus",
        "ppp_password":          "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.Password",
        "vlan_id":               "InternetGatewayDevice.WANDevice.1.WANConnectionDevice.1.WANPPPConnection.1.X_HW_VLAN",
        "rx_optical_power":      "InternetGatewayDevice.WANDevice.1.X_GponInterafceConfig.RXPower",
        "reboot":                "Reboot",
    },
}

# Derive the two standard TR-098 paths that were missing from the scrape
# (LastConnectionError, DNSServers) from each router's PPP-connection prefix,
# so the fallback map is complete. The migration adds the same to the graph.
for _model, _roles in PATHS.items():
    _cs = _roles.get("ppp_connection_status", "")
    if _cs.endswith(".ConnectionStatus"):
        _prefix = _cs[: -len("ConnectionStatus")]
        _roles.setdefault("last_connection_error", _prefix + "LastConnectionError")
        _roles.setdefault("dns_servers",           _prefix + "DNSServers")

# Default reboot path for any model that doesn't list one.
for _roles in PATHS.values():
    _roles.setdefault("reboot", "Reboot")


# ---------------------------------------------------------------------------
# 3) FAULT MODEL (role-keyed — single source of truth for seed + genie)
# ---------------------------------------------------------------------------

HEALTHY = {
    "ppp_connection_status": "Connected",
    "last_connection_error": "None",
    "vlan_id":               100,
    "dns_servers":           "8.8.8.8",
    "rx_optical_power":      -15.2,
    "dsl_crc_errors":        0,
    "uptime":                86400,
}

CORRECT_VLAN = 100

# Each fault overlays role->value on top of HEALTHY and declares the fix that
# remediates it. `escalate` faults have no remote fix (technician needed).
FAULTS = {
    "healthy": {"overlay": {}, "fix": None},

    "ppp_auth_failure": {
        "overlay": {"ppp_connection_status": "Disconnected",
                    "last_connection_error": "AuthenticationFailure",
                    "uptime": 0},
        "fix": "restart_ppp",
    },
    "random_disconnect": {
        "overlay": {"ppp_connection_status": "Disconnected",
                    "last_connection_error": "ServerTimeout",
                    "uptime": 0},
        "fix": "restart_ppp",
    },
    "wrong_vlan": {
        "overlay": {"ppp_connection_status": "Unconfigured",
                    "vlan_id": 999,
                    "uptime": 0},
        "fix": "set_vlan",
    },
    "dns_failure": {
        "overlay": {"dns_servers": "0.0.0.0",
                    "ppp_connection_status": "Connected"},
        "fix": "set_dns",
    },
    "weak_signal": {
        "overlay": {"rx_optical_power": -30.5,
                    "ppp_connection_status": "Disconnected",
                    "uptime": 0},
        "fix": "escalate_to_technician",
    },
    "hardware_fault": {
        "overlay": {"dsl_crc_errors": 847,
                    "ppp_connection_status": "Error",
                    "uptime": 0},
        "fix": "escalate_to_technician",
    },
}


# ---------------------------------------------------------------------------
# 4) RESOLVER  (model, role) -> real TR-069 path
# ---------------------------------------------------------------------------

class Resolver:
    """Resolve role -> path, live from the graph if reachable, else cached."""

    def __init__(self, neo4j_api: str | None = None, use_graph: bool = True):
        self.neo4j_api = (neo4j_api or NEO4J_API).rstrip("/")
        self.use_graph = use_graph
        self._cache: dict[tuple[str, str], str | None] = {}

    @staticmethod
    def normalize_model(model: str) -> str | None:
        """Map a model string to a key in PATHS (exact, then fuzzy)."""
        if not model:
            return None
        if model in PATHS:
            return model
        needle = model.lower().replace(" ", "").replace("-", "").replace("_", "")
        for key in PATHS:
            k = key.lower().replace(" ", "").replace("-", "").replace("_", "")
            if needle in k or k in needle:
                return key
        for key in PATHS:                      # last resort: token overlap
            ktoks = set(key.lower().replace("-", " ").replace("_", " ").split())
            mtoks = set(model.lower().replace("-", " ").replace("_", " ").split())
            if ktoks & mtoks:
                return key
        return None

    def _from_graph(self, model: str, role: str) -> str | None:
        if not self.use_graph:
            return None
        try:
            r = requests.get(f"{self.neo4j_api}/tools/resolve_path",
                             params={"model": model, "role": role}, timeout=5)
            if r.status_code == 200:
                return (r.json() or {}).get("path") or None
        except Exception:
            pass
        return None

    def _from_cache_map(self, model: str, role: str) -> str | None:
        key = self.normalize_model(model)
        if not key:
            return None
        return PATHS.get(key, {}).get(role)

    def resolve(self, model: str, role: str) -> str | None:
        ck = (model, role)
        if ck in self._cache:
            return self._cache[ck]
        path = self._from_graph(model, role) or self._from_cache_map(model, role)
        self._cache[ck] = path
        return path

    def roles_for(self, model: str) -> dict[str, str]:
        """All role->path the cached map knows for a model (used by seed/genie)."""
        key = self.normalize_model(model)
        return dict(PATHS.get(key, {})) if key else {}


# ---------------------------------------------------------------------------
# 5) BUILD path-keyed state from a role-keyed fault (seed + genie injection)
# ---------------------------------------------------------------------------

def expand_state(model: str, fault: str, resolver: Resolver) -> dict:
    """
    Return a path-keyed `parameters` dict for `model` in state `fault`.
    Only roles that exist for this model are written (e.g. DSL models get no
    rx_optical_power), so the device tree stays realistic per technology.
    """
    role_values = dict(HEALTHY)
    role_values.update(FAULTS.get(fault, FAULTS["healthy"])["overlay"])

    available = resolver.roles_for(model)
    params: dict = {}
    for role, value in role_values.items():
        path = available.get(role)
        if path:
            params[path] = value
    return params


def healthy_overlay(model: str, resolver: Resolver) -> dict:
    """Path-keyed parameters that 'clear_fault' resets a router back to."""
    return expand_state(model, "healthy", resolver)


# ---------------------------------------------------------------------------
# 6) DIAGNOSIS  (reads values by role->path, returns fault + fix + trace)
# ---------------------------------------------------------------------------

RX_ESCALATE_DBM   = -27.0
CRC_ESCALATE      = 100


def _read(params: dict, resolver: Resolver, model: str, role: str):
    """Read a role's live value from path-keyed params + record the trace."""
    path = resolver.resolve(model, role)
    if not path:
        return None, None
    return params.get(path), path


def diagnose(model: str, params: dict, resolver: Resolver) -> dict:
    """
    Deterministic, graph-driven diagnosis.

    Returns dict:
        fault        : str
        fix_tool     : str | None
        fix_extra    : dict          (args for the fix)
        paths_read   : list[{role, path, value}]   <- the GraphRAG trace
    """
    trace: list[dict] = []

    def val(role):
        v, path = _read(params, resolver, model, role)
        if path is not None:
            trace.append({"role": role, "path": path, "value": v})
        return v

    rx      = val("rx_optical_power")
    crc     = val("dsl_crc_errors")
    conn    = str(val("ppp_connection_status") or "")
    vlan    = val("vlan_id")
    dns     = str(val("dns_servers") or "")
    lasterr = str(val("last_connection_error") or "")
    val("uptime")  # informational, recorded in trace

    def out(fault, fix, extra=None):
        return {"fault": fault, "fix_tool": fix,
                "fix_extra": extra or {}, "paths_read": trace}

    # 1) Optical signal too weak — physical, escalate.
    try:
        if rx is not None and float(rx) < RX_ESCALATE_DBM:
            return out("weak_signal", "escalate_to_technician")
    except (TypeError, ValueError):
        pass

    # 2) DSL line errors too high — hardware, escalate.
    try:
        if crc is not None and int(crc) > CRC_ESCALATE:
            return out("hardware_fault", "escalate_to_technician")
    except (TypeError, ValueError):
        pass

    # 3) VLAN misconfigured — remote fix.
    if "unconfigured" in conn.lower():
        return out("wrong_vlan", "set_vlan", {"vlan_id": CORRECT_VLAN})
    try:
        if vlan is not None and int(vlan) not in (0, CORRECT_VLAN):
            return out("wrong_vlan", "set_vlan", {"vlan_id": CORRECT_VLAN})
    except (TypeError, ValueError):
        pass

    # 4) DNS broken — remote fix.
    if dns == "0.0.0.0":
        return out("dns_failure", "set_dns",
                   {"primary": "8.8.8.8", "secondary": "1.1.1.1"})

    # 5) PPP down with auth error — remote fix (re-push credentials).
    if "disconnect" in conn.lower() and "auth" in lasterr.lower():
        return out("ppp_auth_failure", "restart_ppp")

    # 6) PPP down (generic) — remote fix.
    if "disconnect" in conn.lower() or "error" in conn.lower():
        return out("random_disconnect", "restart_ppp")

    # 7) No fault condition holds.
    return out("healthy", None)


def derive_status(model: str, params: dict, resolver: Resolver) -> str:
    """A router is UP iff diagnosis finds no fault. Status is a function of
    the parameters, never a flag a fix sets."""
    return "UP" if diagnose(model, params, resolver)["fault"] == "healthy" else "DOWN"


# ---------------------------------------------------------------------------
# 7) EDITABLE normalization + write guard (used by RaDuce)
# ---------------------------------------------------------------------------

def is_writable_role(role: str) -> bool:
    """RaDuce may only write roles on the allowlist (the editable gate)."""
    return role in WRITE_ROLES


def normalize_editable(value) -> str:
    """The graph stores editable as 'yes'/'non'/None/'read'/'write'. Normalize."""
    v = str(value or "").strip().lower()
    if v in ("yes", "write", "readwrite", "rw", "true", "1", "oui"):
        return "write"
    return "read"
