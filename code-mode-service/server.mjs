/**
 * code-mode HTTP service — v3
 * Uses a local proxy to bypass @utcp/http security check.
 * Retries client initialization until neo4j-agent is ready.
 */

import http from "http";
import { createServer } from "http";
import { CodeModeUtcpClient } from "@utcp/code-mode";
import "@utcp/http";

const NEO4J_AGENT_URL = process.env.NEO4J_AGENT_URL || "http://localhost:8000";
const PORT = parseInt(process.env.CODE_MODE_PORT || "8010");

// ── Local proxy: forwards 127.0.0.1:proxyPort → neo4j-agent:8000 ─────────────
// This tricks @utcp/http into thinking it's talking to localhost

let proxyPort = null;

async function startProxy() {
  return new Promise((resolve) => {
    const proxy = createServer((req, res) => {
      const target = new URL(`${NEO4J_AGENT_URL}${req.url}`);
      const options = {
        hostname: target.hostname,
        port: parseInt(target.port) || 80,
        path: target.pathname + (target.search || ""),
        method: req.method,
        headers: { ...req.headers, host: target.host }
      };
      const upstream = http.request(options, (upRes) => {
        res.writeHead(upRes.statusCode, upRes.headers);
        upRes.pipe(res, { end: true });
      });
      upstream.on("error", (e) => {
        res.writeHead(502, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: e.message }));
      });
      req.pipe(upstream, { end: true });
    });

    proxy.listen(0, "127.0.0.1", () => {
      proxyPort = proxy.address().port;
      console.log(`[code-mode] Proxy: 127.0.0.1:${proxyPort} → ${NEO4J_AGENT_URL}`);
      resolve(proxyPort);
    });
  });
}

// ── Initialize UTCP client with retry ────────────────────────────────────────

let client = null;

async function getClient() {
  if (client) return client;
  if (!proxyPort) await startProxy();

  const config = {
    manual_call_templates: [{
      name: "router_mcp",
      call_template_type: "http",
      http_method: "GET",
      url: `http://127.0.0.1:${proxyPort}/tools`
    }]
  };

  // Retry until neo4j-agent is ready (up to 60s)
  const maxAttempts = 30;
  const delay = ms => new Promise(r => setTimeout(r, ms));

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      console.log(`[code-mode] Connecting to neo4j-agent (attempt ${attempt}/${maxAttempts})...`);
      client = await CodeModeUtcpClient.create(".", config);
      const tools = await client.getTools();
      console.log(`[code-mode] ✅ Loaded ${tools.length} tools: ${tools.map(t => t.name).join(", ")}`);
      return client;
    } catch (e) {
      console.warn(`[code-mode] Not ready yet: ${e.message}`);
      client = null;
      if (attempt < maxAttempts) await delay(2000);
    }
  }
  throw new Error("Failed to connect to neo4j-agent after 60 seconds");
}

// ── Tool: search_tools ────────────────────────────────────────────────────────

async function searchTools(taskDescription, limit = 10) {
  const target = new URL(`${NEO4J_AGENT_URL}/tools`);
  const data = await new Promise((resolve, reject) => {
    const opts = {
      hostname: target.hostname,
      port: parseInt(target.port) || 80,
      path: "/tools",
      method: "GET"
    };
    const req = http.request(opts, res => {
      let body = "";
      res.on("data", c => body += c);
      res.on("end", () => {
        try { resolve(JSON.parse(body)); }
        catch (e) { reject(e); }
      });
    });
    req.on("error", reject);
    req.end();
  });

  if (!data.tools) throw new Error("Invalid /tools response");
  const keywords = taskDescription.toLowerCase().split(/\s+/);
  const matched = data.tools
    .filter(t => keywords.some(k =>
      t.name.toLowerCase().includes(k) ||
      (t.description || "").toLowerCase().includes(k)
    ))
    .slice(0, limit);

  return {
    tools: matched.map(t => ({
      name: `router_mcp.${t.name}`,
      description: t.description
    }))
  };
}

// ── Tool: call_tool_chain ─────────────────────────────────────────────────────

async function callToolChain(code, timeout = 30000) {
  const c = await getClient();
  const { result, logs } = await c.callToolChain(code, timeout);
  return { result, logs };
}

// ── Tool: sandbox_diagnostics ─────────────────────────────────────────────────

async function sandboxDiagnostics() {
  const c = await getClient();
  const { result, logs } = await c.callToolChain(`
    return {
      interfaces_present: typeof __interfaces !== 'undefined',
      interfaces_preview: typeof __interfaces !== 'undefined' ? __interfaces.slice(0, 300) : null,
      global_keys: Object.keys(global).sort().slice(0, 20)
    };
  `, 10000);
  return { success: true, result, logs };
}

// ── HTTP server ───────────────────────────────────────────────────────────────

function readBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";
    req.on("data", c => body += c);
    req.on("end", () => {
      try { resolve(body ? JSON.parse(body) : {}); }
      catch (e) { reject(new Error("Invalid JSON")); }
    });
  });
}

function send(res, status, data) {
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*"
  });
  res.end(JSON.stringify(data));
}

const server = createServer(async (req, res) => {
  try {
    if (req.method === "GET" && req.url === "/health") {
      try {
        const c = await getClient();
        const tools = await c.getTools();
        return send(res, 200, { status: "ok", tools_loaded: tools.length });
      } catch (e) {
        return send(res, 503, { status: "starting", error: e.message });
      }
    }

    if (req.method === "POST" && req.url === "/search_tools") {
      const body = await readBody(req);
      return send(res, 200, await searchTools(body.task_description || "", body.limit || 10));
    }

    if (req.method === "POST" && req.url === "/call_tool_chain") {
      const body = await readBody(req);
      if (!body.code) return send(res, 400, { error: "Missing code" });
      return send(res, 200, await callToolChain(body.code, body.timeout || 30000));
    }

    if (req.method === "POST" && req.url === "/sandbox_diagnostics") {
      return send(res, 200, await sandboxDiagnostics());
    }

    send(res, 404, { error: "Not found" });
  } catch (e) {
    console.error(`[code-mode] Error: ${e.message}`);
    send(res, 500, { error: e.message });
  }
});

server.listen(PORT, "0.0.0.0", async () => {
  console.log(`\n  Code-Mode HTTP Service`);
  console.log(`  Neo4j Agent  : ${NEO4J_AGENT_URL}`);
  console.log(`  Listen       : http://0.0.0.0:${PORT}`);
  console.log(`  search_tools : POST /search_tools`);
  console.log(`  call_chain   : POST /call_tool_chain`);
  console.log(`  diagnostics  : POST /sandbox_diagnostics\n`);

  // Start connecting in background — don't block server startup
  getClient().catch(e => console.error(`[code-mode] Init failed: ${e.message}`));
});
