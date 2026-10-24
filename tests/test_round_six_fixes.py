"""Round six: the fifth consecutive round where the worst findings were
regressions of the previous round's fixes.

2026-07-29. Six reviewers on 7482163. Every test here failed before its fix.
"""


import pytest

from semantix.adapters.agents_schema import _lookml_filters
from semantix.adapters.catalog import load_catalog
from semantix.extraction.canonicalize import canonicalize_sql
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import PROOF_LEVELS, CanonLevel, Provenance
from semantix.signing import receipt_trust

CATALOG = {
    "raw.orders": {"order_id": "bigint", "customer_id": "bigint", "status": "varchar",
                   "channel": "varchar", "amount": "decimal(18,2)"},
    "raw.stg": {"order_id": "bigint", "amt": "decimal(18,2)"},
}


def core(sql, dialect="duckdb", cat=None):
    registry = SchemaRegistry(cat or CATALOG)
    canon = canonicalize_sql(sql, registry.sqlglot_schema(dialect), dialect)
    fps = extract_fingerprints(canon, "analytics.m", Provenance("t", "t"),
                               dialect, registry)
    if not fps:
        return None, "none", ()
    return fps[0].core.hash(), fps[0].canon_level, fps[0].gaps


def not_certifiable(a, b, dialect="duckdb"):
    ha, la, _ = core(a, dialect)
    hb, lb, _ = core(b, dialect)
    both_proof = la in PROOF_LEVELS and lb in PROOF_LEVELS
    return not (ha is not None and ha == hb and both_proof)


# ------------------------------------------------------------- extraction

def test_distinct_on_ordering_is_semantic():
    """DISTINCT ON's ORDER BY decides which row survives per key — an order-
    sensitive consumer that is neither an aggregate nor a window, invisible
    to both consumer checks, so asc/desc variants falsely certified in outer
    AND inner positions (the round-five keep_inner strip made the inner one
    a regression)."""
    assert not_certifiable(
        "select distinct on (status) status, sum(amount) as m from raw.orders "
        "group by status, customer_id order by status, sum(amount) desc",
        "select distinct on (status) status, sum(amount) as m from raw.orders "
        "group by status, customer_id order by status, sum(amount) asc")
    assert not_certifiable(
        "with dm as (select distinct on (customer_id) customer_id, amount "
        "from raw.orders order by customer_id, amount desc) "
        "select sum(amount) as m from dm",
        "with dm as (select distinct on (customer_id) customer_id, amount "
        "from raw.orders order by customer_id, amount asc) "
        "select sum(amount) as m from dm")


def test_single_item_grouping_sets_ordinals_resolve():
    """`GROUPING SETS ((status), (2))` parses items as Paren, not Tuple — the
    Tuple-only descent left `(2)` unresolved and two different single-column
    breakdowns hashed identically at proof grade."""
    assert not_certifiable(
        "select status, channel, sum(amount) as m from raw.orders "
        "group by grouping sets ((status), (2))",
        "select status, customer_id, sum(amount) as m from raw.orders "
        "group by grouping sets ((status), (2))")


@pytest.mark.parametrize("pair", [
    ("with s as (select status, amount from raw.orders order by amount desc), "
     "w as (select status, first_value(amount ignore nulls) over (partition by status) "
     "as fv from s) select status, sum(fv) as m from w group by status",
     "with s as (select status, amount from raw.orders order by amount asc), "
     "w as (select status, first_value(amount ignore nulls) over (partition by status) "
     "as fv from s) select status, sum(fv) as m from w group by status"),
    ("with s as (select amount from raw.orders where status='done' order by amount desc) "
     "select mode(amount) as m from s",
     "with s as (select amount from raw.orders where status='done' order by amount asc) "
     "select mode(amount) as m from s"),
])
def test_wrapped_and_omitted_order_sensitive_functions_are_gated(pair):
    """`first_value(x IGNORE NULLS)` parses as IgnoreNulls(FirstValue) — the
    wrapper hid the function name from the gate; and `mode()` (tie-break =
    first seen) was a one-word omission from the table the previous round
    audited."""
    assert not_certifiable(*pair)


def test_decorative_order_in_unrelated_cte_is_neutral():
    """keep_inner was a whole-AST boolean: one FIRST anywhere preserved a
    cosmetic `order by 2` in a joined lookup CTE, fabricating a difference
    between models that compute identical numbers. The keep-set is now the
    consumer's single-source chain."""
    a = core("with src as (select customer_id, amount from raw.orders order by amount asc), "
             "tot as (select status, sum(amount) as t from raw.orders group by 1 order by 2) "
             "select src.customer_id, first(src.amount) as m from src join tot on tot.t > 0 "
             "group by 1")
    b = core("with src as (select customer_id, amount from raw.orders order by amount asc), "
             "tot as (select status, sum(amount) as t from raw.orders group by 1) "
             "select src.customer_id, first(src.amount) as m from src join tot on tot.t > 0 "
             "group by 1")
    assert a[0] == b[0]


def test_dbt_convention_cte_names_do_not_blow_up_cycle_detection():
    """_cyclic_names lacked the no-db guard its two siblings got: a CTE `stg`
    reading the physical raw.stg read as a self-edge, marking every CTE
    cyclic and restoring the exponential blowup (~600x at 30 CTEs)."""
    import time
    parts = ["stg as (select order_id, amt as amount from raw.stg)"]
    for i in range(14):
        for side in ("a", "b"):
            src = f"l{i-1}_a" if i else "stg"
            src2 = f"l{i-1}_b" if i else "stg"
            parts.append(f"l{i}_{side} as (select order_id, sum(amount) as amount "
                         f"from {src} group by 1 union all select order_id, "
                         f"sum(amount) as amount from {src2} group by 1)")
    sql = "with " + ",\n".join(parts) + "\nselect sum(amount) as m from l13_a"
    start = time.perf_counter()
    core(sql)
    assert time.perf_counter() - start < 5.0


def test_correlated_subquery_alias_does_not_enter_the_hash():
    """The top-scope-only rename left correlated references on the AUTHOR's
    alias, so identical queries by two authors contradicted at proof grade
    and the canonical text carried a dangling alias."""
    template = ("select sum({a}.amount) as m from raw.orders {a} "
                "where {a}.amount > (select coalesce(sum(r.amt), 0) from raw.stg r "
                "where r.order_id <= {a}.order_id)")
    ha, la, _ = core(template.format(a="o"))
    hb, lb, _ = core(template.format(a="orders_src"))
    if la in PROOF_LEVELS and lb in PROOF_LEVELS:
        assert ha == hb, "a pure alias choice changed a proof-grade hash"


def test_multi_database_estates_bind_both_reference_forms():
    """Round five's replication bound only the first-seen database (70%
    coverage on PROD+DEV) and an `_`-only fallback broke fully-qualified
    refs. Short entries now live under `_` AND replicas, with short
    references PINNED to `_` before optimizing — neither form is ambiguous."""
    # Round seven found the first version registered models with NO AST, so
    # the schema stayed two-part and the test exercised neither padding,
    # replication nor pinning. Real models make the estate three-part.
    cols = {"id": "bigint", "region": "varchar", "amount": "decimal(18,2)"}
    registry = SchemaRegistry({"landing.raw_t": cols})
    for relation, ref in (("PROD.analytics.m1", "PROD.LANDING.RAW_T"),
                          ("DEV.analytics.m2", "DEV.LANDING.RAW_T")):
        canon = canonicalize_sql(f"select region, sum(amount) as m from {ref} group by 1",
                                 registry.sqlglot_schema("snowflake"), "snowflake")
        registry.register_model(relation, canon.ast)
    assert set(registry._nested()) >= {"_", "PROD", "DEV"}
    assert registry.short_entries(), "short entries must exist on a mixed-depth estate"
    for sql in ("select region, sum(amount) as m from PROD.LANDING.RAW_T group by 1",
                "select region, sum(amount) as m from DEV.LANDING.RAW_T group by 1",
                "select region, sum(amount) as m from landing.raw_t group by 1"):
        canon = canonicalize_sql(sql, registry.sqlglot_schema("snowflake"), "snowflake")
        assert canon.level == CanonLevel.OPTIMIZED, f"{sql}: {canon.error}"
        # And the short form binds to the CATALOG relation, never to `_`.
        assert "_." not in canon.ast.sql()


def test_reaggregation_refusal_survives_spelling_variants():
    """The exact-key aggregate_outputs lookup meant `ANALYTICS.MARTS.M` or a
    database-omitted spelling bypassed the refusal and the sev-96
    metric-over-mart noise returned."""
    from semantix.adapters.base import DeclaredSpec
    from semantix.engine import build_claim
    registry = SchemaRegistry(
        {"analytics.raw.orders": {"customer_id": "bigint", "amount": "decimal(18,2)"}})
    mart = canonicalize_sql(
        "select customer_id, sum(amount) as revenue_m from analytics.raw.orders group by 1",
        registry.sqlglot_schema("snowflake"), "snowflake")
    registry.register_model("analytics.marts.mart_orders", mart.ast)
    for spelling in ("ANALYTICS.MARTS.MART_ORDERS", "marts.mart_orders",
                     "Analytics.Marts.Mart_Orders"):
        claim = build_claim(
            DeclaredSpec(system="yaml", name="revenue_m", model=spelling,
                         expr="sum(revenue_m)"), registry, "snowflake")
        assert "re-aggregation" in (claim.error or ""), spelling


# ------------------------------------------------------------------ engine

def test_cte_mediated_and_transitive_readers_of_contested_relations(tmp_path):
    """touches() saw only DIRECT readers: a CTE-mediated reader's join_nodes
    are cte:<digest> strings and a downstream model's source_columns resolve
    PAST the contested relation — both kept signed CERTIFIED receipts over
    scan-order-ambiguous lineage. Contamination now propagates through the
    dependency graph."""
    from semantix.engine import run_scan
    root = tmp_path / "sql" / "analytics"
    root.mkdir(parents=True)
    # Two DIFFERENT definitions racing for one relation name.
    (root / "aaa.sql").write_text(
        "-- semantix: relation=analytics.base_rev\n"
        "select customer_id, sum(amount) as rev from raw.orders "
        "where status = 'completed' group by 1")
    (root / "zzz.sql").write_text(
        "-- semantix: relation=analytics.base_rev\n"
        "select customer_id, sum(amount) as rev from raw.orders group by 1")
    # A CTE-mediated reader and a transitive reader.
    (root / "mid.sql").write_text(
        "select customer_id, sum(rev) as rev from analytics.base_rev group by 1")
    (root / "cte_rev.sql").write_text(
        "with s as (select customer_id, sum(rev) as r from analytics.base_rev group by 1) "
        "select sum(r) as cte_rev from s")
    (root / "hop_rev.sql").write_text(
        "select sum(rev) as hop_rev from analytics.mid")
    (tmp_path / "catalog.yml").write_text(
        "raw:\n  orders:\n    customer_id: bigint\n    status: varchar\n"
        "    amount: 'decimal(18,2)'\n")
    result, _ = run_scan(sql_dir=str(tmp_path / "sql"),
                         catalog_path=str(tmp_path / "catalog.yml"),
                         db_path=str(tmp_path / "s.db"))
    surviving = [v for v in result.verdicts
                 if v.verdict_type.value in ("CERTIFIED", "ALIASING")
                 and v.concept_label in ("cte_rev", "hop_rev")]
    assert not surviving, (
        f"non-direct consumers of a contested relation kept certificates: "
        f"{[(v.concept_label, v.verdict_type.value) for v in surviving]}")


# ------------------------------------------------------------------- trust

def test_partial_no_drift_receipts_are_historical_not_drifted():
    """Round six: a partial-no-drift receipt must never be told 'the estate has
    drifted', and the advice must change the outcome. Round eight: it is also
    not valid for CURRENT use — systems that were not re-checked may hold the
    very change that breaks the certificate — and the warning names them, so
    re-attesting with those sources yields a different (fresh) receipt."""
    receipt = {"status": "CERTIFIED",
               "freshness": {"status": "partial", "unchecked_systems": ["metrics_yaml"],
                             "estate_drift": {"changed": [], "added": [], "removed": []}}}
    trusted, warnings = receipt_trust(receipt)
    assert not trusted
    assert not any("has drifted" in w for w in warnings)
    assert any("metrics_yaml" in w for w in warnings)
    fresh = {"status": "CERTIFIED", "freshness": {"status": "fresh"}}
    assert receipt_trust(fresh)[0] is True


def test_stale_with_drift_is_still_distrusted():
    receipt = {"status": "CERTIFIED",
               "freshness": {"status": "stale",
                             "estate_drift": {"changed": ["dbt:model.x"]}}}
    trusted, _ = receipt_trust(receipt)
    assert not trusted


def test_as_of_receipts_survive_current_estate_drift():
    """An as-of receipt is ABOUT a past instant; current drift is disclosure,
    not decay — distrusting it made every historical receipt permanently
    unverifiable."""
    receipt = {"status": "CERTIFIED", "as_of": "2026-01-01T00:00:00.000Z",
               "freshness": {"status": "stale",
                             "estate_drift": {"changed": ["dbt:model.x"]}}}
    trusted, warnings = receipt_trust(receipt)
    assert trusted
    assert any("valid for that instant only" in w for w in warnings)
    # Round nine: valid for its instant is not valid for current use.
    from semantix.signing import assess_receipt
    assert assess_receipt(receipt).current_use is False


# ----------------------------------------------------------------- adapters

def test_unbalanced_legacy_block_does_not_fabricate():
    """A filters string truncated at an export's VARCHAR limit matched
    neither legacy regex and fell through to the modern parser, which matched
    the `value: "US"` sub-token — the round-four value-column fabrication
    resurrected through the round-five remainder path."""
    predicates, untranslatable = _lookml_filters('{ field: status, value: "US"')
    assert predicates == []
    assert any("unterminated" in u or "cannot be read" in u for u in untranslatable)


def test_legacy_shapes_inside_quoted_values_do_not_fabricate():
    predicates, _ = _lookml_filters(
        '[docs: "use { field: status, value: "x" } form"]')
    assert "status = 'x'" not in predicates


def test_balanced_legacy_plus_modern_still_works():
    predicates, _ = _lookml_filters('{ field: region, value: "US" } [status: "completed"]')
    assert "region = 'US'" in predicates
    assert "status = 'completed'" in predicates


def test_non_string_lookml_sql_is_refused():
    """str()-coercing a dict produced `sum({'dialect': 'x'})` — a valid
    DuckDB struct literal, canonicalized at proof grade as a definition
    nobody wrote."""
    from semantix.adapters.agents_schema import _lookml_expr
    expr, untranslatable = _lookml_expr({"sql": {"dialect": "x"}, "type": "sum"})
    assert expr is None
    assert untranslatable


def test_all_struct_table_recognized_across_parents(tmp_path):
    """The single-parent sibling rule missed table evidence one branch over:
    prod.analytics.orders proves tables live at depth three, so
    prod.raw.events (all-struct) is a table — not a namespace minting a
    four-part phantom."""
    (tmp_path / "c.yml").write_text(
        "prod:\n"
        "  raw:\n    events:\n      payload:\n        user_id: bigint\n"
        "  analytics:\n    orders:\n      id: bigint\n")
    catalog = load_catalog(tmp_path / "c.yml")
    assert "prod.raw.events" in catalog
    assert "prod.raw.events.payload" not in catalog


def test_none_stub_does_not_fabricate_a_relation(tmp_path):
    """`orders: null` beside a real table converted the whole namespace into
    a fabricated relation with a column typed "None" and deleted the sibling
    table."""
    (tmp_path / "c.yml").write_text(
        "raw:\n  orders:\n  customers:\n    customer_id: bigint\n")
    catalog = load_catalog(tmp_path / "c.yml")
    assert "raw.customers" in catalog
    assert "raw" not in catalog


def test_supersession_disclosure_does_not_block_certification(tmp_path):
    """The OR REPLACE supersession entry was routed through the unreadable-
    artifact channel, so a routine replay dump PERMANENTLY un-certified the
    estate with the false reason 'could not be read' — the statement WAS
    read; OR REPLACE means the end state is the truth."""
    from semantix.engine import run_scan
    dump = tmp_path / "views.sql"
    dump.write_text(
        "CREATE VIEW analytics.net_revenue AS SELECT SUM(amount) AS net_revenue "
        "FROM raw.orders WHERE status = 'x';\n"
        "CREATE OR REPLACE VIEW analytics.net_revenue AS SELECT SUM(amount) AS "
        "net_revenue FROM raw.orders WHERE status = 'completed';\n")
    (tmp_path / "catalog.yml").write_text(
        "raw:\n  orders:\n    status: varchar\n    amount: 'decimal(18,2)'\n")
    (tmp_path / "metrics.yml").write_text(
        "metrics:\n  - name: net_revenue\n    model: raw.orders\n"
        "    expr: sum(amount)\n    filters: [\"status = 'completed'\"]\n")
    result, _ = run_scan(views_path=str(dump),
                         catalog_path=str(tmp_path / "catalog.yml"),
                         metrics_path=str(tmp_path / "metrics.yml"),
                         db_path=str(tmp_path / "s.db"))
    kinds = {v.verdict_type.value for v in result.verdicts}
    assert "CERTIFIED" in kinds, (
        f"a disclosed supersession blocked certification: "
        f"{[(v.verdict_type.value, v.summary[:80]) for v in result.verdicts]}")
    assert any(s.get("informational") for s in result.skipped)


def test_mcp_server_refuses_a_missing_store(tmp_path):
    from semantix.mcp import McpServer
    with pytest.raises(ValueError):
        McpServer(str(tmp_path / "typo.db"))
    assert not (tmp_path / "typo.db").exists()
