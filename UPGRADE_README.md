# MyPFE upgrade — GraphRAG-driven, path-keyed remediation (A + DNS)

This package makes the Neo4j knowledge graph **load-bearing**: router state is
stored under real TR-069 paths, RaDuce resolves each fix to the vendor-specific
path from the graph and writes it, and `status` is derived from the parameters
(UP iff no fault holds) — so a fix that doesn't clear the root cause leaves the
router DOWN and escalation fires for real.

It also fixes a latent bug: the old server read `rel.label` / `rel.editable`,
but in your graph those live on the `Parameter` node, so the graph was returning
nothing to diagnosis.

---

## What changed

| File | Change |
|------|--------|
| `mypfe-mongo/mcp_engine.py` | **NEW.** Roles, real per-vendor path map (from your dump), Neo4j resolver + cached fallback, role-based fault model, graph-driven `diagnose()`, derived `status`, editable write-gate. The core of the upgrade. |
| `mypfe-app/mcp_engine.py` | **NEW.** Identical copy (the app container copies its own folder). |
| `mypfe-mongo/raduce_server.py` | Fixes resolve role → real path, enforce the editable allowlist, write by path, re-derive status. |
| `mypfe-mongo/genie_server.py` | Reads/injects path-keyed state; adds a readable `parameters_named` view; derives status on read. |
| `mypfe-mongo/seed.py` | Uses the **real graph router ids**, tech-appropriate faults, path-keyed parameters. |
| `mypfe-app/app.py` | Diagnosis now calls `mcp_engine.diagnose()` (reads by role → real path); the trace shows real TR-069 paths. |
| `neo4j-agent-server.py` | Bug fix (`rel.* → p.*`) + new `/tools/resolve_path?model=&role=` endpoint. |
| `Dockerfile.raduce/genie/seed` | Copy `mcp_engine.py` into the image and add `requests`. |
| `neo4j-host/migrate_roles.py` | **Run once on the Neo4j host.** Stamps `role` on `HAS_PARAM`, adds the two standard TR-098 paths (`DNSServers`, `LastConnectionError`) missing from your scrape. |

---

## Where each file goes (drop-in over your repo)

```
pfeDockerV4/
├── neo4j-agent-server.py          ← replace
├── Dockerfile.raduce              ← replace
├── Dockerfile.genie               ← replace
├── Dockerfile.seed                ← replace
├── mypfe-mongo/
│   ├── mcp_engine.py              ← NEW
│   ├── raduce_server.py           ← replace
│   ├── genie_server.py            ← replace
│   └── seed.py                    ← replace
└── mypfe-app/
    ├── mcp_engine.py              ← NEW
    └── app.py                     ← replace
```

`neo4j-host/` is **not** part of the Docker build — copy it onto the Windows
machine that runs Neo4j Desktop and run it there.

> `Dockerfile.mypfe-app` and `Dockerfile.neo4j-agent` need **no changes**
> (the app Dockerfile already copies its whole folder; the agent needs no engine).

---

## Install steps

1. Copy the files into your repo as shown above.

2. On the **Neo4j host** (where Neo4j Desktop runs), stamp the roles + add the
   two missing paths (one time, idempotent):
   ```bash
   pip install neo4j
   # set NEO4J_PASSWORD / NEO4J_DATABASE if not the defaults
   python migrate_roles.py
   ```
   Expected tail: `Stamped 80+ (model, role) edges. Roles now in graph: ...`

3. Rebuild and reseed:
   ```bash
   docker compose up --build -d
   docker compose run --rm seed         # or: docker compose up seed
   ```

4. Verify:
   ```bash
   # role resolves to the right vendor path
   curl "http://localhost:8000/tools/resolve_path?model=Huawei_8145&role=vlan_id"
   curl "http://localhost:8000/tools/resolve_path?model=TP-LINK_VC220-G3v&role=vlan_id"
   # router state now path-keyed (+ readable view)
   curl "http://localhost:8001/tools/get_router_state?serial=SN-NK-1004"
   ```

If Neo4j or the resolve endpoint is unreachable, the system still works — it
falls back to the cached path map in `mcp_engine.py` (which mirrors your graph).

---

## The 60-second jury demo

Inject the **same logical fault** on two different vendors and show the system
resolving to two **different real paths**:

```bash
# Huawei  -> writes ...WANPPPConnection.1.X_HW_VLAN
curl -X POST localhost:5000/api/noc/inject_fault -H 'Content-Type: application/json' \
     -d '{"serial":"SN-HW-1001","fault":"wrong_vlan"}'

# TP-Link -> writes ...WANPTMLinkConfig.X_TP_VID
curl -X POST localhost:5000/api/noc/inject_fault -H 'Content-Type: application/json' \
     -d '{"serial":"SN-TP-1008","fault":"wrong_vlan"}'
```

Then run the customer support flow for each and point at the diagnostic trace:
the `tr069_paths_read` and the RaDuce `tr069_path_written` are vendor-specific.
That is the proof the knowledge graph is doing real work and couldn't be a
hardcoded dictionary.

---

## One-line answers for the three predictable questions

- **What does Neo4j actually do?** It maps a logical role (e.g. `vlan_id`) to the
  exact vendor TR-069 path for each model; RaDuce cannot write the fix without it.
- **Is this real automation or DB edits?** The decision pipeline is automated and
  graph-driven; the device layer is a simulated ACS (MongoDB device tree keyed by
  real paths), which a real GenieACS NBI could replace with no logic change.
- **What if a fix fails?** `status` is derived, not set — if the root cause param
  is still bad, the router stays DOWN and the flow escalates to a technician.
