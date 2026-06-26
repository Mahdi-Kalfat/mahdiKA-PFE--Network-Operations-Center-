"""
agent.py  —  Router TR-069 Agent
Flow: Web UI → Ollama (qwen2.5:3b) → search_tools → call_tool_chain → Neo4j

Ollama uses exactly 3 tools mirroring code-mode-mcp:
  1. search_tools        — discover available router_mcp tools
  2. call_tool_chain     — execute TypeScript in sandbox
  3. sandbox_diagnostics — inspect sandbox state (optional)
"""

import json
import os
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
OLLAMA_URL    = os.getenv("OLLAMA_URL",    "http://localhost:11434")
OLLAMA_MODEL  = "qwen2.5:3b"
FASTAPI_URL   = os.getenv("FASTAPI_URL",   "http://localhost:8000")
CODE_MODE_URL = os.getenv("CODE_MODE_URL", "http://localhost:8010")

# ── The exact 3 tools from code-mode-mcp, exposed to Ollama ──────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_tools",
            "description": (
                "Search for available router_mcp tools by describing your task. "
                "Always call this FIRST before call_tool_chain to discover what tools exist "
                "and get their TypeScript interfaces. Returns tool names and descriptions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_description": {
                        "type": "string",
                        "description": "Natural language description of what you want to do. E.g. 'list all routers' or 'get PPP parameters for HG8145V5'"
                    },
                    "limit": {
                        "type": "number",
                        "description": "Max number of tools to return (default 10)"
                    }
                },
                "required": ["task_description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "call_tool_chain",
            "description": (
                "Execute TypeScript code in a sandboxed VM that has access to router_mcp tools. "
                "Use router_mcp.tool_name(args) syntax — tools are synchronous, no await needed. "
                "Always use return to return the final result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "TypeScript code to execute in sandbox. Example:\n"
                            "const r = router_mcp.list_routers();\n"
                            "return r;"
                        )
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Timeout in milliseconds (default 30000)"
                    }
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "sandbox_diagnostics",
            "description": "Inspect the sandbox runtime state. Use if call_tool_chain fails to debug what tools are loaded.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    }
]

SYSTEM_PROMPT = """You are a TR-069 router parameter assistant for a telecom operator.
You have access to a code execution sandbox with router_mcp tools via 3 tools.

Workflow — ALWAYS follow this order:
1. Call search_tools with a description of your task to discover available tools
2. Call call_tool_chain with TypeScript code that uses router_mcp.tool_name(args)
3. Return the result to the user in a clear, formatted way

Code rules for call_tool_chain:
- Tools are synchronous: const r = router_mcp.list_routers(); (NO await)
- Always end with: return result;
- Available tools: router_mcp.list_routers(), router_mcp.get_router_parameters({model}),
  router_mcp.get_router_by_category({model, category}), router_mcp.search_by_technology({technology}),
  router_mcp.search_parameter({keyword})

Never answer from memory — always use the tools."""


# ── Tool executors ────────────────────────────────────────────────────────────

def call_tool(name: str, args: dict) -> dict:
    try:
        if name == "search_tools":
            r = requests.post(
                f"{CODE_MODE_URL}/search_tools",
                json={"task_description": args.get("task_description", ""), "limit": args.get("limit", 10)},
                timeout=15
            )
            r.raise_for_status()
            return r.json()

        elif name == "call_tool_chain":
            r = requests.post(
                f"{CODE_MODE_URL}/call_tool_chain",
                json={"code": args.get("code", ""), "timeout": args.get("timeout", 30000)},
                timeout=35
            )
            r.raise_for_status()
            return r.json()

        elif name == "sandbox_diagnostics":
            r = requests.post(f"{CODE_MODE_URL}/sandbox_diagnostics", json={}, timeout=15)
            r.raise_for_status()
            return r.json()

        else:
            return {"error": f"Unknown tool: {name}"}

    except requests.exceptions.ConnectionError:
        return {"error": f"Cannot reach code-mode service at {CODE_MODE_URL}"}
    except Exception as e:
        return {"error": str(e)}


# ── Ollama agentic loop ───────────────────────────────────────────────────────

def chat_with_tools(user_message: str, history: list) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for turn in history[-6:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": user_message})

    max_iterations = 6  # search_tools + call_tool_chain + possible retry
    for _ in range(max_iterations):
        payload = {
            "model": OLLAMA_MODEL,
            "messages": messages,
            "tools": TOOLS,
            "stream": False,
            "options": {"temperature": 0.1, "num_predict": 2048}
        }

        try:
            resp = requests.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=60)
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            return "Cannot reach Ollama. Make sure Ollama is running."
        except Exception as e:
            return f"Error talking to Ollama: {e}"

        data = resp.json()
        msg = data.get("message", {})
        tool_calls = msg.get("tool_calls")

        if not tool_calls:
            return msg.get("content", "No response from model.")

        messages.append({
            "role": "assistant",
            "content": msg.get("content", ""),
            "tool_calls": tool_calls
        })

        for tc in tool_calls:
            fn   = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}

            result = call_tool(name, args)
            messages.append({
                "role": "tool",
                "content": json.dumps(result, ensure_ascii=False)
            })

    return "Unable to complete request after several attempts."


# ── Health check ──────────────────────────────────────────────────────────────

def check_services() -> dict:
    status = {"ollama": False, "fastapi": False, "code_mode": False, "model": OLLAMA_MODEL}
    try:
        r = requests.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        models = [m["name"] for m in r.json().get("models", [])]
        status["ollama"] = True
        status["model_available"] = any(OLLAMA_MODEL in m for m in models)
    except Exception:
        status["model_available"] = False
    try:
        r = requests.get(f"{FASTAPI_URL}/health", timeout=3)
        h = r.json()
        status["fastapi"] = h.get("status") in ("ok", "degraded")
        status["neo4j"] = h.get("neo4j") == "connected"
    except Exception:
        status["neo4j"] = False
    try:
        r = requests.get(f"{CODE_MODE_URL}/health", timeout=3)
        h = r.json()
        status["code_mode"] = h.get("status") == "ok"
        status["tools_loaded"] = h.get("tools_loaded", 0)
    except Exception:
        pass
    return status


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    return jsonify(check_services())

@app.route("/api/chat", methods=["POST"])
def api_chat():
    data    = request.json or {}
    message = data.get("message", "").strip()
    history = data.get("history", [])
    if not message:
        return jsonify({"error": "Empty message"}), 400
    answer = chat_with_tools(message, history)
    return jsonify({"answer": answer})


if __name__ == "__main__":
    print("\n  Router TR-069 Agent (Code-Mode)")
    print(f"  Model     : {OLLAMA_MODEL}")
    print(f"  Ollama    : {OLLAMA_URL}")
    print(f"  Neo4j API : {FASTAPI_URL}")
    print(f"  Code-Mode : {CODE_MODE_URL}")
    print(f"  UI        : http://localhost:5004\n")
    app.run(debug=False, host="0.0.0.0", port=5004)
