# MyPFE — MongoDB + MCP Servers

## Files

| File | What it does | Port |
|---|---|---|
| `seed.py` | Creates all MongoDB collections with fake customers + router states | — |
| `genie_server.py` | GenieACS MCP — reads router state, fault injection for NOC | 8001 |
| `raduce_server.py` | RaDuce MCP — pushes fixes (PPP, VLAN, DNS, reboot, escalate) | 8002 |
| `customer_server.py` | Customer MCP — auth (phone+PIN), tickets, admin auth | 8003 |

## Setup

```powershell
# 1. Create venv
python -m venv .venv
.venv\Scripts\activate

# 2. Install
pip install -r requirements.txt

# 3. Make sure MongoDB is running (MongoDB Compass or mongod service)
# Default: mongodb://localhost:27017

# 4. Seed the database (run once)
python seed.py
```

## Start order

```powershell
# Terminal 1 — GenieACS (read)
python genie_server.py

# Terminal 2 — RaDuce (write/fix)
python raduce_server.py

# Terminal 3 — Customer (auth + tickets)
python customer_server.py
```

## Test credentials

| Phone | PIN | Name | Fault |
|---|---|---|---|
| 0661234567 | 1234 | Mohamed Ben Ali | PPP auth failure |
| 0662345678 | 2345 | Fatima Zahra Mansour | Weak optical signal |
| 0663456789 | 3456 | Karim Trabelsi | Healthy |
| 0664567890 | 4567 | Amira Bouaziz | Wrong VLAN |
| 0665678901 | 5678 | Youssef Gharbi | Healthy |
| 0666789012 | 6789 | Nour El Houda Riahi | DNS failure |
| 0668901234 | 8901 | Leila Hammami | Random disconnect |
| 0669012345 | 9012 | Bilel Saidi | Hardware fault |

## Admin credentials

| Username | Password | Role |
|---|---|---|
| mehdi | admin123 | superadmin |
| noc_op1 | noc2025! | admin |
| noc_op2 | noc2025! | admin |

## Quick verify (after seed + servers running)

```
GET http://localhost:8001/health          # GenieACS
GET http://localhost:8001/noc/fleet       # All routers
GET http://localhost:8002/health          # RaDuce
GET http://localhost:8003/health          # Customer
GET http://localhost:8003/tools/authenticate_customer?phone=0661234567&pin=1234
```





Test credentials:
    0661234567  PIN: 1234  (Mohamed Ben Ali / ppp_auth_failure)
    0662345678  PIN: 2345  (Fatima Zahra Mansour / weak_signal)
    0663456789  PIN: 3456  (Karim Trabelsi / healthy)
    0664567890  PIN: 4567  (Amira Bouaziz / wrong_vlan)
    0665678901  PIN: 5678  (Youssef Gharbi / healthy)

  Admin credentials:
    mehdi / admin123  (superadmin)
    noc_op1 / noc2025!  (admin)
    noc_op2 / noc2025!  (admin)