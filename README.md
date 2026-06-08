# MyPFE — ISP Automated Support System

> Final Year Engineering Project — ESPRIT Tunis  
> AI-powered ISP customer support with TR-069 router diagnostics, GraphRAG, and Code-Mode tool execution.

---

## Project Overview

MyPFE is a full-stack AI system that automates ISP (Internet Service Provider) technical support. When a customer reports an internet issue, the system automatically diagnoses the problem by reading live router state, cross-referencing TR-069 parameters from a knowledge graph, applies a remote fix, and creates a support ticket — all without human intervention.

### Architecture

```
Customer Browser
    │
    ├── :5000  mypfe-app (Flask)          ← Customer Portal + NOC Dashboard
    │           ├── :8001  genie          ← GenieACS mock (live router state)
    │           ├── :8002  raduce         ← RaDuce MCP (remote fix execution)
    │           ├── :8003  customer       ← Customer auth + ticket management
    │           ├── :8000  neo4j-agent    ← TR-069 GraphRAG (Neo4j)
    │           └── :11434 ollama         ← Local LLM (qwen2.5:3b)
    │
    └── :5004  router-agent (Flask)       ← Router TR-069 Assistant
                ├── :11434 ollama         ← LLM reasoning
                └── :8010  code-mode      ← Code-Mode sandbox (Node.js VM)
                            └── :8000  neo4j-agent

External (Windows):
    Neo4j Desktop (routers-db)  ← GraphRAG knowledge base (12 router models)
```

### Key Technologies

| Technology | Role |
|-----------|------|
| **Neo4j GraphRAG** | Knowledge graph of 12 router models with TR-069 parameters |
| **Code-Mode (UTCP)** | Sandboxed TypeScript execution for tool chaining (1 call instead of many) |
| **Ollama qwen2.5:3b** | Local LLM — no cloud API dependency |
| **GenieACS mock** | Simulates a real ISP ACS (Auto Configuration Server) |
| **MongoDB** | Customer accounts, router states, support tickets |
| **Docker** | Full containerized deployment (10 services) |

---

## Prerequisites

- **Docker Desktop** — [Download](https://www.docker.com/products/docker-desktop/)
- **Neo4j Desktop** — [Download](https://neo4j.com/download/) with your `routers-db` database
- **Git** — [Download](https://git-scm.com/)
- At least **8 GB RAM** allocated to Docker

---

## Setup & Installation

### 1. Clone the repository

```bash
git clone https://github.com/YOUR_USERNAME/mypfe.git
cd mypfe
```

### 2. Configure your Neo4j password

Open `docker-compose.yml` and find the `neo4j-agent` service. Update the password to match your Neo4j Desktop password:

```yaml
neo4j-agent:
  environment:
    NEO4J_PASSWORD: your_password_here   # ← change this
    NEO4J_DATABASE: neo4j                # ← your database name
```

> To find your database name: open Neo4j Desktop → click your instance → "Open" → run `SHOW DATABASES`

### 3. Start Neo4j Desktop

- Open **Neo4j Desktop**
- Start your **routers-db** instance
- Wait for the green **RUNNING** status

### 4. Start Docker Desktop

- Open **Docker Desktop**
- Wait for the green whale icon in the system tray

---

## Starting the Project

### First time (builds all Docker images)

```bash
docker compose up --build -d
```

This will take 5-10 minutes on first run as it builds all images.

### Pull the Ollama AI model (first time only, ~2 GB)

```bash
docker exec -it mypfe-ollama ollama pull qwen2.5:3b
```

Wait for the download to complete before using the apps.

### Every time after that

```bash
docker compose up -d
```

---

## Accessing the Applications

| Application | URL | Description |
|------------|-----|-------------|
| Customer Portal | http://localhost:5000 | Customer login + automated support |
| NOC Dashboard | http://localhost:5000/noc | Network Operations Center view |
| Router TR-069 Agent | http://localhost:5004 | AI assistant for router parameters |
| Neo4j Agent API | http://localhost:8000 | GraphRAG REST API |
| GenieACS MCP | http://localhost:8001 | Router state service |
| RaDuce MCP | http://localhost:8002 | Remote fix service |
| Customer MCP | http://localhost:8003 | Customer management service |
| Code-Mode Service | http://localhost:8010 | Sandbox execution service |

---

## Verifying Everything Works

```bash
# Check all containers are running
docker compose ps

# Check Neo4j connected successfully
docker compose logs neo4j-agent | grep "Connected"
# Expected: [server] Connected to Neo4j at bolt://host.docker.internal:7687

# Check Code-Mode loaded router tools
docker compose logs code-mode | grep "tools"
# Expected: [code-mode] ✅ Loaded 5 tools: router_mcp.list_routers, ...

# Check all 12 routers are in the database
curl http://localhost:8000/tools/list_routers
# Expected: {"total":12,"routers":[...]}
```

---

## Stopping the Project

```bash
# Stop all containers (keeps data)
docker compose down

# Stop and remove all data volumes (full reset)
docker compose down -v
```

---

## Project Structure

```
mypfe/
├── docker-compose.yml              # Orchestrates all 10 services
│
├── neo4j-agent-server.py           # FastAPI — TR-069 GraphRAG server
├── Dockerfile.neo4j-agent
│
├── code-mode-service/
│   ├── server.mjs                  # Node.js HTTP wrapper for Code-Mode sandbox
│   └── package.json
├── Dockerfile.code-mode
│
├── router-agent/
│   ├── agent.py                    # Flask — Router TR-069 AI assistant
│   └── templates/index.html
├── Dockerfile.router-agent
│
├── mypfe-app/
│   ├── app.py                      # Flask — Customer Portal + NOC Dashboard
│   └── templates/
├── Dockerfile.mypfe-app
│
├── mypfe-mongo/
│   ├── genie_server.py             # FastAPI — GenieACS mock server
│   ├── raduce_server.py            # FastAPI — RaDuce MCP server
│   ├── customer_server.py          # FastAPI — Customer MCP server
│   └── seed.py                     # MongoDB seed script
│
└── code-mode-mcp/                  # Claude Desktop MCP server (Windows only)
    ├── index.ts
    └── dist/
```

---

## How It Works — Diagnostic Flow

When a customer reports "my internet is not working":

```
1. Customer logs in with phone + PIN
2. GenieACS MCP reads live router state (PPP status, signal, errors)
3. Neo4j MCP fetches TR-069 parameter schema for that router model
4. Diagnostic Engine cross-references live values vs expected schema
5. Fault detected (e.g. ppp_auth_failure, weak_signal, vlan_mismatch)
6. RaDuce MCP sends SetParameterValues to fix the router remotely
7. GenieACS MCP re-reads router state to confirm fix
8. Support ticket created automatically with full diagnostic trace
```

### Code-Mode Execution (Router Agent)

The Router TR-069 Agent uses **Code-Mode** — instead of making multiple individual tool calls, Ollama writes TypeScript code that runs in a sandboxed Node.js VM:

```typescript
// Ollama writes this code — executed in isolated sandbox
const params = router_mcp.get_router_parameters({ model: "HG8145V5" });
const ppp = params.parameters.filter(p => p.category === "ppp_info");
return { router: params.router_model, ppp_params: ppp };
```

This is **67-88% more efficient** than traditional tool calling (single execution vs multiple API round trips).

---

## Troubleshooting

**Neo4j connection refused**
- Make sure Neo4j Desktop is running and `routers-db` shows RUNNING
- Check `NEO4J_PASSWORD` in `docker-compose.yml` matches your actual password

**Code-mode shows "Loaded 0 tools"**
- Wait 30 seconds — it retries automatically until neo4j-agent is ready
- Run `docker compose logs code-mode` to see retry attempts

**Ollama model not found**
- Run `docker exec -it mypfe-ollama ollama pull qwen2.5:3b`

**Port already in use**
- Another service is using one of the ports (8000, 8001, 8002, 8003, 8010, 5000, 5004)
- Stop the conflicting service or change the port in `docker-compose.yml`

---

## Git Setup

### Initialize and push to GitHub

```bash
# Initialize git repository
git init

# Create .gitignore
echo "node_modules/" >> .gitignore
echo "__pycache__/" >> .gitignore
echo "*.pyc" >> .gitignore
echo ".venv/" >> .gitignore
echo "*.env" >> .gitignore

# Add all files
git add .

# First commit
git commit -m "Initial commit — MyPFE Docker setup"

# Add your GitHub repository as remote
git remote add origin https://github.com/YOUR_USERNAME/mypfe.git

# Push
git push -u origin main
```

### Daily workflow

```bash
# Check what changed
git status

# Stage changes
git add .

# Commit with a message
git commit -m "Fix: neo4j database connection"

# Push to GitHub
git push
```

---

## Environment Variables Reference

| Variable | Service | Default | Description |
|----------|---------|---------|-------------|
| `NEO4J_URI` | neo4j-agent | `bolt://host.docker.internal:7687` | Neo4j Desktop connection |
| `NEO4J_USER` | neo4j-agent | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | neo4j-agent | `password` | Neo4j password |
| `NEO4J_DATABASE` | neo4j-agent | `neo4j` | Neo4j database name |
| `OLLAMA_URL` | router-agent, mypfe-app | `http://ollama:11434` | Ollama LLM service |
| `CODE_MODE_URL` | router-agent | `http://code-mode:8010` | Code-Mode sandbox |
| `MONGO_URI` | genie, raduce, customer | `mongodb://mongodb:27017` | MongoDB connection |

---

*ESPRIT Tunis — Génie Informatique — 2025/2026*
