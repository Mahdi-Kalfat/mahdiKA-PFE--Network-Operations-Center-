# Router TR-069 Agent

A local LLM agent with a web UI for querying TR-069 router parameters.
Uses **qwen2.5:3b** via Ollama — fully offline, runs on your GTX 1650.

## Stack

```
Browser UI  →  Flask agent (agent.py)  →  Ollama (qwen2.5:3b)
                                       →  FastAPI server (server.py:8000)  →  Neo4j
```

## Setup

```powershell
# 1. Create and activate venv
python -m venv .venv
.venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt
```

## Start order (every time)

```powershell
# 1. Neo4j — start from Neo4j Desktop

# 2. FastAPI server (in its own terminal)
cd ..\neo4j-code-mode-agent
.venv\Scripts\activate
python server.py

# 3. Ollama (if not already running as a service)
ollama serve

# 4. Agent (in its own terminal)
cd ..\router-agent
.venv\Scripts\activate
python agent.py
```

Then open: http://localhost:5000

## Tools available

| Tool | What it does |
|---|---|
| `list_routers` | All 12 router models |
| `search_by_technology` | Filter by GPON / ADSL / VDSL / DSL |
| `get_router_parameters` | All TR-069 params for a model |
| `get_router_by_category` | Params filtered by category |
| `search_parameter` | Keyword search across all routers |

## Config (top of agent.py)

```python
OLLAMA_URL   = "http://localhost:11434"
OLLAMA_MODEL = "qwen2.5:3b"
FASTAPI_URL  = "http://localhost:8000"
```
