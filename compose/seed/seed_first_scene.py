#!/usr/bin/env python3
"""Seed the first scene (순매출 정의·소유자·원천 → 변경 영향) and verify it by reading back.

Development evidence (2026-09-12): a local OpenMetadata 2.0.1 instance was seeded
and read back with 41 graph checks matching. No server state is bundled here.
Defaults to a dry run: it prints the plan without requests. --verify-only logs in and reads;
--apply writes the reviewed plan.

Schema facts below were read from the pinned revision bf621b166ec12e8c99fcb1c1443442723386fa41,
not guessed. See the public root README for the scope of this exported fixture.

What this file does and does NOT guarantee - read the scope, not the vibe:
  * guard() does NOT gate every write. It is called on entity names in build_plan()/build_metric()
    only. The --edges-only path and the expected-driven edge writes do not call it; their targets
    come from expected-graph.json and the safety there rests on that file having been reviewed.
  * PUT is createOrUpdate, so it is NOT overwrite protection. A plain --apply can update an
    existing lab_ entity. "Re-running changes nothing" holds for --edges-only (it touches no
    entity and only adds missing edges), not for --apply in general.
  * Real scope: the reviewed exact lab_ fixture, new experiment targets, and the operator-side
    request limits. Not a structural guarantee against arbitrary non-lab targets.
Things that do hold as written:
  * Only POST (one login), PUT and GET reach _request. There is no DELETE path in this file.
  * The access token is never printed, logged, or written to disk. Only a redacted marker is shown.
    Auth-endpoint error bodies are withheld rather than echoed.
  * Reads the password from an env var; it is never taken as a command-line argument.
  * Missing relationships are reported UNVERIFIED, never as "no impact". Exit 2 means unverified
    remains and is NOT success.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

LAB_PREFIX = "lab_"
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_EXPECTED = os.path.join(HERE, "expected-graph.json")

MATCH, MISMATCH, UNVERIFIED = "MATCH", "MISMATCH", "UNVERIFIED"


class Api:
    def __init__(self, base_url, timeout=30):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._token = None

    # -- token is held here and deliberately never rendered ------------------
    def login(self, email, password):
        body = {"email": email, "password": base64.b64encode(password.encode()).decode()}
        # UserResource.java:1703 - email plain-text, password base64, returns JwtResponse
        resp = self._request("POST", "/v1/users/login", body, auth=False)
        token = resp.get("accessToken")
        if not token:
            raise RuntimeError("login returned no accessToken (keys: %s)" % sorted(resp))
        self._token = token
        return "<token acquired, %d chars, not shown>" % len(token)

    def _request(self, method, path, body=None, auth=True):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if auth:
            if not self._token:
                raise RuntimeError("not logged in")
            req.add_header("Authorization", "Bearer " + self._token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:400]
            raise RuntimeError("%s %s -> HTTP %s: %s" % (method, path, e.code, detail)) from None

    def put(self, path, body):
        return self._request("PUT", path, body)

    def get(self, path):
        return self._request("GET", path)

    def get_or_none(self, path):
        try:
            return self.get(path)
        except RuntimeError as e:
            if "HTTP 404" in str(e):
                return None
            raise


def guard(name):
    """Refuse to touch anything outside the lab namespace."""
    root = name.split(".")[0]
    if not root.startswith(LAB_PREFIX):
        raise RuntimeError(
            "refusing to write outside the lab namespace: %r (root %r must start with %r)"
            % (name, root, LAB_PREFIX)
        )
    return name


# --------------------------------------------------------------------------
# Entity payloads. Field names and enums verified against the pinned schemas.
# All create schemas are additionalProperties:false, so no stray keys here.
# --------------------------------------------------------------------------
DB_SERVICE = "lab_synthetic_db"
DATABASE = "lab_commerce"
SCHEMA = "lab_sales"
SCHEMA_FQN = f"{DB_SERVICE}.{DATABASE}.{SCHEMA}"
PIPE_SERVICE = "lab_synthetic_pipelines"
PIPELINE = "build_net_revenue_daily"
PIPELINE_FQN = f"{PIPE_SERVICE}.{PIPELINE}"
DASH_SERVICE = "lab_synthetic_dashboards"
DASHBOARD = "finance_exec_daily"
DASHBOARD_FQN = f"{DASH_SERVICE}.{DASHBOARD}"
GLOSSARY = "lab_business_metrics"
TERM = "net_revenue"
METRIC = "lab_net_revenue"

ORDERS_FQN = f"{SCHEMA_FQN}.orders"
REFUNDS_FQN = f"{SCHEMA_FQN}.refunds"
NET_REV_FQN = f"{SCHEMA_FQN}.net_revenue_daily"

# Two things this expression has to get right, both written down in
# expected-graph.json -> net_revenue_semantics BEFORE this SQL was written:
#
#  1. orders has no `day` column. The only defined date field is `order_ts`.
#  2. Joining refunds directly fans the order rows out. With one 100 order and two
#     refunds of 20 and 10, a direct LEFT JOIN doubles the order row and gives
#     gross=200 / net=170. Pre-aggregating refunds per order keeps it 1:1 -> net=70.
#
# Refund attribution is a CHOICE of this synthetic case: refunds land on the ORIGINAL
# ORDER's day, not on the refund's own day. That is what makes a refund change rewrite
# a past day's number, which is the whole point of the scene. It is not a general
# accounting rule and must not be read as one.
NET_REVENUE_SQL = (
    "WITH refunds_per_order AS ("
    "SELECT order_id, SUM(refund_amount) AS refund_amount FROM refunds GROUP BY order_id"
    ") "
    "SELECT CAST(o.order_ts AS DATE) AS day, "
    "SUM(o.gross_amount) AS gross_amount, "
    "COALESCE(SUM(r.refund_amount), 0) AS refund_amount, "
    "SUM(o.gross_amount) - COALESCE(SUM(r.refund_amount), 0) AS net_revenue "
    "FROM orders o "
    "LEFT JOIN refunds_per_order r ON r.order_id = o.order_id "
    "GROUP BY CAST(o.order_ts AS DATE)"
)

TERM_DESCRIPTION = (
    "순매출(net revenue)은 총매출에서 **환불 금액을 차감한** 값이다.\n\n"
    "- 집계 단위: 일(day). `day`는 `orders.order_ts`에서 파생한다 (orders에 `day` 컬럼은 없다).\n"
    "- 환불은 **order_id 단위로 먼저 합산한 뒤** 주문과 1:1로 결합한다.\n"
    "  한 주문에 환불이 여러 건이면 그냥 조인할 경우 주문 금액이 중복 계산된다.\n"
    "  예: 주문 100 하나, 환불 20과 10 → 순매출은 **70**이다 (단순 조인이면 170이 되어 틀린다).\n"
    "- 환불액은 **원주문의 날짜**에 귀속시킨다. 그래서 환불 데이터가 바뀌면 과거 일자의 순매출도 바뀐다.\n\n"
    "※ 환불의 날짜 귀속은 **이 합성 사례에서 고른 규칙**이며 실제 회계의 환불 인식 규칙이 아니다.\n"
    "※ 이 용어는 실험용 합성 fixture다. 회사의 실제 회계 정의가 아니다."
)


def build_plan():
    """Entities that must exist before the metric can reference them. Ordered."""
    plan = [
        ("databaseService", f"/v1/services/databaseServices",
         {"name": guard(DB_SERVICE), "serviceType": "CustomDatabase",
          "description": "실험용 합성 원천 (연결 설정 없음)"}),

        ("database", "/v1/databases",
         {"name": DATABASE, "service": guard(DB_SERVICE),
          "description": "실험용 합성 커머스 DB"}),

        ("databaseSchema", "/v1/databaseSchemas",
         {"name": SCHEMA, "database": guard(f"{DB_SERVICE}.{DATABASE}"),
          "description": "실험용 합성 매출 스키마"}),

        ("table:orders", "/v1/tables",
         {"name": "orders", "databaseSchema": guard(SCHEMA_FQN),
          "description": "주문 원장 (합성). 총매출의 원천.",
          "columns": [
              {"name": "order_id", "dataType": "BIGINT"},
              {"name": "order_ts", "dataType": "TIMESTAMP"},
              {"name": "customer_id", "dataType": "BIGINT"},
              {"name": "gross_amount", "dataType": "DECIMAL"},
          ]}),

        ("table:refunds", "/v1/tables",
         {"name": "refunds", "databaseSchema": guard(SCHEMA_FQN),
          "description": "환불 원장 (합성). 순매출 차감의 원천이며 변경 영향의 출발점.",
          "columns": [
              {"name": "refund_id", "dataType": "BIGINT"},
              {"name": "order_id", "dataType": "BIGINT"},
              {"name": "refund_ts", "dataType": "TIMESTAMP"},
              {"name": "refund_amount", "dataType": "DECIMAL"},
          ]}),

        ("table:net_revenue_daily", "/v1/tables",
         {"name": "net_revenue_daily", "databaseSchema": guard(SCHEMA_FQN),
          "description": "일별 순매출 (합성). orders와 refunds에서 파생된다.",
          "columns": [
              {"name": "day", "dataType": "DATE"},
              {"name": "gross_amount", "dataType": "DECIMAL"},
              {"name": "refund_amount", "dataType": "DECIMAL"},
              {"name": "net_revenue", "dataType": "DECIMAL"},
          ]}),

        ("pipelineService", "/v1/services/pipelineServices",
         {"name": guard(PIPE_SERVICE), "serviceType": "CustomPipeline",
          "description": "실험용 합성 파이프라인 서비스 (연결 설정 없음)"}),

        ("pipeline", "/v1/pipelines",
         {"name": PIPELINE, "service": guard(PIPE_SERVICE),
          "description": "orders + refunds 를 읽어 net_revenue_daily 를 만든다 (합성)."}),

        ("dashboardService", "/v1/services/dashboardServices",
         {"name": guard(DASH_SERVICE), "serviceType": "CustomDashboard",
          "description": "실험용 합성 대시보드 서비스 (연결 설정 없음)"}),

        ("dashboard", "/v1/dashboards",
         {"name": DASHBOARD, "service": guard(DASH_SERVICE),
          "description": "경영 일간 보고서 (합성). 순매출 변경의 영향을 받는 보고서."}),

        ("glossary", "/v1/glossaries",
         {"name": guard(GLOSSARY), "description": "실험용 합성 비즈니스 지표 용어집"}),

        ("glossaryTerm", "/v1/glossaryTerms",
         {"glossary": guard(GLOSSARY), "name": TERM, "displayName": "순매출",
          "description": TERM_DESCRIPTION,
          "synonyms": ["net_sales", "순매출"]}),
    ]

    return plan


def build_metric(owner_ref, asset_ref):
    """The metric is written last: it references the owner and the asset table by id.

    assets becomes an APPLIED_TO edge metric->asset in MetricRepository.storeRelationships
    (MetricRepository.java:219-222), so it is set in this same PUT - there is no separate
    asset-linking endpoint to call.
    """
    metric = {
        "name": guard(METRIC),
        "displayName": "순매출 (Net Revenue)",
        "description": TERM_DESCRIPTION,
        "metricType": "DERIVED",
        "unitOfMeasurement": "DOLLARS",
        "granularity": "DAY",
        "metricExpression": {"language": "SQL", "code": NET_REVENUE_SQL},
    }
    if owner_ref:
        metric["owners"] = [owner_ref]
    if asset_ref:
        metric["assets"] = [asset_ref]
    return ("metric", "/v1/metrics", metric)


# NOTE: there is deliberately no second copy of the edge list here. The edges to write
# and the edges to verify both come from expected-graph.json, so the check cannot silently
# agree with the writer by sharing a constant.


def resolve_owner(api, owner_name):
    """Resolve an owner principal. Returns an entityReference or None.

    None means UNVERIFIED downstream - never 'there is no owner'.
    """
    for path, etype in ((f"/v1/users/name/{owner_name}", "user"),
                        (f"/v1/teams/name/{owner_name}", "team")):
        got = api.get_or_none(path)
        if got and got.get("id"):
            return {"id": got["id"], "type": etype}
    return None


def write_edges(api, expected, ids, skip_existing, log=print):
    """PUT the expected lineage edges. Returns (added, skipped, unresolved)."""
    added = skipped = unresolved = 0
    for e in expected["lineage_edges"]:
        if skip_existing:
            fq = urllib.parse.quote(e["from_fqn"], safe="")
            tq = urllib.parse.quote(e["to_fqn"], safe="")
            if api.get_or_none(f"/v1/lineage/getLineageEdge/{e['from_type']}/name/{fq}/"
                               f"{e['to_type']}/name/{tq}"):
                skipped += 1
                continue
        fid, _ = ids.get(e["from_fqn"], (None, None))
        tid, _ = ids.get(e["to_fqn"], (None, None))
        if not fid or not tid:
            log(f"! skip lineage {e['from_fqn']} -> {e['to_fqn']}: id unresolved (UNVERIFIED)")
            unresolved += 1
            continue
        api.put("/v1/lineage",
                {"edge": {"fromEntity": {"id": fid, "type": e["from_type"]},
                          "toEntity": {"id": tid, "type": e["to_type"]}}})
        added += 1
        log(f"  put lineage {e['from_fqn']} -> {e['to_fqn']}")
    return added, skipped, unresolved


# PUT /v1/lineage carries the whole lineageDetails object, so annotating REPLACES whatever
# details an edge already had. This file does not implement a merge. Instead it refuses to run
# when an edge already carries content, so nothing is silently clobbered. These are the fields
# that count as content worth protecting (server-managed timestamps/actors are not).
EDGE_CONTENT_FIELDS = ("description", "sqlQuery", "columnsLineage", "pipeline",
                       "assetEdges", "tempLineageTables")


def _edge_details(resp):
    """Return the LineageDetails object from a getLineageEdge response.

    Observed and source-confirmed shape:
        {"edge": {"columnsLineage": [], "source": "Manual",
                  "createdAt": ..., "createdBy": "admin", "updatedAt": ..., "updatedBy": "admin"}}
    LineageRepository.java:1645-1651 does responseMap.put("edge", lineageDetails), so resp["edge"]
    IS the details object. There is no nested "lineageDetails" key - an earlier version of this
    function looked for one, found nothing, and wrongly reported every edge as having no details.

    Returns None only when the response is not that shape. Callers must treat None as
    unknown-shape and stop; it is never evidence that the edge is empty.
    """
    if isinstance(resp, dict) and isinstance(resp.get("edge"), dict):
        return resp["edge"]
    return None


def edge_baseline(api, context):
    """Read each target edge BEFORE writing. Returns rows of (edge, present, details, verdict).

    verdict: 'clean' (safe to annotate) | 'has-content' (would clobber, must stop)
             | 'absent' (edge missing) | 'unknown-shape' (cannot tell, must stop)
    """
    rows = []
    for e in context["edges"]:
        fq = urllib.parse.quote(e["from_fqn"], safe="")
        tq = urllib.parse.quote(e["to_fqn"], safe="")
        resp = api.get_or_none(f"/v1/lineage/getLineageEdge/{e['from_type']}/name/{fq}/"
                               f"{e['to_type']}/name/{tq}")
        if not resp:
            rows.append((e, False, None, "absent"))
            continue
        details = _edge_details(resp)
        if details is None:
            # Unreadable shape is NOT evidence of an empty edge. Stop rather than guess.
            rows.append((e, True, None, "unknown-shape"))
            continue
        occupied = [k for k in EDGE_CONTENT_FIELDS if details.get(k)]
        rows.append((e, True, details, "has-content" if occupied else "clean"))
    return rows


def apply_edge_context(api, context, ids, log=print, baseline=None):
    """Attach lineageDetails (description / sqlQuery / source) to edges that ALREADY exist.

    Creates no entity and no edge. Absent edges are reported, not created.

    Operating condition: callers pass the pre-apply `baseline`. If any target edge already
    carries content, or its response shape cannot be read, this raises instead of writing -
    because the PUT replaces lineageDetails wholesale and would drop that content.

    Renders in EdgeInfoDrawer.component.tsx:223~253 - description, `label.sql-uppercase-query`,
    and `label.lineage-source`.
    """
    if baseline is not None:
        blocked = [(e, v) for e, _p, _d, v in baseline if v in ("has-content", "unknown-shape")]
        if blocked:
            for e, v in blocked:
                log(f"! {v}: {e['from_fqn']} -> {e['to_fqn']}")
            raise RuntimeError(
                "refusing to annotate: %d edge(s) differ from the expected empty-details "
                "baseline. PUT replaces lineageDetails wholesale, so existing content would be "
                "lost. Inspect those edges and decide explicitly." % len(blocked))

    updated = absent = 0
    for e in context["edges"]:
        fq = urllib.parse.quote(e["from_fqn"], safe="")
        tq = urllib.parse.quote(e["to_fqn"], safe="")
        if not api.get_or_none(f"/v1/lineage/getLineageEdge/{e['from_type']}/name/{fq}/"
                               f"{e['to_type']}/name/{tq}"):
            log(f"! edge absent, not annotating: {e['from_fqn']} -> {e['to_fqn']}")
            absent += 1
            continue
        fid, _ = ids.get(e["from_fqn"], (None, None))
        tid, _ = ids.get(e["to_fqn"], (None, None))
        if not fid or not tid:
            log(f"! id unresolved, skipping: {e['from_fqn']} -> {e['to_fqn']}")
            absent += 1
            continue
        api.put("/v1/lineage",
                {"edge": {"fromEntity": {"id": fid, "type": e["from_type"]},
                          "toEntity": {"id": tid, "type": e["to_type"]},
                          "lineageDetails": e["details"]}})
        updated += 1
        log(f"  annotated {e['from_fqn']} -> {e['to_fqn']}")
    return updated, absent


def verify_edge_details(api, context):
    """Read the annotations back. SEPARATE from the 41-MATCH graph check.

    The graph check only asks whether edges exist. It says nothing about whether
    description/sqlQuery/source were stored. This does, and only this.
    """
    results = []
    for e, present, details, _verdict in edge_baseline(api, context):
        label = f"{e['from_fqn']} -> {e['to_fqn']}"
        if not present:
            results.append({"check": f"edge-details {label}", "state": UNVERIFIED,
                            "detail": "edge absent"})
            continue
        if details is None:
            results.append({"check": f"edge-details {label}", "state": UNVERIFIED,
                            "detail": "no lineageDetails readable in the response"})
            continue
        want = e["details"]
        for key in ("source", "description", "sqlQuery"):
            if key not in want:
                continue
            got = details.get(key)
            if got is None:
                state, note = UNVERIFIED, "not returned"
            elif got == want[key]:
                state, note = MATCH, ""
            else:
                state, note = MISMATCH, f"stored value differs ({len(str(got))} chars)"
            results.append({"check": f"edge-details {label} .{key}", "state": state,
                            "detail": note})
    return results


def apply_full(api, expected, owner_ref, log=print):
    """Full seed: entities, then the metric, then the edges.

    Ordering matters and has bitten us once. The metric must be written after the tables
    because it references net_revenue_daily by id, and the edge list must be written after
    the metric because one expected edge ENDS at the metric. Resolving ids only once - before
    the metric exists - silently dropped that edge on an empty server. So ids are refreshed
    after the metric PUT. test_verify_stub.py drives this same function against a fresh fake
    server to keep that property honest.
    """
    for label, path, payload in build_plan():
        api.put(path, payload)
        log(f"  put {label}")

    ids = collect_ids(api, expected)
    asset_ref = None
    aid, atype = ids.get(NET_REV_FQN, (None, None))
    if aid:
        asset_ref = {"id": aid, "type": atype}
    else:
        log("! net_revenue_daily id unresolved; metric.assets omitted (UNVERIFIED)")
    label, path, payload = build_metric(owner_ref, asset_ref)
    api.put(path, payload)
    log(f"  put {label}")

    # The metric did not exist when ids was built. Refresh, or its edge is skipped.
    ids = collect_ids(api, expected)
    return write_edges(api, expected, ids, skip_existing=False, log=log)


def collect_ids(api, expected):
    """FQN -> (id, type) for every endpoint named by the expected edges, plus the metric asset."""
    wanted = {(NET_REV_FQN, "table")}
    for e in expected["lineage_edges"]:
        wanted.add((e["from_fqn"], e["from_type"]))
        wanted.add((e["to_fqn"], e["to_type"]))
    wanted = [(t, f) for f, t in wanted]
    plural = {"table": "tables", "pipeline": "pipelines", "dashboard": "dashboards",
              "metric": "metrics"}
    out = {}
    for etype, fqn in wanted:
        got = api.get_or_none(f"/v1/{plural[etype]}/name/{urllib.parse.quote(fqn, safe='')}")
        out[fqn] = (got["id"], etype) if got and got.get("id") else (None, etype)
    return out


# --------------------------------------------------------------------------
# Verification: read back from the SERVER and compare to expected-graph.json
# --------------------------------------------------------------------------
def verify(api, expected):
    results = []

    def record(check, state, detail=""):
        results.append({"check": check, "state": state, "detail": detail})

    plural = {"databaseService": "services/databaseServices", "database": "databases",
              "databaseSchema": "databaseSchemas", "table": "tables",
              "pipelineService": "services/pipelineServices", "pipeline": "pipelines",
              "dashboardService": "services/dashboardServices", "dashboard": "dashboards",
              "glossary": "glossaries", "glossaryTerm": "glossaryTerms", "metric": "metrics"}

    entity_cache = {}
    for spec in expected["entities"]:
        etype, fqn = spec["type"], spec["fqn"]
        q = urllib.parse.quote(fqn, safe="")
        extra = "?fields=owners,assets" if etype == "metric" else ""
        got = api.get_or_none(f"/v1/{plural[etype]}/name/{q}{extra}")
        entity_cache[fqn] = got
        if not got:
            record(f"{etype} {fqn} exists", UNVERIFIED, "not found on server")
            continue
        record(f"{etype} {fqn} exists", MATCH)

        exp = spec.get("expect", {})
        if "serviceType" in exp:
            ok = got.get("serviceType") == exp["serviceType"]
            record(f"{fqn}.serviceType == {exp['serviceType']}", MATCH if ok else MISMATCH,
                   "" if ok else f"got {got.get('serviceType')!r}")
        if "columns" in exp:
            names = [c.get("name") for c in got.get("columns", [])]
            ok = names == exp["columns"]
            record(f"{fqn}.columns", MATCH if ok else MISMATCH, "" if ok else f"got {names}")
        if "description_contains" in exp:
            ok = exp["description_contains"] in (got.get("description") or "")
            record(f"{fqn}.description contains {exp['description_contains']!r}",
                   MATCH if ok else MISMATCH)
        if "synonyms" in exp:
            ok = sorted(got.get("synonyms") or []) == sorted(exp["synonyms"])
            record(f"{fqn}.synonyms", MATCH if ok else MISMATCH,
                   "" if ok else f"got {got.get('synonyms')}")
        for key in ("metricType", "unitOfMeasurement", "granularity"):
            if key in exp:
                ok = got.get(key) == exp[key]
                record(f"{fqn}.{key} == {exp[key]}", MATCH if ok else MISMATCH,
                       "" if ok else f"got {got.get(key)!r}")
        if "metricExpression_language" in exp:
            lang = (got.get("metricExpression") or {}).get("language")
            ok = lang == exp["metricExpression_language"]
            record(f"{fqn}.metricExpression.language", MATCH if ok else MISMATCH,
                   "" if ok else f"got {lang!r}")
        if "metricExpression_contains" in exp:
            code = (got.get("metricExpression") or {}).get("code") or ""
            ok = exp["metricExpression_contains"] in code
            record(f"{fqn}.metricExpression.code contains {exp['metricExpression_contains']!r}",
                   MATCH if ok else MISMATCH)
        if "metricExpression_must_not_contain" in exp:
            code = (got.get("metricExpression") or {}).get("code") or ""
            bad = [s for s in exp["metricExpression_must_not_contain"] if s in code]
            record(f"{fqn}.metricExpression avoids undefined fields",
                   MATCH if not bad else MISMATCH,
                   "" if not bad else f"references undefined field(s): {bad}")
        if "owners_include_name" in exp:
            owners = got.get("owners") or []
            names = [o.get("name") for o in owners]
            want = exp["owners_include_name"]
            if not owners:
                record(f"{fqn}.owners == {want!r}", UNVERIFIED,
                       "no owner principal resolved - UNVERIFIED, not '소유자 없음'")
            elif want in names:
                record(f"{fqn}.owners == {want!r}", MATCH)
            else:
                # an arbitrary different owner must NOT pass
                record(f"{fqn}.owners == {want!r}", MISMATCH, f"got {names}")
        if "connection_absent" in exp:
            conn = got.get("connection")
            record(f"{fqn} has no connection config",
                   MATCH if not conn else MISMATCH,
                   "" if not conn else "a connection block is present - this fixture must carry no credential")
        if "assets_contains_fqn" in exp:
            assets = got.get("assets") or []
            fqns = [a.get("fullyQualifiedName") for a in assets]
            want = exp["assets_contains_fqn"]
            if not assets:
                record(f"{fqn}.assets contains {want}", UNVERIFIED,
                       "no assets returned - cannot tell 'not written' from 'not surfaced'")
            else:
                record(f"{fqn}.assets contains {want}",
                       MATCH if want in fqns else MISMATCH, f"got {fqns}")

    # -- lineage: each expected edge checked INDEPENDENTLY -----------------
    # Read the expected edges from the JSON, not from this module's write list, and
    # check one edge at a time via getLineageEdgeByName (LineageResource.java:966).
    # A single node's graph is NOT sufficient: refunds' graph need not contain the
    # sibling edge orders->pipeline.
    edge_state = {}

    def check_edge(ftype, ffqn, ttype, tfqn):
        key = (ffqn, tfqn)
        if key in edge_state:
            return edge_state[key]
        fq, tq = urllib.parse.quote(ffqn, safe=""), urllib.parse.quote(tfqn, safe="")
        got = api.get_or_none(
            f"/v1/lineage/getLineageEdge/{ftype}/name/{fq}/{ttype}/name/{tq}")
        state = MATCH if got else UNVERIFIED
        edge_state[key] = state
        return state

    for e in expected["lineage_edges"]:
        st = check_edge(e["from_type"], e["from_fqn"], e["to_type"], e["to_fqn"])
        record(f"lineage {e['from_fqn']} -> {e['to_fqn']}", st,
               "" if st == MATCH else "edge not found (404) - UNVERIFIED, not '영향 없음'")

    # -- scene assertions, also driven from the JSON ----------------------
    entity_states = {r["check"]: r["state"] for r in results}
    for sa in expected.get("scene_assertions", []):
        parts = []
        for edge in sa.get("requires_edges", []):
            parts.append(check_edge(*edge))
        for fqn in sa.get("requires_entity_checks", []):
            parts += [st for chk, st in entity_states.items() if chk.startswith(fqn + ".")]
        if not parts:
            state = UNVERIFIED
        elif MISMATCH in parts:
            state = MISMATCH
        elif UNVERIFIED in parts:
            state = UNVERIFIED
        else:
            state = MATCH
        record(f"SCENE {sa['id']}: {sa['question']}", state,
               "" if state == MATCH else f"{parts.count(UNVERIFIED)} unverified, "
                                         f"{parts.count(MISMATCH)} mismatch among {len(parts)} parts")

    return results


def main():
    p = argparse.ArgumentParser(description="Seed and verify the first OpenMetadata scene.")
    p.add_argument("--base-url", default="http://127.0.0.1:8585/api")
    p.add_argument("--user", default="admin@open-metadata.org",
                   help="login email. Official basic-auth docs list admin@open-metadata.org; "
                        "confirm against this server before relying on it.")
    p.add_argument("--password-env", default="OM_PASSWORD",
                   help="env var holding the password. Never pass the password as an argument.")
    p.add_argument("--owner", default="admin",
                   help="user or team name to set as metric owner; unresolved -> UNVERIFIED")
    p.add_argument("--expected", default=DEFAULT_EXPECTED)
    p.add_argument("--apply", action="store_true", help="actually write. Default is dry-run.")
    p.add_argument("--verify-only", action="store_true", help="skip writes, only read back and compare")
    p.add_argument("--edge-context", action="store_true",
                   help="attach lineageDetails (description/sqlQuery/source) from "
                        "edge-context.json to edges that already exist. Creates no entity and "
                        "no edge. Opt-in: the default verification does not require this.")
    p.add_argument("--edges-only", action="store_true",
                   help="do not create or update any entity; only PUT lineage edges that are "
                        "currently missing, then verify. Use this to add a newly expected edge "
                        "to an already-seeded server without re-running the whole seed.")
    args = p.parse_args()

    expected = json.load(open(args.expected, encoding="utf-8"))

    if args.edge_context and not args.apply:
        ctx = json.load(open(os.path.join(HERE, "edge-context.json"), encoding="utf-8"))
        print("=== DRY RUN --edge-context (no login, no request sent). Add --apply to write. ===\n")
        print("would create NO entity and NO edge. would only attach lineageDetails to "
              "these existing edges:\n")
        for e in ctx["edges"]:
            d = e["details"]
            print(f"  {e['from_fqn']}\n    -> {e['to_fqn']}")
            print(f"       source  : {d['source']}")
            print(f"       desc    : {d['description'].splitlines()[0]}")
            if d.get("sqlQuery"):
                print(f"       sqlQuery: {d['sqlQuery'][:70]}...")
            print()
        return 0

    if args.edges_only and not args.apply:
        print("=== DRY RUN --edges-only (no login, no request sent). Add --apply to write. ===\n")
        print("would create/update NO entity. would PUT only the lineage edges below that are "
              "reported missing by getLineageEdge:\n")
        for e in expected["lineage_edges"]:
            print(f"  {e['from_type']:9s} {e['from_fqn']}\n"
                  f"    -> {e['to_type']:6s} {e['to_fqn']}")
        print("\nthen GET read-back and compare against expected-graph.json")
        return 0

    if not args.apply and not args.verify_only:
        print("=== DRY RUN (no login, no request will be sent). Pass --apply to write. ===\n")
        for label, path, payload in build_plan():
            print(f"PUT {path}\n  # {label}\n  {json.dumps(payload, ensure_ascii=False)[:220]}\n")
        label, path, payload = build_metric({"id": "<owner-id>", "type": "user"},
                                            {"id": "<net_revenue_daily-id>", "type": "table"})
        print(f"PUT {path}\n  # {label}\n  {json.dumps(payload, ensure_ascii=False)[:400]}\n")
        for e in expected["lineage_edges"]:
            print(f"PUT /v1/lineage\n  # {e['from_fqn']} -> {e['to_fqn']}")
        print(f"\nthen GET read-back and compare against {os.path.basename(args.expected)}")
        print("exit codes: 0=all match, 1=mismatch, 2=unverified remains (NOT success)")
        return 0

    password = os.environ.get(args.password_env)
    if not password:
        print(f"error: env var {args.password_env} is not set", file=sys.stderr)
        return 1

    api = Api(args.base_url)
    try:
        print(api.login(args.user, password))   # prints a redacted marker only
    except RuntimeError:
        # Never echo an auth endpoint's response body: it can carry credential material.
        print("error: login failed (response body withheld deliberately). "
              "Check the email, the password env var, and the server URL.", file=sys.stderr)
        return 1

    if args.edge_context:
        ctx = json.load(open(os.path.join(HERE, "edge-context.json"), encoding="utf-8"))
        ids = collect_ids(api, expected)

        print("=== pre-apply baseline of the 5 target edges ===")
        baseline = edge_baseline(api, ctx)
        for e, present, details, verdict in baseline:
            occupied = [k for k in EDGE_CONTENT_FIELDS if (details or {}).get(k)]
            print(f"  [{verdict:13s}] {e['from_fqn']} -> {e['to_fqn']}"
                  + (f"  occupied={occupied}" if occupied else ""))
        try:
            updated, absent = apply_edge_context(api, ctx, ids, baseline=baseline)
        except RuntimeError as exc:
            print(f"\nSTOPPED, nothing written: {exc}", file=sys.stderr)
            return 1
        print(f"  ({updated} annotated, {absent} skipped, 0 entities and 0 edges created)")

        print("\n=== read-back of the annotations (NOT the graph check) ===")
        code = report(verify_edge_details(api, ctx))
        print("\n=== graph read-back vs expected-graph.json (unchanged by this mode) ===")
        graph_code = report(verify(api, expected))
        return code or graph_code

    if args.edges_only:
        # Touch no entity. Add only edges that are actually missing, so re-running is a no-op.
        ids = collect_ids(api, expected)
        added, skipped, _ = write_edges(api, expected, ids, skip_existing=True)
        print(f"  ({added} added, {skipped} already present, 0 entities touched)")
        print("\n=== read-back vs expected-graph.json ===")
        return report(verify(api, expected))

    if not args.verify_only:
        owner_ref = resolve_owner(api, args.owner)
        if not owner_ref:
            print(f"! owner {args.owner!r} not resolved; metric.owners will be omitted "
                  f"and reported UNVERIFIED (not '소유자 없음')")
        apply_full(api, expected, owner_ref)

    print("\n=== read-back vs expected-graph.json ===")
    results = verify(api, expected)
    return report(results)


def report(results):
    counts = {MATCH: 0, MISMATCH: 0, UNVERIFIED: 0}
    for r in results:
        counts[r["state"]] += 1
        mark = {MATCH: "ok  ", MISMATCH: "FAIL", UNVERIFIED: "????"}[r["state"]]
        print(f"  [{mark}] {r['check']}" + (f"  -- {r['detail']}" if r["detail"] else ""))
    print(f"\n{counts[MATCH]} match, {counts[MISMATCH]} mismatch, {counts[UNVERIFIED]} unverified")
    if counts[MISMATCH]:
        print("=> exit 1: 기대와 다른 값이 관측됐다.")
        return 1
    if counts[UNVERIFIED]:
        print("=> exit 2: MISMATCH 는 없지만 확인 못 한 항목이 남았다. 성공이 아니다.")
        print("   unverified != 영향 없음. 미확인은 미확인으로 남긴다.")
        return 2
    print("=> exit 0: 전부 일치.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
