#!/usr/bin/env python3
"""Offline stub test: prove verify() actually rejects broken graphs.

No server, no network - a fake api object answers GETs from a dict. The point is narrow:
a healthy fixture must pass, and each of the three seeded defects must NOT pass.

Run: python3 compose/seed/test_verify_stub.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed_first_scene as s  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXPECTED = json.load(open(os.path.join(HERE, "expected-graph.json"), encoding="utf-8"))

SCHEMA_FQN = "lab_synthetic_db.lab_commerce.lab_sales"
ORDERS = f"{SCHEMA_FQN}.orders"
REFUNDS = f"{SCHEMA_FQN}.refunds"
NETREV = f"{SCHEMA_FQN}.net_revenue_daily"
PIPELINE = "lab_synthetic_pipelines.build_net_revenue_daily"
DASHBOARD = "lab_synthetic_dashboards.finance_exec_daily"


def healthy_world():
    """A server state that satisfies expected-graph.json completely."""
    cols = {
        ORDERS: ["order_id", "order_ts", "customer_id", "gross_amount"],
        REFUNDS: ["refund_id", "order_id", "refund_ts", "refund_amount"],
        NETREV: ["day", "gross_amount", "refund_amount", "net_revenue"],
    }
    entities = {}
    for spec in EXPECTED["entities"]:
        fqn, etype = spec["fqn"], spec["type"]
        body = {"id": "id-" + fqn, "name": fqn.split(".")[-1], "fullyQualifiedName": fqn}
        exp = spec.get("expect", {})
        if "serviceType" in exp:
            body["serviceType"] = exp["serviceType"]
        if fqn in cols:
            body["columns"] = [{"name": c} for c in cols[fqn]]
        if etype == "glossaryTerm":
            body["description"] = "순매출은 총매출에서 환불 금액을 차감한 값이다."
            body["synonyms"] = ["net_sales", "순매출"]
        if etype == "metric":
            body.update({
                "metricType": "DERIVED", "unitOfMeasurement": "DOLLARS", "granularity": "DAY",
                "metricExpression": {"language": "SQL", "code": s.NET_REVENUE_SQL},
                "owners": [{"id": "u1", "type": "user", "name": "admin"}],
                "assets": [{"id": "id-" + NETREV, "type": "table", "fullyQualifiedName": NETREV}],
            })
        entities[fqn] = body
    edges = {(e["from_fqn"], e["to_fqn"]) for e in EXPECTED["lineage_edges"]}
    return entities, edges


class FakeApi:
    def __init__(self, entities, edges):
        self.entities, self.edges = entities, edges

    def get_or_none(self, path):
        import urllib.parse
        if path.startswith("/v1/lineage/getLineageEdge/"):
            parts = path.split("/")
            ffqn = urllib.parse.unquote(parts[6])
            tfqn = urllib.parse.unquote(parts[9])
            return {"edge": "yes"} if (ffqn, tfqn) in self.edges else None
        base = path.split("?")[0]
        fqn = urllib.parse.unquote(base.split("/name/")[-1])
        return self.entities.get(fqn)


# Verbatim from a real getLineageEdge response on the running server (read-only, via the
# coordinator). The pre-annotation response had this shape: no description, no sqlQuery, empty
# columnsLineage, source already Manual. The stub uses THIS, rather than a shape invented to
# suit our parser.
SERVER_DEFAULT_DETAILS = {
    "columnsLineage": [],
    "source": "Manual",
    "createdAt": 1789174223767,
    "createdBy": "admin",
    "updatedAt": 1789174223767,
    "updatedBy": "admin",
}


class FreshServerApi:
    """An empty server that only knows an entity after it has been PUT.

    Just enough FQN assembly to mirror how the real server derives a child's FQN from its
    parent. Used to drive the REAL apply_full(), so the id-resolution ordering is tested
    rather than restated.
    """

    PLURAL_TO_TYPE = {
        "services/databaseServices": "databaseService", "databases": "database",
        "databaseSchemas": "databaseSchema", "tables": "table",
        "services/pipelineServices": "pipelineService", "pipelines": "pipeline",
        "services/dashboardServices": "dashboardService", "dashboards": "dashboard",
        "glossaries": "glossary", "glossaryTerms": "glossaryTerm", "metrics": "metric",
    }

    def __init__(self):
        self.entities = {}       # fqn -> body
        self.edges = set()       # (from_fqn, to_fqn)
        self.edge_details = {}   # (from_fqn, to_fqn) -> lineageDetails
        self.id_to_fqn = {}
        self.put_log = []

    @staticmethod
    def _fqn(kind, body):
        name = body["name"]
        parent = {"database": "service", "databaseSchema": "database", "table": "databaseSchema",
                  "pipeline": "service", "dashboard": "service", "glossaryTerm": "glossary"}
        key = parent.get(kind)
        return f"{body[key]}.{name}" if key else name

    def put(self, path, body):
        if path == "/v1/lineage":
            edge = body["edge"]
            f = self.id_to_fqn[edge["fromEntity"]["id"]]
            t = self.id_to_fqn[edge["toEntity"]["id"]]
            self.edges.add((f, t))
            if "lineageDetails" in edge:
                # mirrors the real PUT: details are replaced wholesale, not merged
                self.edge_details[(f, t)] = dict(SERVER_DEFAULT_DETAILS,
                                                 **edge["lineageDetails"])
            else:
                # a plain edge PUT still lands with server-side defaults present
                self.edge_details.setdefault((f, t), dict(SERVER_DEFAULT_DETAILS))
            self.put_log.append(("edge", f, t))
            return {}
        kind = self.PLURAL_TO_TYPE[path[len("/v1/"):]]
        fqn = self._fqn(kind, body)
        eid = "id-" + fqn
        stored = dict(body)
        stored.update({"id": eid, "fullyQualifiedName": fqn, "name": body["name"]})
        if kind == "table":
            stored["columns"] = [{"name": c["name"]} for c in body.get("columns", [])]
        if kind == "metric":
            stored["assets"] = [{"id": a["id"], "type": a["type"],
                                 "fullyQualifiedName": self.id_to_fqn[a["id"]]}
                                for a in body.get("assets", [])]
            stored["owners"] = [dict(o, name="admin") for o in body.get("owners", [])]
        self.entities[fqn] = stored
        self.id_to_fqn[eid] = fqn
        self.put_log.append((kind, fqn))
        return stored

    def get_or_none(self, path):
        import urllib.parse
        if path.startswith("/v1/lineage/getLineageEdge/"):
            parts = path.split("/")
            f = urllib.parse.unquote(parts[6])
            t = urllib.parse.unquote(parts[9])
            if (f, t) not in self.edges:
                return None
            # REAL shape, copied from an actual server response - not invented to match our code.
            # LineageRepository.java:1645-1651 -> responseMap.put("edge", lineageDetails)
            return {"edge": dict(self.edge_details[(f, t)])}
        base = path.split("?")[0]
        fqn = urllib.parse.unquote(base.split("/name/")[-1])
        return self.entities.get(fqn)


def fresh_server_case():
    """Seed an empty server via the production path; every expected edge must land."""
    api = FreshServerApi()
    owner = {"id": "u1", "type": "user"}
    added, _, unresolved = s.apply_full(api, EXPECTED, owner, log=lambda *_: None)
    want = {(e["from_fqn"], e["to_fqn"]) for e in EXPECTED["lineage_edges"]}
    missing = want - api.edges
    results = s.verify(api, EXPECTED)
    counts = {s.MATCH: 0, s.MISMATCH: 0, s.UNVERIFIED: 0}
    for r in results:
        counts[r["state"]] += 1
    code = 1 if counts[s.MISMATCH] else (2 if counts[s.UNVERIFIED] else 0)
    print(f"  {'empty server -> full seed':38s} edges={added}/{len(want)} unresolved={unresolved} "
          f"match={counts[s.MATCH]} exit={code}")
    return code, missing, unresolved


def run(label, entities, edges):
    results = s.verify(FakeApi(entities, edges), EXPECTED)
    counts = {s.MATCH: 0, s.MISMATCH: 0, s.UNVERIFIED: 0}
    for r in results:
        counts[r["state"]] += 1
    code = 1 if counts[s.MISMATCH] else (2 if counts[s.UNVERIFIED] else 0)
    print(f"  {label:38s} match={counts[s.MATCH]:3d} mismatch={counts[s.MISMATCH]:2d} "
          f"unverified={counts[s.UNVERIFIED]:2d} exit={code}")
    return code, results


def main():
    failures = []

    print("case: EMPTY SERVER full seed via the real apply_full (regression: metric edge)")
    code, missing, unresolved = fresh_server_case()
    if missing:
        failures.append(f"empty-server seed dropped edges: {sorted(missing)}")
    if unresolved:
        failures.append(f"empty-server seed left {unresolved} edge(s) id-unresolved")
    if code != 0:
        failures.append(f"empty-server seed should verify clean, got exit {code}")

    print("\ncase: --edge-context annotates existing edges, creates nothing")
    api = FreshServerApi()
    s.apply_full(api, EXPECTED, {"id": "u1", "type": "user"}, log=lambda *_: None)
    before_entities = dict(api.entities)
    before_edges = set(api.edges)
    ctx = json.load(open(os.path.join(HERE, "edge-context.json"), encoding="utf-8"))
    ids = s.collect_ids(api, EXPECTED)
    updated, absent = s.apply_edge_context(api, ctx, ids, log=lambda *_: None)
    edge_puts = [p for p in api.put_log if p[0] == "edge"]
    entity_puts_after = len(api.entities)
    print(f"  {'annotate 5 existing edges':38s} annotated={updated} absent={absent} "
          f"entities={entity_puts_after} edges={len(api.edges)}")
    if updated != 5 or absent:
        failures.append(f"edge-context should annotate 5 edges, got {updated}/{absent}")
    if api.entities != before_entities:
        failures.append("edge-context must not create or modify entities")
    if api.edges != before_edges:
        failures.append("edge-context must not add or remove edges")
    if len(edge_puts) != 10:  # 5 from the seed + 5 annotations, no extras
        failures.append(f"unexpected edge PUT count: {len(edge_puts)}")

    print("\ncase: --edge-context read-back verifies the stored details (separate from graph)")
    det = s.verify_edge_details(api, ctx)
    dcounts = {s.MATCH: 0, s.MISMATCH: 0, s.UNVERIFIED: 0}
    for r in det:
        dcounts[r["state"]] += 1
    print(f"  {'annotations read back':38s} match={dcounts[s.MATCH]} "
          f"mismatch={dcounts[s.MISMATCH]} unverified={dcounts[s.UNVERIFIED]}")
    if dcounts[s.MISMATCH] or dcounts[s.UNVERIFIED]:
        failures.append("annotations should read back clean on the fake server")
    if dcounts[s.MATCH] != 11:  # 5 source + 5 description + 1 sqlQuery
        failures.append(f"expected 11 detail checks, got {dcounts[s.MATCH]}")

    print("\ncase: parser reads the REAL response shape (regression: nested-key assumption)")
    real = {"edge": dict(SERVER_DEFAULT_DETAILS)}
    real_with_desc = {"edge": dict(SERVER_DEFAULT_DETAILS,
                                   description="already written; preserve me")}
    got_empty = s._edge_details(real)
    got_desc = s._edge_details(real_with_desc)
    print(f"  {'real empty details':38s} parsed={got_empty is not None} "
          f"source={(got_empty or {}).get('source')}")
    print(f"  {'real details carrying a description':38s} parsed={got_desc is not None} "
          f"desc={bool((got_desc or {}).get('description'))}")
    if got_empty is None or got_empty.get("source") != "Manual":
        failures.append("parser must read the real {'edge': <details>} shape")
    if not (got_desc or {}).get("description"):
        failures.append("parser must see a description that is actually present")

    print("\ncase: edge already carries a description -> must STOP without writing")
    api3 = FreshServerApi()
    s.apply_full(api3, EXPECTED, {"id": "u1", "type": "user"}, log=lambda *_: None)
    first = ctx["edges"][0]
    api3.edge_details[(first["from_fqn"], first["to_fqn"])] = dict(
        SERVER_DEFAULT_DETAILS, description="already written; preserve me")
    ids3 = s.collect_ids(api3, EXPECTED)
    base3 = s.edge_baseline(api3, ctx)
    verdicts = [v for *_x, v in base3]
    puts_before = len(api3.put_log)
    stopped = False
    try:
        s.apply_edge_context(api3, ctx, ids3, log=lambda *_: None, baseline=base3)
    except RuntimeError:
        stopped = True
    print(f"  {'pre-existing description on 1 edge':38s} has_content={verdicts.count('has-content')} "
          f"stopped={stopped} writes={len(api3.put_log) - puts_before}")
    if verdicts.count("has-content") != 1:
        failures.append(f"baseline must flag exactly 1 has-content, got {verdicts}")
    if not stopped:
        failures.append("must refuse to annotate an edge that already carries content")
    if len(api3.put_log) != puts_before:
        failures.append("stop condition must write nothing at all")

    print("\ncase: unreadable response shape -> must STOP without writing")

    class BadShapeApi(FreshServerApi):
        def get_or_none(self, path):
            if path.startswith("/v1/lineage/getLineageEdge/"):
                got = super().get_or_none(path)
                return {"unexpected": "shape"} if got else None
            return super().get_or_none(path)

    api4 = BadShapeApi()
    s.apply_full(api4, EXPECTED, {"id": "u1", "type": "user"}, log=lambda *_: None)
    ids4 = s.collect_ids(api4, EXPECTED)
    base4 = s.edge_baseline(api4, ctx)
    verd4 = [v for *_x, v in base4]
    puts_before4 = len(api4.put_log)
    stopped4 = False
    try:
        s.apply_edge_context(api4, ctx, ids4, log=lambda *_: None, baseline=base4)
    except RuntimeError:
        stopped4 = True
    print(f"  {'unrecognised shape on all edges':38s} unknown={verd4.count('unknown-shape')} "
          f"stopped={stopped4} writes={len(api4.put_log) - puts_before4}")
    if verd4.count("unknown-shape") != 5:
        failures.append(f"unreadable shape must be unknown-shape, got {verd4}")
    if not stopped4 or len(api4.put_log) != puts_before4:
        failures.append("unreadable shape must write nothing at all")

    print("\ncase: --edge-context on a graph missing an edge leaves it alone")
    api2 = FreshServerApi()
    s.apply_full(api2, EXPECTED, {"id": "u1", "type": "user"}, log=lambda *_: None)
    api2.edges.discard((NETREV, "lab_net_revenue"))
    ids2 = s.collect_ids(api2, EXPECTED)
    upd2, abs2 = s.apply_edge_context(api2, ctx, ids2, log=lambda *_: None)
    print(f"  {'one edge absent':38s} annotated={upd2} absent={abs2}")
    if abs2 != 1 or upd2 != 4:
        failures.append(f"absent edge must be reported, not created: {upd2}/{abs2}")
    if (NETREV, "lab_net_revenue") in api2.edges:
        failures.append("edge-context must not resurrect a missing edge")

    print("\ncase: healthy fixture (must pass, exit 0)")
    code, _ = run("healthy", *healthy_world())
    if code != 0:
        failures.append(f"healthy world should exit 0, got {code}")

    print("\ncase: one lineage edge missing (must NOT pass)")
    ents, edges = healthy_world()
    edges.discard((ORDERS, PIPELINE))          # sibling edge that refunds' graph would hide
    code, res = run("missing orders->pipeline", ents, edges)
    if code == 0:
        failures.append("missing edge must not exit 0")
    src = [r for r in res if r["check"].startswith("SCENE source_upstream")]
    if not src or src[0]["state"] == s.MATCH:
        failures.append("source_upstream must not MATCH when orders->pipeline is gone")

    print("\ncase: metric<-table lineage edge missing (the observed UI gap; must NOT pass)")
    ents, edges = healthy_world()
    edges.discard((NETREV, "lab_net_revenue"))
    code, res = run("missing net_revenue_daily->metric", ents, edges)
    if code == 0:
        failures.append("missing metric lineage edge must not exit 0")
    m2s = [r for r in res if r["check"].startswith("SCENE metric_to_source")]
    if not m2s or m2s[0]["state"] == s.MATCH:
        failures.append("metric_to_source must not MATCH without the table->metric edge")

    print("\ncase: wrong owner (must be MISMATCH, exit 1)")
    ents, edges = healthy_world()
    ents["lab_net_revenue"]["owners"] = [{"id": "u2", "type": "user", "name": "someone_else"}]
    code, res = run("owner=someone_else", ents, edges)
    if code != 1:
        failures.append(f"wrong owner must exit 1, got {code}")

    print("\ncase: metric->asset link missing (must NOT pass)")
    ents, edges = healthy_world()
    ents["lab_net_revenue"]["assets"] = []
    code, _ = run("assets=[]", ents, edges)
    if code == 0:
        failures.append("missing metric->asset link must not exit 0")

    print("\ncase: service carries a connection block (must be MISMATCH)")
    ents, edges = healthy_world()
    ents["lab_synthetic_db"]["connection"] = {"config": {"hostPort": "somewhere:5432"}}
    code, _ = run("connection present", ents, edges)
    if code != 1:
        failures.append(f"unexpected connection must exit 1, got {code}")

    print("\ncase: SQL regressed to the undefined o.day column (must be MISMATCH)")
    ents, edges = healthy_world()
    ents["lab_net_revenue"]["metricExpression"]["code"] = (
        "SELECT o.day, SUM(o.gross_amount) FROM orders o "
        "LEFT JOIN refunds r ON r.order_id=o.order_id GROUP BY o.day")
    code, _ = run("SQL uses o.day", ents, edges)
    if code != 1:
        failures.append(f"undefined-field SQL must exit 1, got {code}")

    print()
    if failures:
        for f in failures:
            print("STUB TEST FAILED:", f)
        return 1
    print("all stub cases behaved correctly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
