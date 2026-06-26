"""
migrate_roles.py  —  One-time Neo4j migration for the role layer.

Run ONCE against your Neo4j (same host as dump_graph.py). It is idempotent —
safe to re-run.

It does two things:
  1. Stamps a canonical `role` property on the HAS_PARAM relationship for every
     parameter the engine cares about, so resolution becomes deterministic
     (MATCH ()-[:HAS_PARAM {role:'vlan_id'}]->() instead of fuzzy CONTAINS).
  2. Adds the two standard TR-098 paths that were missing from the scraped
     sheets — DNSServers and LastConnectionError — to every router, so the
     DNS and PPP-auth faults resolve to a real path like every other fix.

After this runs (and the neo4j-agent /tools/resolve_path endpoint is deployed),
the live graph drives resolution; before/without it, the cached PATHS map in
mcp_engine is used. Either way the project works.

Usage:
    pip install neo4j
    python migrate_roles.py
"""

import os
from neo4j import GraphDatabase

import mcp_engine as E   # reuse the same role->path map as the source of truth

NEO4J_URI      = os.getenv("NEO4J_URI",      "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER",     "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

# Metadata for the parameters we ADD (standard TR-098 paths missing from scrape).
ADDED_META = {
    "dns_servers":           {"label": "DNS Servers",            "category": "ppp_info", "editable": "yes"},
    "last_connection_error": {"label": "Last Connection Error",  "category": "ppp_info", "editable": "non"},
}


def migrate(tx, model: str, role: str, path: str):
    meta = ADDED_META.get(role)
    if meta:
        # Add (idempotently) the missing standard parameter + stamp the role.
        tx.run("""
            MATCH (r:Router)
            WHERE r.id = $model OR r.product_class = $model OR r.sheet_name = $model
            MERGE (p:Parameter {path: $path})
              ON CREATE SET p.label = $label, p.category = $category, p.editable = $editable
            MERGE (r)-[rel:HAS_PARAM]->(p)
              ON CREATE SET rel.band = 'all', rel.notes = 'added by migrate_roles (standard TR-098)'
            SET rel.role = $role
        """, model=model, path=path, role=role,
             label=meta["label"], category=meta["category"], editable=meta["editable"])
    else:
        # Stamp the role on the existing parameter edge (don't touch its props).
        tx.run("""
            MATCH (r:Router)-[rel:HAS_PARAM]->(p:Parameter {path: $path})
            WHERE r.id = $model OR r.product_class = $model OR r.sheet_name = $model
            SET rel.role = $role
        """, model=model, path=path, role=role)


def main():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    driver.verify_connectivity()
    print(f"Connected to {NEO4J_URI} / db={NEO4J_DATABASE}")

    stamped = 0
    with driver.session(database=NEO4J_DATABASE) as s:
        for model, roles in E.PATHS.items():
            for role, path in roles.items():
                s.execute_write(migrate, model, role, path)
                stamped += 1
            print(f"  {model:22s} {len(roles)} roles")

        # Report
        counts = s.run("""
            MATCH ()-[rel:HAS_PARAM]->()
            WHERE rel.role IS NOT NULL
            RETURN rel.role AS role, count(*) AS n ORDER BY n DESC
        """).data()

    driver.close()
    print(f"\nStamped {stamped} (model, role) edges. Roles now in graph:")
    for c in counts:
        print(f"  {c['n']:3d}  {c['role']}")
    print("\nDone. The neo4j-agent /tools/resolve_path endpoint can now serve these.")


if __name__ == "__main__":
    main()
