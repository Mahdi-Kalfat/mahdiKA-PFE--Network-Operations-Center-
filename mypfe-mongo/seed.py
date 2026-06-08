"""
seed.py — MongoDB seed script for MyPFE
Creates 4 collections: customers, router_states, admins, tickets

Run:
    python seed.py

Requires:
    pip install pymongo bcrypt python-dotenv
    MongoDB running on localhost:27017
"""

import bcrypt
import random
from datetime import datetime, timedelta
from pymongo import MongoClient, ASCENDING
from dotenv import load_dotenv
import os

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME   = os.getenv("MONGO_DB",  "mypfe")

client = MongoClient(MONGO_URI)
db     = client[DB_NAME]

# ── Helpers ───────────────────────────────────────────────────────────────────

def hash_pin(pin: str) -> str:
    return bcrypt.hashpw(pin.encode(), bcrypt.gensalt()).decode()

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def rand_serial(prefix: str) -> str:
    return f"{prefix}-{''.join([str(random.randint(0,9)) for _ in range(8)])}"

def rand_mac():
    return ":".join([f"{random.randint(0,255):02X}" for _ in range(6)])

def rand_ip():
    return f"192.168.{random.randint(1,10)}.{random.randint(2,254)}"

def rand_uptime():
    return random.randint(3600, 30 * 24 * 3600)  # 1h to 30 days in seconds

def ago(days=0, hours=0, minutes=0):
    return datetime.utcnow() - timedelta(days=days, hours=hours, minutes=minutes)

# ── Router models (from Neo4j graph) ─────────────────────────────────────────

ROUTERS = [
    # Huawei GPON
    {"model": "HG8145V5",   "vendor": "Huawei", "technology": "GPON",  "serial_prefix": "HW-ONT"},
    {"model": "HG8245H5",   "vendor": "Huawei", "technology": "GPON",  "serial_prefix": "HW-ONT"},
    {"model": "HG8145X7-10","vendor": "Huawei", "technology": "GPON",  "serial_prefix": "HW-ONT"},
    {"model": "V163",       "vendor": "Huawei", "technology": "GPON",  "serial_prefix": "HW-ONT"},
    {"model": "V166a-20",   "vendor": "Huawei", "technology": "GPON",  "serial_prefix": "HW-ONT"},
    # Nokia GPON
    {"model": "G-1425G-B",  "vendor": "Nokia",  "technology": "GPON",  "serial_prefix": "NK-ONT"},
    {"model": "G-2425G-A",  "vendor": "Nokia",  "technology": "GPON",  "serial_prefix": "NK-ONT"},
    {"model": "G-240W-A",   "vendor": "Nokia",  "technology": "GPON",  "serial_prefix": "NK-ONT"},
    {"model": "G-240W-F",   "vendor": "Nokia",  "technology": "GPON",  "serial_prefix": "NK-ONT"},
    {"model": "G-1426G-D",  "vendor": "Nokia",  "technology": "GPON",  "serial_prefix": "NK-ONT"},
    # D-Link DSL
    {"model": "IGD",        "vendor": "D-Link", "technology": "DSL",   "serial_prefix": "DL-DSL"},
    # TP-Link VDSL
    {"model": "VC220-G3v",  "vendor": "TP-Link","technology": "VDSL",  "serial_prefix": "TP-DSL"},
]

# ── Fault scenarios ───────────────────────────────────────────────────────────

FAULTS = {
    "healthy": {
        "status": "UP",
        "params_override": {
            "PPPStatus": "Connected",
            "LastConnectionError": "None",
            "Uptime": rand_uptime,
            "ConnectionStatus": "Connected",
        }
    },
    "ppp_auth_failure": {
        "status": "DOWN",
        "params_override": {
            "PPPStatus": "Disconnected",
            "LastConnectionError": "AuthenticationFailure",
            "Uptime": 0,
            "ConnectionStatus": "Disconnected",
        }
    },
    "wrong_vlan": {
        "status": "DOWN",
        "params_override": {
            "PPPStatus": "Disconnected",
            "ConnectionStatus": "Unconfigured",
            "VLANId": 999,
            "Uptime": 0,
        }
    },
    "dns_failure": {
        "status": "DOWN",
        "params_override": {
            "DNSServer": "0.0.0.0",
            "DNSStatus": "Error",
            "PPPStatus": "Connected",
        }
    },
    "weak_signal": {
        "status": "DOWN",
        "params_override": {
            "RXPower": -30.5,
            "SignalLoss": True,
            "PPPStatus": "Disconnected",
            "Uptime": 0,
        }
    },
    "random_disconnect": {
        "status": "DOWN",
        "params_override": {
            "PPPStatus": "Disconnected",
            "LastConnectionError": "ServerTimeout",
            "Uptime": 0,
        }
    },
    "hardware_fault": {
        "status": "DOWN",
        "params_override": {
            "ErrorCount": 847,
            "PPPStatus": "Disconnected",
            "ConnectionStatus": "Error",
            "Temperature": 92.4,
            "Uptime": 0,
        }
    },
}

def make_base_params(router_info: dict, correct_vlan: int = 100) -> dict:
    """Generate realistic base TR-069 parameter values for a router."""
    return {
        # Basic
        "MACAddress":          rand_mac(),
        "SerialNumber":        rand_serial(router_info["serial_prefix"]),
        "IPAddress":           rand_ip(),
        "OUI":                 f"{random.randint(0,255):02X}{random.randint(0,255):02X}{random.randint(0,255):02X}",
        # PPP
        "PPPStatus":           "Connected",
        "LastConnectionError": "None",
        "ConnectionStatus":    "Connected",
        "PPPUsername":         f"user{random.randint(1000,9999)}@topnet.tn",
        "VLANId":              correct_vlan,
        "ExternalIPAddress":   f"197.{random.randint(1,254)}.{random.randint(1,254)}.{random.randint(1,254)}",
        "IPv6Address":         f"2001:db8::{random.randint(1,9999):x}",
        # WiFi
        "SSID_2G":             f"TOPNET-{random.randint(1000,9999)}",
        "SSID_5G":             f"TOPNET-{random.randint(1000,9999)}-5G",
        "WiFiPassword":        f"{''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=10))}",
        "WiFiChannel_2G":      random.choice([1, 6, 11]),
        "WiFiChannel_5G":      random.choice([36, 40, 44, 48, 149, 153]),
        "WiFiBandwidth_2G":    "20MHz",
        "WiFiBandwidth_5G":    "80MHz",
        # Voice
        "SIP_URI":             f"sip:{random.randint(20000000,29999999)}@voip.topnet.tn",
        "SIP_ProxyServer":     "sip.topnet.tn",
        "SIP_Port":            5060,
        # Diagnostic (GPON only)
        "RXPower":             round(random.uniform(-18.0, -12.0), 2),
        "TXPower":             round(random.uniform(0.5, 5.0), 2),
        "Temperature":         round(random.uniform(38.0, 55.0), 2),
        "BiasCurrent":         round(random.uniform(8.0, 20.0), 2),
        "SignalLoss":          False,
        # LAN
        "ConnectedHosts":      random.randint(1, 8),
        "DNSServer":           random.choice(["8.8.8.8", "1.1.1.1", "208.67.222.222"]),
        "DNSStatus":           "Active",
        "ErrorCount":          random.randint(0, 5),
        # Uptime
        "Uptime":              rand_uptime(),
    }


# ── Customers ─────────────────────────────────────────────────────────────────

CUSTOMERS_RAW = [
    # phone, name, city, plan, router_idx, fault, pin
    ("0661234567", "Mohamed Ben Ali",      "Tunis",    "Fiber 100MB", 0,  "ppp_auth_failure",  "1234"),
    ("0662345678", "Fatima Zahra Mansour", "Sfax",     "Fiber 200MB", 5,  "weak_signal",       "2345"),
    ("0663456789", "Karim Trabelsi",       "Sousse",   "Fiber 100MB", 1,  "healthy",           "3456"),
    ("0664567890", "Amira Bouaziz",        "Tunis",    "Fiber 500MB", 6,  "wrong_vlan",        "4567"),
    ("0665678901", "Youssef Gharbi",       "Bizerte",  "Fiber 100MB", 2,  "healthy",           "5678"),
    ("0666789012", "Nour El Houda Riahi",  "Tunis",    "Fiber 200MB", 7,  "dns_failure",       "6789"),
    ("0667890123", "Sami Jlassi",          "Nabeul",   "Fiber 100MB", 3,  "healthy",           "7890"),
    ("0668901234", "Leila Hammami",        "Monastir", "Fiber 200MB", 8,  "random_disconnect", "8901"),
    ("0669012345", "Bilel Saidi",          "Tunis",    "Fiber 500MB", 4,  "hardware_fault",    "9012"),
    ("0660123456", "Rania Khelifi",        "Ariana",   "Fiber 100MB", 9,  "healthy",           "0123"),
    ("0671234567", "Hedi Ferchichi",       "Gabes",    "DSL 20MB",    10, "ppp_auth_failure",  "1357"),
    ("0672345678", "Meryem Oueslati",      "Tunis",    "VDSL 50MB",   11, "dns_failure",       "2468"),
    ("0673456789", "Amine Chaabane",       "Sfax",     "Fiber 100MB", 0,  "healthy",           "3579"),
    ("0674567890", "Sonia Ben Romdhane",   "Tunis",    "Fiber 200MB", 5,  "wrong_vlan",        "4680"),
    ("0675678901", "Tarek Zouari",         "Sousse",   "Fiber 100MB", 1,  "healthy",           "5791"),
]

def build_customers_and_states():
    customers_docs    = []
    router_state_docs = []

    for i, (phone, name, city, plan, router_idx, fault, pin) in enumerate(CUSTOMERS_RAW):
        router_info   = ROUTERS[router_idx]
        correct_vlan  = 100
        account_id    = f"TN-2024-{(i+1):05d}"
        serial        = rand_serial(router_info["serial_prefix"])

        # Customer document
        cust = {
            "phone":             phone,
            "pin_hash":          hash_pin(pin),
            "name":              name,
            "city":              city,
            "account_id":        account_id,
            "router_model":      router_info["model"],
            "router_serial":     serial,
            "vendor":            router_info["vendor"],
            "technology":        router_info["technology"],
            "subscription_plan": plan,
            "created_at":        ago(days=random.randint(30, 730)),
            "language":          "fr",   # default language preference
        }
        customers_docs.append(cust)

        # Router state document
        base_params = make_base_params(router_info, correct_vlan)
        fault_info  = FAULTS[fault]

        # Apply fault overrides
        for k, v in fault_info["params_override"].items():
            base_params[k] = v() if callable(v) else v

        state = {
            "serial":       serial,
            "account_id":   account_id,
            "model":        router_info["model"],
            "vendor":       router_info["vendor"],
            "technology":   router_info["technology"],
            "status":       fault_info["status"],
            "fault":        fault,
            "correct_vlan": correct_vlan,
            "last_updated": datetime.utcnow(),
            "parameters":   base_params,
        }
        router_state_docs.append(state)

    return customers_docs, router_state_docs


# ── Admins ────────────────────────────────────────────────────────────────────

ADMINS_RAW = [
    ("mehdi",       "admin123",  "Mehdi",          "superadmin"),
    ("noc_op1",     "noc2025!",  "Karim Operator",  "admin"),
    ("noc_op2",     "noc2025!",  "Sonia Operator",  "admin"),
]

def build_admins():
    return [
        {
            "username":      uname,
            "password_hash": hash_password(pwd),
            "name":          name,
            "role":          role,
            "created_at":    ago(days=365),
            "last_login":    ago(hours=random.randint(1, 48)),
        }
        for uname, pwd, name, role in ADMINS_RAW
    ]


# ── Sample tickets ────────────────────────────────────────────────────────────

def build_tickets(customer_ids):
    """Create a few historical resolved tickets for some customers."""
    tickets = []
    sample_pairs = [
        (0, "ppp_auth_failure", "restart_ppp",          "resolved"),
        (2, "dns_failure",      "set_dns",               "resolved"),
        (4, "wrong_vlan",       "set_vlan",              "resolved"),
        (6, "weak_signal",      "escalate_to_technician","escalated"),
        (8, "random_disconnect","restart_ppp",           "resolved"),
    ]
    for cust_idx, fault, fix, status in sample_pairs:
        created  = ago(days=random.randint(1, 30), hours=random.randint(0, 23))
        resolved = created + timedelta(minutes=random.randint(1, 5)) if status != "escalated" else None
        tickets.append({
            "customer_id":          customer_ids[cust_idx],
            "account_id":           CUSTOMERS_RAW[cust_idx][4-4+2],   # just a ref
            "created_at":           created,
            "resolved_at":          resolved,
            "problem_description":  random.choice([
                "my internet doesn't work",
                "connection is very slow",
                "internet is down since this morning",
                "I can't connect to the internet",
                "my router shows a red light",
            ]),
            "fault_detected":       fault,
            "fix_applied":          fix if status != "escalated" else None,
            "status":               status,
            "resolution_time_s":    int((resolved - created).total_seconds()) if resolved else None,
            "agent_trace": [
                "authenticate_customer",
                "get_customer_by_phone",
                "get_router_state",
                "get_router_parameters",
                "diagnose_fault",
                fix,
                "get_router_state",   # verify
                "create_ticket",
            ],
            "escalation_reason":    "RX power below threshold — physical technician required" if status == "escalated" else None,
        })
    return tickets


# ── Main ──────────────────────────────────────────────────────────────────────

def seed():
    print(f"\n  MyPFE — MongoDB Seed")
    print(f"  URI : {MONGO_URI}")
    print(f"  DB  : {DB_NAME}\n")

    # Drop existing collections
    for col in ["customers", "router_states", "admins", "tickets"]:
        db[col].drop()
        print(f"  Dropped collection: {col}")

    # Customers + router states
    customers_docs, state_docs = build_customers_and_states()

    result = db.customers.insert_many(customers_docs)
    customer_ids = result.inserted_ids
    print(f"\n  ✓ Inserted {len(customer_ids)} customers")

    # Patch router_states with customer ObjectId
    for i, state in enumerate(state_docs):
        state["customer_id"] = customer_ids[i]
    db.router_states.insert_many(state_docs)
    print(f"  ✓ Inserted {len(state_docs)} router states")

    # Admins
    admin_docs = build_admins()
    db.admins.insert_many(admin_docs)
    print(f"  ✓ Inserted {len(admin_docs)} admins")

    # Tickets
    ticket_docs = build_tickets(customer_ids)
    db.tickets.insert_many(ticket_docs)
    print(f"  ✓ Inserted {len(ticket_docs)} historical tickets")

    # Indexes
    db.customers.create_index([("phone", ASCENDING)], unique=True)
    db.customers.create_index([("account_id", ASCENDING)], unique=True)
    db.router_states.create_index([("serial", ASCENDING)], unique=True)
    db.router_states.create_index([("customer_id", ASCENDING)])
    db.admins.create_index([("username", ASCENDING)], unique=True)
    db.tickets.create_index([("customer_id", ASCENDING)])
    db.tickets.create_index([("status", ASCENDING)])
    print(f"  ✓ Indexes created")

    # Summary
    print(f"\n  ─────────────────────────────────")
    print(f"  Customers    : {db.customers.count_documents({})}")
    print(f"  Router states: {db.router_states.count_documents({})}")
    print(f"    UP (healthy): {db.router_states.count_documents({'status': 'UP'})}")
    print(f"    DOWN (fault): {db.router_states.count_documents({'status': 'DOWN'})}")
    print(f"  Admins       : {db.admins.count_documents({})}")
    print(f"  Tickets      : {db.tickets.count_documents({})}")
    print(f"  ─────────────────────────────────")

    print(f"\n  Test credentials:")
    for phone, name, city, plan, _, fault, pin in CUSTOMERS_RAW[:5]:
        print(f"    {phone}  PIN: {pin}  ({name} / {fault})")
    print(f"\n  Admin credentials:")
    for uname, pwd, name, role in ADMINS_RAW:
        print(f"    {uname} / {pwd}  ({role})")
    print()


if __name__ == "__main__":
    seed()
