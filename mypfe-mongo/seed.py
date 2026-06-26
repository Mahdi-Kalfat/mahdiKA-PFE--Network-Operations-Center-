"""
seed.py  —  Seed MongoDB for MyPFE (graph-aligned, path-keyed)

What changed vs v1
------------------
* Customers now use the REAL router models that exist in the Neo4j graph
  (Huawei_8145, TP-LINK_VC220-G3v, NOKIA_1425, ...) so every model resolves.
* router_states.parameters is now keyed by REAL TR-069 paths (a real ACS
  device tree), built from the role-based fault model in mcp_engine.
* Faults are assigned per technology (optical weak_signal only to GPON, DSL
  CRC hardware faults only to DSL), so the seeded data is physically coherent.
* status is derived from the parameters, not hard-coded.

Run:
    python seed.py
"""

import os
from datetime import datetime, timedelta

import bcrypt
from dotenv import load_dotenv
from pymongo import MongoClient

import mcp_engine as E

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.getenv("MONGO_DB",  "mypfe")
NEO4J_API = os.getenv("NEO4J_API", "http://neo4j-agent:8000")

client   = MongoClient(MONGO_URI)
db       = client[MONGO_DB]
resolver = E.Resolver(NEO4J_API)


def hash_pw(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


# phone, pin, name, fault, vendor, model(=graph id), serial, technology, plan
CUSTOMERS = [
    ("0661234567", "1234", "Mohamed Ben Ali",      "ppp_auth_failure",  "Huawei",  "Huawei_8145",       "SN-HW-1001", "GPON", "Fiber 100M"),
    ("0662345678", "2345", "Fatima Zahra Mansour", "weak_signal",       "Huawei",  "Huawei_HG8245H5",   "SN-HW-1002", "GPON", "Fiber 200M"),
    ("0663456789", "3456", "Karim Trabelsi",       "healthy",           "Huawei",  "HG8145X7-10",       "SN-HW-1003", "GPON", "Fiber 50M"),
    ("0664567890", "4567", "Amira Bouaziz",        "wrong_vlan",        "Nokia",   "NOKIA_G-240W-A",    "SN-NK-1004", "GPON", "Fiber 100M"),
    ("0665678901", "5678", "Youssef Gharbi",       "healthy",           "Nokia",   "NOKIA_1425",        "SN-NK-1005", "GPON", "Fiber 100M"),
    ("0666789012", "6789", "Nour El Houda Riahi",  "dns_failure",       "Nokia",   "NOKIA_2425",        "SN-NK-1006", "GPON", "Fiber 200M"),
    ("0667890123", "7890", "Sami Khelifi",         "random_disconnect", "Huawei",  "Huawei_V163",       "SN-HW-1007", "GPON", "Fiber 100M"),
    ("0668901234", "8901", "Leila Hammami",        "wrong_vlan",        "TP-Link", "TP-LINK_VC220-G3v", "SN-TP-1008", "VDSL", "ADSL 20M"),
    ("0669012345", "9012", "Bilel Saidi",          "hardware_fault",    "D-Link",  "D-Link_DSL224",     "SN-DL-1009", "VDSL", "ADSL 20M"),
    ("0660123456", "0123", "Rania Jelassi",        "ppp_auth_failure",  "Nokia",   "NOKIA_G-240W-F",    "SN-NK-1010", "GPON", "Fiber 100M"),
    ("0661230987", "1230", "Hatem Mejri",          "healthy",           "Nokia",   "G-1426G-D",         "SN-NK-1011", "GPON", "Fiber 50M"),
    ("0662340876", "2340", "Sonia Belhadj",        "weak_signal",       "Huawei",  "V166a-20",          "SN-HW-1012", "GPON", "Fiber 200M"),
]

ADMINS = [
    ("mehdi",   "admin123",  "superadmin"),
    ("noc_op1", "noc2025!",  "admin"),
    ("noc_op2", "noc2025!",  "admin"),
]


def seed():
    for coll in ("customers", "router_states", "admins", "tickets"):
        db[coll].drop()

    now = datetime.utcnow()

    for i, (phone, pin, name, fault, vendor, model, serial, tech, plan) in enumerate(CUSTOMERS, start=1):
        account_id = f"ACC{1000 + i}"
        cust_doc = {
            "name":              name,
            "phone":             phone,
            "pin_hash":          hash_pw(pin),
            "account_id":        account_id,
            "router_model":      model,        # == graph router id
            "vendor":            vendor,
            "technology":        tech,
            "subscription_plan": plan,
            "created_at":        now - timedelta(days=120),
        }
        cust_id = db.customers.insert_one(cust_doc).inserted_id

        # Path-keyed parameters built from the role-based fault model.
        params = E.expand_state(model, fault, resolver)
        status = E.derive_status(model, params, resolver)

        db.router_states.insert_one({
            "serial":       serial,
            "account_id":   account_id,
            "customer_id":  cust_id,
            "model":        model,
            "status":       status,
            "fault":        fault,
            "correct_vlan": E.CORRECT_VLAN,
            "last_updated": now,
            "parameters":   params,
        })

    for username, password, role in ADMINS:
        db.admins.insert_one({
            "username":      username,
            "password_hash": hash_pw(password),
            "role":          role,
            "created_at":    now,
        })

    print(f"[seed] customers     : {db.customers.count_documents({})}")
    print(f"[seed] router_states : {db.router_states.count_documents({})}")
    print(f"[seed]   up          : {db.router_states.count_documents({'status': 'UP'})}")
    print(f"[seed]   down        : {db.router_states.count_documents({'status': 'DOWN'})}")
    print(f"[seed] admins        : {db.admins.count_documents({})}")
    print("[seed] done.")


if __name__ == "__main__":
    print(f"\n  Seeding MongoDB at {MONGO_URI} / {MONGO_DB}  (path-keyed, graph-aligned)\n")
    seed()
