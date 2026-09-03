"""Round five: regressions of round four's fixes, and the fixes' fixes.

2026-07-29. Six adversarial reviewers on commit 8f8ae7a. The dominant theme:
five of the worst findings were introduced BY round-four fixes — the ordinal
resolver that spliced projections into GROUP BY, an order-sensitivity gate
comparing class names that never match, millisecond timestamps that order
backwards against existing stores, a UNION guard parsing SQL that cannot
contain the union, and a "wider" tolerance that was mathematically dead code.
Every test here failed before its fix.
"""

import json
import pathlib
import shutil

import pytest

from semantix.extraction.canonicalize import canonicalize_sql
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import PROOF_LEVELS, CanonLevel, Provenance
from semantix.services.execute import _diverges, _is_additive
from semantix.store import Store, normalize_ts

CATALOG = {
    "raw.orders": {"order_id": "bigint", "customer_id": "bigint", "status": "varchar",
                   "channel": "varchar", "amount": "decimal(18,2)"},
    "raw.stg": {"order_id": "bigint", "amt": "decimal(18,2)"},
}


def core(sql, dialect="duckdb"):
    registry = SchemaRegistry(CATALOG)
    canon = canonicalize_sql(sql, registry.sqlglot_schema(dialect), dialect)
    fps = extract_fingerprints(canon, "analytics.m", Provenance("t", "t"),
                               dialect, registry)
    if not fps:
        return None, "none", ()
    return fps[0].core.hash(), fps[0].canon_level, fps[0].gaps


# ------------------------------------------------------- extraction criticals

def test_int_literals_inside_group_expressions_are_not_ordinals():
    """`container.find_all(exp.Literal)` resolved the `2` in `customer_id % 2`
    to the second projection — splicing a SUM into the grouping — so `% 2` and
    `% 3` collapsed to the same garbage grain and FALSELY CERTIFIED."""
    a = core("select status, sum(amount) as m, sum(amount) as m2 "
             "from raw.orders group by status, customer_id % 2")
    b = core("select status, sum(amount) as m, sum(amount) as m2 "
             "from raw.orders group by status, customer_id % 3")
    assert a[0] != b[0], "hash-bucket groupings falsely certified"


def test_top_level_ordinals_still_resolve():
    a = core("select status, sum(amount) as m from raw.orders group by 1")
    b = core("select status, sum(amount) as m from raw.orders group by status")
    assert a[0] == b[0]
    a = core("select status, channel, sum(amount) as m from raw.orders group by rollup(1, 2)")
    b = core("select channel, status, sum(amount) as m from raw.orders group by rollup(1, 2)")
    assert a[0] != b[0]


@pytest.mark.parametrize("fn", ["array_agg(amount)", "string_agg(amount, ',')",
                                "any_value(amount)", "first(amount)"])
def test_order_sensitive_spellings_are_gated(fn):
    """The gate compared sqlglot CLASS names (ArrayAgg) against SQL spellings
    (ARRAY_AGG) — nine of eleven entries could never match, so array_agg over
    a merged-away inner ORDER BY falsely certified. Only FIRST and LAST — the
    two the round-four test used — happened to work."""
    _, level, gaps = core(
        f"with s as (select customer_id, amount from raw.orders order by amount asc) "
        f"select customer_id, {fn} as m from s group by 1")
    assert level not in PROOF_LEVELS
    assert any("ORDER BY" in g for g in gaps)


def test_ranked_summary_sql_certifies():
    """A blanket window check fired the gap for ANY window plus ANY inner
    ORDER BY — mainstream ranked-summary SQL could never certify, with a gap
    text asserting a consumer that does not exist."""
    _, level, gaps = core(
        "with s as (select customer_id, amount from raw.orders order by customer_id) "
        "select customer_id, sum(amount) as m, rank() over (order by sum(amount) desc) "
        "as rnk from s group by 1")
    assert level in PROOF_LEVELS, gaps


def test_decorative_inner_order_by_is_neutral():
    """Keeping every inner ORDER BY unconditionally fabricated a difference:
    a staging CTE with a cosmetic ordering hashed differently from the same
    CTE without it — a CONTRADICTION between equivalent models."""
    a = core("with s as (select customer_id, sum(amount) as t from raw.orders "
             "group by 1 order by 2) select sum(t) as m from s")
    b = core("with s as (select customer_id, sum(amount) as t from raw.orders "
             "group by 1) select sum(t) as m from s")
    assert a[0] == b[0]


def test_inner_scope_aliases_are_not_rewritten():
    """Alias canonicalization renamed columns across ALL scopes with a rename
    map built for the top scope — a CTE body reusing a top-scope alias name
    had its columns rewritten to the outer t-alias: invalid SQL stored at
    proof grade as the canonical form."""
    registry = SchemaRegistry(CATALOG)
    canon = canonicalize_sql(
        "with f as (select stg.order_id, sum(stg.amt) as s from raw.stg stg group by 1) "
        "select sum(f.s) as m from f join raw.stg stg on f.order_id = stg.order_id",
        registry.sqlglot_schema("duckdb"), "duckdb")
    body = canon.ast.sql("duckdb").split("SELECT", 2)[1]
    assert "t1." not in body.split(")")[0], "outer alias leaked into the CTE body"


def test_cte_digest_ignores_db_qualified_base_name_collisions():
    """A body reading the PHYSICAL raw.stg had the unrelated CTE `stg`'s
    digest folded in because base names matched — renaming that CTE changed
    this body's digest and fabricated a CONTRADICTION on a pure refactor."""
    a = core("with stg as (select status, sum(amount) as n from raw.orders group by 1), "
             "f as (select src.order_id, sum(src.amt) as s from raw.stg src group by 1), "
             "g as (select f.order_id, f.s, stg.n from f join stg on f.s > stg.n) "
             "select sum(g.s) as m from g")
    b = core("with staging as (select status, sum(amount) as n from raw.orders group by 1), "
             "f as (select src.order_id, sum(src.amt) as s from raw.stg src group by 1), "
             "g as (select f.order_id, f.s, staging.n from f join staging on f.s > staging.n) "
             "select sum(g.s) as m from g")
    assert a[0] == b[0], "a pure CTE rename changed the fingerprint"


# --------------------------------------------------------------- schema binding

def test_short_catalog_entries_bind_fully_qualified_references():
    """`_` padding never matches a fully-qualified reference, so a two-part
    hand catalog against three-part SQL bound nothing — 40% coverage on the
    exact scenario round four claimed fixed, with the docstring's "and vice
    versa" being false in this direction. Short entries are replicated under
    every observed head instead."""
    cols = {"order_id": "bigint", "region": "varchar", "amount": "decimal(18,2)"}
    registry = SchemaRegistry({"raw.orders_00": cols,
                               "ANALYTICS.staging.stg_seen": cols})
    for sql in ("select region, sum(amount) as m from ANALYTICS.RAW.ORDERS_00 group by 1",
                "select region, sum(amount) as m from raw.orders_00 group by 1"):
        canon = canonicalize_sql(sql, registry.sqlglot_schema("snowflake"), "snowflake")
        assert canon.level == CanonLevel.OPTIMIZED, f"{sql}: {canon.error}"


# ------------------------------------------------------------------ execution

def test_union_models_are_never_measured_through_one_branch(acme, tmp_path):
    """The round-four guard parsed canonical_sql for a Union node — but a
    set-op fingerprint's canonical_sql IS one branch, so the guard could
    never fire and a `us UNION ALL eu` model still measured one branch,
    manufacturing a +1800% divergence between definitions that agree."""
    pytest.importorskip("duckdb")
    from semantix.models import Concept, Fingerprint, SemanticCore, Verdict, VerdictType
    from semantix.services.execute import annotate_with_execution

    core_obj = SemanticCore(measure_expr="SUM(amount)",
                            source_columns=("raw.orders.amount",))

    def fp(relation, sql, gaps=()):
        return Fingerprint(
            fingerprint_id=Fingerprint.make_id(core_obj, "revenue") + relation,
            concept_candidate="revenue", core=core_obj, relation=relation,
            dialect="duckdb",
            canon_level="incomplete" if gaps else "optimized",
            canonical_sql=sql,
            provenance=Provenance("sql", relation, f"{relation}.sql"),
            gaps=gaps,
        )

    concept = Concept(
        concept_id="c1", label="revenue",
        fingerprints=[
            fp("analytics.m1", "select sum(amount) as revenue from raw.orders"),
            # The union model: stored canonical_sql is ONE BRANCH.
            fp("analytics.c",
               "select sum(amount) as revenue from raw.orders where region = 'us'",
               gaps=("set operation",)),
        ],
    )
    verdict = Verdict(VerdictType.CONTRADICTION, "c1", "revenue", 90, "differ.", {})
    seed = tmp_path / "seed.sql"
    seed.write_text(
        "CREATE SCHEMA IF NOT EXISTS raw;\n"
        "CREATE TABLE raw.orders (region VARCHAR, amount DECIMAL(18,2));\n"
        "INSERT INTO raw.orders VALUES ('us',100),('eu',1800);\n")
    annotate_with_execution([concept], [verdict], [], seed, "duckdb")
    measured = verdict.evidence.get("measured") or {}
    totals = {t["where"]: t["total"] for t in measured.get("totals", [])}
    assert "analytics.c" not in totals, \
        "a branch-only canonical_sql was measured and published as the model's total"


def test_divergence_tolerance_catches_dollars_on_billions():
    """`max(scale*1e-9, ulp*4)` was dead code — a max can only WIDEN and the
    ULP term never won, so behavior was bit-identical to the bug it claimed
    to fix. $1.50 on a $2bn book must diverge; float conversion noise must
    not."""
    assert _diverges(2_000_000_001.50, 2_000_000_000.00)
    assert not _diverges(700.7000000000001, 700.70)
    assert not _diverges(0.0, 0.0)


def test_reciprocals_are_not_additive():
    """Mul/Div accepted any one-literal split, so `1000 / SUM(x)` — a
    reciprocal — was summed across groups and published as measured
    evidence: a number with no interpretation."""
    assert not _is_additive('1000 / SUM(t0."amount")')
    assert _is_additive('SUM(t0."amount") / 100')
    assert _is_additive('SUM(t0."amount") * 100')


# ------------------------------------------------------------------- store

def _whole_second_store(path: pathlib.Path) -> Store:
    """A store written by the PRE-millisecond code: whole-second timestamps."""
    store = Store(path)
    conn = store._conn
    conn.execute("INSERT INTO scans (scan_id, label, dialect, started_at, stats_json) "
                 "VALUES ('s1', 'old', 'duckdb', '2026-01-01T10:00:00Z', '{}')")
    conn.execute("INSERT INTO fingerprints (fingerprint_id, concept_candidate, "
                 "semantic_hash, relation) VALUES ('f1', 'net_revenue', 'aaa', 'x.y')")
    conn.execute("INSERT INTO fingerprints (fingerprint_id, concept_candidate, "
                 "semantic_hash, relation) VALUES ('f2', 'net_revenue', 'bbb', 'x.y')")
    for scan, ts, ref in (("s1", "2026-01-01T10:00:00Z", "f1"),):
        conn.execute("INSERT INTO observations VALUES (?, ?, 'fingerprint', "
                     "'sql:x:net_revenue', ?)", (scan, ts, ref))
    conn.commit()
    return store


def test_old_whole_second_stores_are_migrated_on_open(tmp_path):
    """'…:00Z' sorts AFTER '…:00.500Z' of the same second — the OPPOSITE of
    what the round-four comment claimed — so on any pre-existing store,
    as_of at a recorded instant returned the superseded definition and
    attest --as-of signed the previous scan's verdict for the whole
    transition second. Migration to one width on open is the only sound
    state; representation, not history."""
    db = tmp_path / "old.db"
    store = _whole_second_store(db)
    store.close()
    reopened = Store(db)
    rows = reopened._conn.execute("SELECT started_at FROM scans").fetchall()
    assert rows[0][0] == "2026-01-01T10:00:00.000Z"
    obs = reopened._conn.execute("SELECT observed_at FROM observations").fetchall()
    assert obs[0][0] == "2026-01-01T10:00:00.000Z"
    # And the ordering defect is gone: a new-format timestamp in the same
    # second now orders AFTER the migrated old one.
    assert rows[0][0] < normalize_ts("2026-01-01T10:00:00.500Z")


def test_one_shared_artifact_is_not_absence_proof(tmp_path, monkeypatch, acme):
    """Boolean overlap made ONE shared staging model turn team B's scan into
    absence-proof for ALL of team A's other artifacts — the per-team-CI
    scenario the round-four fix's own comment names. Majority overlap is
    required now; deleting half of a two-model estate is unprovable and
    stays live, which is the direction absence-requires-proof points."""
    import semantix.store as store_mod
    times = iter([f"2026-0{m}-01T00:00:00Z" for m in range(1, 9)])
    now = {"t": next(times)}
    monkeypatch.setattr(store_mod, "utcnow", lambda: now["t"])
    from semantix.engine import run_scan

    def estate(root, files):
        sql = root / "sql" / "analytics"
        sql.mkdir(parents=True, exist_ok=True)
        for name, body in files.items():
            (sql / name).write_text(body)
        return root / "sql"

    shared = "select order_id, amount from raw.orders"
    a = estate(tmp_path / "team_a", {"metric_a.sql":
                                     "select sum(amount) as metric_a from raw.orders",
                                     "stg_shared.sql": shared})
    b = estate(tmp_path / "team_b", {"metric_b.sql":
                                     "select sum(amount) as metric_b from raw.orders",
                                     "stg_shared.sql": shared})
    catalog = tmp_path / "c.yml"
    catalog.write_text("raw:\n  orders:\n    order_id: bigint\n    amount: 'decimal(18,2)'\n")
    db = tmp_path / "s.db"
    run_scan(sql_dir=str(a), catalog_path=str(catalog), db_path=str(db))
    now["t"] = next(times)
    _, store = run_scan(sql_dir=str(b), catalog_path=str(catalog), db_path=str(db))
    live = {i.object_key for i in store.as_of("metric_a", "2026-03-01T00:00:00Z")}
    assert live, "team B's scan retired team A's metric through one shared file"


def test_scan_as_of_skips_interrupted_scans(tmp_path, monkeypatch):
    import semantix.store as store_mod
    times = iter([f"2026-0{m}-01T00:00:00Z" for m in range(1, 9)])
    now = {"t": next(times)}
    monkeypatch.setattr(store_mod, "utcnow", lambda: now["t"])
    store = Store(tmp_path / "s.db")
    done, _ = store.begin_scan("done", "duckdb")
    store.finish_scan(done, {})
    now["t"] = next(times)
    store.begin_scan("interrupted", "duckdb")  # dies here
    picked = store.scan_as_of("2026-03-01T00:00:00Z")
    assert picked["scan_id"] == done, \
        "scan_as_of served a stats-less scan latest_scan disowns"


def test_attest_by_any_member_name_finds_the_concept(acme, tmp_path):
    """The alias-name fallback landed only in issue_receipt; history/as_of
    still derived names from fingerprint candidates only — so a claim-only
    name went dark and a receipt SIGNED 'no definition was live' for a
    concept with two live definitions."""
    shutil.copytree(acme, tmp_path / "estate")
    from semantix.engine import run_scan
    _, store = run_scan(
        dbt_path=str(tmp_path / "estate" / "dbt_project"),
        metrics_path=str(tmp_path / "estate" / "metrics.yml"),
        catalog_path=str(tmp_path / "estate" / "catalog.yml"),
        db_path=str(tmp_path / "s.db"))
    # `gmv` is the declared name; the computed members are total_gmv and
    # gross_merchandise_value. Every one of the three must resolve.
    for name in ("gmv", "total_gmv", "gross_merchandise_value"):
        receipt = store.issue_receipt(name)
        assert receipt is not None, name
        assert receipt.definition_version, \
            f"receipt for {name!r} signed with no live definitions"


def test_readonly_store_opens_for_reading(tmp_path):
    db = tmp_path / "ro.db"
    store = Store(db)
    scan_id, _ = store.begin_scan("x", "duckdb")
    store.finish_scan(scan_id, {})
    store.close()
    # Simulate a read-only mount by removing write permission on dir + file.
    import os
    os.chmod(db, 0o444)
    os.chmod(tmp_path, 0o555)
    try:
        reader = Store(db)
        assert reader.latest_scan()["scan_id"] == scan_id
    finally:
        os.chmod(tmp_path, 0o755)
        os.chmod(db, 0o644)


# ----------------------------------------------------------------- adapters

def test_join_to_timespine_is_read_from_the_measure_reference():
    """dbt serializes it on type_params.measure; the round-four guard checked
    type_params — a location that does not exist in dbt's schema — so the
    refusal never fired on real manifests."""
    from semantix.adapters.dbt import _metric_expression
    metric = {"type": "simple",
              "type_params": {"measure": {"name": "m", "join_to_timespine": True}}}
    expr, _, untranslatable = _metric_expression(
        metric, {"sm": "analytics.t"}, {"m": ("sum", "amount", "sm", {})})
    assert expr is None
    assert any("timespine" in u for u in untranslatable)


def test_filters_outside_legacy_blocks_are_not_dropped():
    """Once one legacy block matched, everything OUTSIDE the blocks — modern
    filters included — was silently dropped: a proof-grade claim missing a
    declared filter, the exact failure `untranslatable` exists to prevent."""
    from semantix.adapters.agents_schema import _lookml_filters
    predicates, untranslatable = _lookml_filters(
        '{ field: region, value: "US" } [status: "completed"]')
    assert "region = 'US'" in predicates
    assert "status = 'completed'" in predicates, "the modern filter vanished"

    predicates, untranslatable = _lookml_filters(
        '{ field: region, value: "US" } { field: status, operator: contains }')
    assert "region = 'US'" in predicates
    assert untranslatable, "an unreadable legacy block vanished without a trace"


def test_a_field_named_field_is_not_a_legacy_block():
    from semantix.adapters.agents_schema import _lookml_filters
    predicates, untranslatable = _lookml_filters('[field: "webinar"]')
    assert predicates == ["field = 'webinar'"]
    assert not untranslatable


def test_all_struct_table_with_table_siblings_is_a_table(tmp_path):
    """A table whose EVERY column is a struct was treated as a namespace —
    deleted, with each struct column becoming a phantom relation — whenever
    no scalar column disambiguated it. A sibling that is unambiguously a
    table proves the level."""
    from semantix.adapters.catalog import load_catalog
    (tmp_path / "c.yml").write_text(
        "analytics:\n"
        "  events:\n"
        "    payload:\n      user_id: bigint\n      ts: timestamp\n"
        "    context:\n      ip: varchar\n"
        "  orders:\n    order_id: bigint\n")
    catalog = load_catalog(tmp_path / "c.yml")
    assert "analytics.events" in catalog, "the all-struct table was deleted"
    assert "analytics.events.payload" not in catalog, "phantom relation returned"


def test_directive_reads_quoted_and_backticked_names(tmp_path):
    from semantix.adapters.sql_dir import load_sql_dir
    root = tmp_path / "sql" / "analytics"
    root.mkdir(parents=True)
    (root / "a.sql").write_text(
        '-- semantix: relation="MY DB".analytics.orders\nselect 1 as x')
    (root / "b.sql").write_text(
        "-- semantix: relation=`prod`.analytics.orders2\nselect 1 as x")
    artifacts, _ = load_sql_dir(tmp_path / "sql")
    relations = {a.relation for a in artifacts}
    assert "MY DB.analytics.orders" in relations, "quoted name truncated at the space"
    assert "prod.analytics.orders2" in relations, "backticked name fell back to the path"


def test_duplicate_view_definitions_are_disclosed(tmp_path):
    """topo_sort's keyed dedup collapsed same-relation CREATE VIEWs before
    duplicate_relations could see them — the superseded contradictor vanished
    without a trace, the one remaining silent-loss channel."""
    from semantix.adapters.sql_dir import load_views_file
    dump = tmp_path / "views.sql"
    dump.write_text(
        "CREATE VIEW analytics.net_revenue AS SELECT SUM(amount) AS net_revenue "
        "FROM raw.orders WHERE status = 'completed';\n"
        "CREATE OR REPLACE VIEW analytics.net_revenue AS SELECT SUM(amount) AS "
        "net_revenue FROM raw.orders;\n")
    artifacts, skipped = load_views_file(dump)
    assert len(artifacts) == 1
    assert "WHERE" not in artifacts[0].sql.upper() or "completed" not in artifacts[0].sql
    assert any("superseded" in s["reason"] for s in skipped), \
        "the superseded definition vanished without a trace"


def test_wrong_shape_metrics_yaml_is_loud(tmp_path):
    """`metrics: "just a string"` ran the scan green with claims 0 — silent
    ontology loss in the one file the customer authors by hand."""
    from semantix.adapters.metrics_yaml import load_metrics
    p = tmp_path / "m.yml"
    p.write_text('metrics: "just a string"\n')
    with pytest.raises(ValueError) as e:
        load_metrics(p)
    assert "m.yml" in str(e.value)


def test_dbt_metrics_declarations_are_freshness_checked(tmp_path):
    """SOURCE_SYSTEMS mapped dbt_path to {"dbt"} only and the sha builder
    never called load_dbt_metrics — declaration drift in the manifest was
    PERMANENTLY invisible: no flag existed that could ever re-check
    dbt_metrics, so a receipt kept verifying after the declared metric's
    meaning changed."""
    from semantix.services.freshness import SOURCE_SYSTEMS, current_estate_shas
    assert "dbt_metrics" in SOURCE_SYSTEMS["dbt_path"]
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "manifest.json").write_text(json.dumps({
        "nodes": {},
        "semantic_models": {"sm": {
            "name": "orders_sm",
            "node_relation": {"relation_name": "analytics.fct_orders"},
            "measures": [{"name": "rev", "agg": "sum", "expr": "amount"}],
        }},
        "metrics": {"m": {"name": "net_revenue", "type": "simple",
                          "type_params": {"measure": {"name": "rev"}}}},
    }))
    shas = current_estate_shas(dbt_path=str(proj))
    assert any(system == "dbt_metrics" for system, _ in shas), \
        "manifest declarations are still not hashed"


# --------------------------------------------------------------- re-aggregation

def test_metric_over_mart_is_refused_with_the_real_reason(tmp_path):
    """A metric declared OVER the mart, re-aggregating the mart's aggregate
    output, produced a sev-96 CONTRADICTION blaming the mart's own internals
    for faithfully restating itself — the standard pilot input turned into
    top-band noise."""
    from semantix.engine import run_scan
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "manifest.json").write_text(json.dumps({"nodes": {
        "model.p.mart_orders": {
            "resource_type": "model", "name": "mart_orders", "schema": "marts",
            "alias": "mart_orders", "database": "analytics",
            "relation_name": "analytics.marts.mart_orders",
            "compiled_code": ("select customer_id, sum(amount) as revenue_m from "
                              "analytics.raw.orders group by 1"),
            "original_file_path": "models/mart_orders.sql",
            "depends_on": {"nodes": []},
        }}}))
    (tmp_path / "catalog.yml").write_text(
        "analytics:\n  raw:\n    orders:\n      customer_id: bigint\n"
        "      amount: 'decimal(18,2)'\n")
    (tmp_path / "metrics.yml").write_text(
        "metrics:\n  - name: revenue_m\n    model: analytics.marts.mart_orders\n"
        "    expr: sum(revenue_m)\n")
    result, _ = run_scan(dbt_path=str(proj), metrics_path=str(tmp_path / "metrics.yml"),
                         catalog_path=str(tmp_path / "catalog.yml"),
                         db_path=str(tmp_path / "s.db"), dialect="snowflake")
    kinds = {v.verdict_type.value for v in result.verdicts
             if v.concept_label == "revenue_m"}
    assert "CONTRADICTION" not in kinds, \
        "a faithful metric-over-mart declaration was blamed as a contradiction"
    claim = next(c for c in result.claims if c.name == "revenue_m")
    assert "re-aggregation" in (claim.error or "")
