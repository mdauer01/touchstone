"""Round fifteen, part two: what an adversarial sweep found next to the two
reported findings. Four independent hunters (projection references, the
DISTINCT ON fix, definition-time eligibility, verdict truth), each claim
re-run by a separate skeptic; 33 confirmed, about 26 distinct.

Extraction cases are pinned by EXECUTION: each pair returns different
DuckDB results, and the engine must not call the two fingerprints equal at
proof grade. Monitor cases are pinned end to end through a pilot.
"""

from __future__ import annotations

import hashlib
import json

import pytest
import yaml

from semantix.extraction.canonicalize import canonicalize_sql
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import PROOF_LEVELS, Provenance
from semantix.services.pilot import load_pilot

from .test_monitor_conformance import GOVERNED, T_IN, VIEW, _rows, codes, make_pilot, run
from .test_round_fifteen_fixes import three_view_pilot
from .test_round_ten_fixes import headline_consistent

duckdb = pytest.importorskip("duckdb")

CAT = {"raw.orders": {"amount": "DOUBLE", "refund": "DOUBLE", "country": "VARCHAR",
                      "tier": "INTEGER"}}
SEED = ("CREATE SCHEMA raw; CREATE TABLE raw.orders(amount DOUBLE, refund DOUBLE, "
        "country VARCHAR, tier INTEGER); INSERT INTO raw.orders VALUES "
        "(100, 1, 'US', 1), (300, 2, 'US', 2), (900, 9, 'CA', 1), (5, 0, NULL, 1), "
        "(50, 5, 'CA', 2)")


def _fp(sql, name="revenue"):
    registry = SchemaRegistry(CAT)
    registry.set_estate_depth(["raw.orders"])
    canon = canonicalize_sql(sql, registry.sqlglot_schema("duckdb"), "duckdb")
    found = [f for f in extract_fingerprints(canon, "x", Provenance("q", "x"), "duckdb",
                                             registry) if f.concept_candidate == name]
    assert found, sql
    return found[0]


def _proof(fp) -> bool:
    return fp.canon_level in PROOF_LEVELS and not fp.gaps


def no_false_equality(a, b, name="revenue"):
    con = duckdb.connect()
    con.execute(SEED)
    ra = sorted(map(repr, con.execute(a).fetchall()))
    rb = sorted(map(repr, con.execute(b).fetchall()))
    assert ra != rb, f"pair does not differ on the seed: {a}"
    fa, fb = _fp(a, name), _fp(b, name)
    assert not (_proof(fa) and _proof(fb) and fa.core.hash() == fb.core.hash()), (
        f"FALSE EQUALITY\n{a}\n{b}\n{fa.core}\n{ra} vs {rb}")


# ============================================================== extraction

@pytest.mark.parametrize("a, b", [
    # ORDER BY ALL orders by every projection, left to right
    ("SELECT SUM(refund) AS k, SUM(amount) AS revenue FROM raw.orders GROUP BY country "
     "ORDER BY ALL LIMIT 1",
     "SELECT -SUM(refund) AS k, SUM(amount) AS revenue FROM raw.orders GROUP BY country "
     "ORDER BY ALL LIMIT 1"),
    ("SELECT SUM(refund) AS k, SUM(amount) AS revenue FROM raw.orders GROUP BY country "
     "ORDER BY ALL LIMIT 1",
     "SELECT SUM(amount) AS revenue, SUM(refund) AS k FROM raw.orders GROUP BY country "
     "ORDER BY ALL DESC LIMIT 1"),
    ("SELECT SUM(refund) AS k, SUM(amount) AS revenue FROM raw.orders GROUP BY country "
     "ORDER BY ALL OFFSET 1",
     "SELECT -SUM(refund) AS k, SUM(amount) AS revenue FROM raw.orders GROUP BY country "
     "ORDER BY ALL OFFSET 1"),
])
def test_order_by_all_is_spelled_out(a, b):
    no_false_equality(a, b)


def test_order_by_all_equals_its_explicit_spelling():
    a = ("SELECT SUM(refund) AS k, SUM(amount) AS revenue FROM raw.orders GROUP BY country "
         "ORDER BY ALL LIMIT 1")
    b = a.replace("ORDER BY ALL", "ORDER BY SUM(refund), SUM(amount)")
    assert _fp(a).core.hash() == _fp(b).core.hash()


@pytest.mark.parametrize("a, b", [
    # a projection alias shadowing a source column, referenced in HAVING
    ("SELECT COALESCE(country, 'ZZ') AS country, SUM(amount) AS revenue FROM raw.orders "
     "GROUP BY country HAVING country IS NULL",
     "SELECT COALESCE(country, 'ZZ') AS c, SUM(amount) AS revenue FROM raw.orders "
     "GROUP BY country HAVING COALESCE(country, 'ZZ') IS NULL"),
    ("SELECT refund AS amount, SUM(amount) AS revenue FROM raw.orders GROUP BY refund "
     "HAVING SUM(amount) > 200",
     "SELECT refund AS amount, SUM(amount) AS revenue FROM raw.orders GROUP BY refund "
     "HAVING SUM(refund) > 200"),
])
def test_having_name_that_is_both_alias_and_source_column(a, b):
    no_false_equality(a, b)
    assert any("both a projection alias and a source column" in g for g in _fp(a).gaps)


def test_having_on_a_plain_aggregate_alias_stays_proof_grade():
    fp = _fp("SELECT country, SUM(amount) AS revenue FROM raw.orders GROUP BY country "
             "HAVING revenue > 100")
    assert _proof(fp)


@pytest.mark.parametrize("a, b", [
    # CTE GROUP BY ALL keys pruned
    ("WITH g AS (SELECT country, refund, SUM(amount) AS s FROM raw.orders GROUP BY ALL) "
     "SELECT COUNT(*) AS revenue FROM g",
     "WITH g AS (SELECT country, SUM(amount) AS s FROM raw.orders GROUP BY ALL) "
     "SELECT COUNT(*) AS revenue FROM g"),
    # CTE ORDER BY ALL whose leading sort key the outer query never reads
    ("WITH t AS (SELECT SUM(refund) AS k, country FROM raw.orders GROUP BY country "
     "ORDER BY ALL LIMIT 1) SELECT SUM(o.amount) AS revenue FROM raw.orders o JOIN t "
     "ON o.country = t.country",
     "WITH t AS (SELECT -SUM(refund) AS k, country FROM raw.orders GROUP BY country "
     "ORDER BY ALL LIMIT 1) SELECT SUM(o.amount) AS revenue FROM raw.orders o JOIN t "
     "ON o.country = t.country"),
])
def test_all_shorthand_survives_projection_pruning(a, b):
    no_false_equality(a, b)


@pytest.mark.parametrize("a, b", [
    ("SELECT COUNT(*) AS revenue FROM (SELECT 'all' AS segment, SUM(amount) AS total "
     "FROM raw.orders) t WHERE segment = 'all'",
     "SELECT COUNT(*) AS revenue FROM raw.orders"),
    ("WITH t AS (SELECT 'all' AS segment, SUM(amount) AS total FROM raw.orders) "
     "SELECT COUNT(*) AS revenue FROM t",
     "SELECT COUNT(*) AS revenue FROM raw.orders"),
])
def test_pruning_an_ungrouped_aggregate_is_not_a_table_scan(a, b):
    no_false_equality(a, b)


@pytest.mark.parametrize("a, b", [
    # a constant the optimizer leaves in DISTINCT ON / ORDER BY is not an ordinal
    ("WITH s AS (SELECT amount, refund, country, 2 AS g FROM raw.orders) "
     "SELECT DISTINCT ON (g) SUM(amount) AS revenue, SUM(refund) AS k FROM s "
     "GROUP BY country, g ORDER BY revenue DESC",
     "WITH s AS (SELECT amount, refund, country, 2 AS g FROM raw.orders) "
     "SELECT DISTINCT ON (2) SUM(amount) AS revenue, SUM(refund) AS k FROM s "
     "GROUP BY country, g ORDER BY revenue DESC"),
    ("SELECT DISTINCT ON (CASE WHEN 1 = 1 THEN 2 ELSE 1 END) SUM(amount) AS revenue, "
     "SUM(refund) AS k FROM raw.orders GROUP BY country ORDER BY revenue DESC",
     "SELECT DISTINCT ON (2) SUM(amount) AS revenue, SUM(refund) AS k FROM raw.orders "
     "GROUP BY country ORDER BY revenue DESC"),
    ("WITH s AS (SELECT amount, refund, country, 1 AS p FROM raw.orders) "
     "SELECT SUM(amount) AS revenue FROM s GROUP BY country, p ORDER BY p, revenue DESC "
     "LIMIT 1",
     "SELECT SUM(amount) AS revenue FROM raw.orders GROUP BY country "
     "ORDER BY revenue, revenue DESC LIMIT 1"),
])
def test_merged_constants_are_not_ordinals(a, b):
    no_false_equality(a, b)


@pytest.mark.parametrize("a, b", [
    ("SELECT DISTINCT ON (SUM(refund)) SUM(amount) AS revenue, SUM(refund) % 2 AS k, "
     "SUM(refund) AS k FROM raw.orders GROUP BY country ORDER BY revenue DESC",
     "SELECT DISTINCT ON (SUM(refund) % 2) SUM(amount) AS revenue, SUM(refund) % 2 AS k, "
     "SUM(refund) AS k FROM raw.orders GROUP BY country ORDER BY revenue DESC"),
])
def test_duplicate_output_names_are_not_resolved_by_guess(a, b):
    no_false_equality(a, b)


@pytest.mark.parametrize("a, b", [
    # DISTINCT ON whose ORDER BY only repeats its key, over an ordered CTE
    ("WITH s AS (SELECT amount, refund, country FROM raw.orders ORDER BY amount DESC), "
     "d AS (SELECT DISTINCT ON (country) country, amount FROM s ORDER BY country) "
     "SELECT SUM(amount) AS revenue FROM d",
     "WITH s AS (SELECT amount, refund, country FROM raw.orders ORDER BY amount ASC), "
     "d AS (SELECT DISTINCT ON (country) country, amount FROM s ORDER BY country) "
     "SELECT SUM(amount) AS revenue FROM d"),
    # LIMIT without its own ORDER BY over an ordered CTE
    ("WITH s AS (SELECT amount FROM raw.orders ORDER BY amount DESC), "
     "d AS (SELECT amount FROM s LIMIT 2) SELECT SUM(amount) AS revenue FROM d",
     "WITH s AS (SELECT amount FROM raw.orders ORDER BY amount ASC), "
     "d AS (SELECT amount FROM s LIMIT 2) SELECT SUM(amount) AS revenue FROM d"),
])
def test_input_order_consumers(a, b):
    no_false_equality(a, b)


def test_group_by_a_merged_constant_is_not_a_real_column():
    no_false_equality(
        "SELECT country, tier, SUM(amount) AS revenue FROM raw.orders GROUP BY country, tier",
        "WITH s AS (SELECT amount, country, 1 AS tier FROM raw.orders) "
        "SELECT country, tier, SUM(amount) AS revenue FROM s GROUP BY country, tier")


# ================================================================= monitor

TWO = VIEW.format(extra="") + "  - name: gross_revenue\n    expr: SUM(amount)\n"


@pytest.mark.parametrize("extra", [
    {"statement_text": "/* semantix: metric=gross_revenue */ "
                       "SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv"},
    {"query_tag": "metric=gross_revenue"},
])
def test_explicit_identity_applies_to_metric_view_uses(tmp_path, extra):
    row = {"statement_id": "1", "start_time": T_IN, "statement_text":
           "SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv", **extra}
    result = run(tmp_path, [row], view_text=TWO)
    (f,) = result["findings"]
    assert (f["kind"], f["concept"]) == ("definition_violation", "gross_revenue")
    assert f["identity_source"] in ("tag", "query_tag")
    headline_consistent(result)


def test_a_tag_names_only_its_own_statement(tmp_path):
    from semantix.services.monitor import build_registry, monitor_history
    from semantix.adapters.catalog import load_catalog
    from semantix.adapters.sql_dir import load_sql_dir
    from semantix.engine import run_scan

    root, models = make_pilot(tmp_path, [], view_text=TWO)
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect="databricks")
    registry = build_registry(load_catalog(root / "catalog.yml"),
                              load_sql_dir(models, "databricks")[0], "databricks")
    log = (f"/* semantix: metric=net_revenue */ SELECT {GOVERNED} AS x FROM "
           f"main.finance.orders WHERE status = 'completed';\n"
           f"SELECT {GOVERNED} AS gross_revenue FROM main.finance.orders "
           f"WHERE status = 'completed'")
    result = monitor_history(store, log, registry, "databricks", min_runs=1,
                             authority_systems={"uc_metrics"})
    store.close()
    kinds = sorted((f["kind"], f["concept"], f["identity_source"]) for f in result["findings"])
    assert ("definition_violation", "gross_revenue", "alias") in kinds
    assert ("conforming_use", "net_revenue", "tag") in kinds


def test_case_variant_names_are_one_metric(tmp_path):
    view = VIEW.format(extra="").replace("- name: net_revenue", "- name: Net_Revenue")
    result = run(tmp_path, _rows("SELECT MEASURE(Net_Revenue) AS net_revenue "
                                 "FROM main.finance.revenue_mv"), view_text=view)
    (f,) = result["findings"]
    assert f["kind"] == "conforming_use"


UNREADABLE = TWO.replace("expr: SUM(amount)\n", "expr: SUM(gross_amount)\n").replace(
    "gross_revenue", "total_revenue")


@pytest.mark.parametrize("sql", [
    f"SELECT {GOVERNED} AS total_revenue FROM main.finance.orders WHERE status = 'completed'",
    "SELECT MEASURE(net_revenue) AS total_revenue FROM main.finance.revenue_mv",
])
def test_claiming_an_unreadable_metric_is_never_conforming(tmp_path, sql):
    result = run(tmp_path, _rows(sql), view_text=UNREADABLE)
    (f,) = result["findings"]
    assert f["kind"] == "unverifiable" and f["concept"] == "total_revenue"
    assert "could not be read" in f["summary"]
    assert result["funnel"]["conforming"] == 0
    headline_consistent(result)


def test_unnamed_match_to_only_an_expired_version(tmp_path):
    sql = f"SELECT {GOVERNED} AS x FROM main.finance.orders WHERE status = 'completed'"
    result = three_view_pilot(tmp_path, sql.replace("AS x", "AS some_alias"))
    (f,) = result["findings"]
    # net_revenue (current) has this exact definition: the unnamed query
    # conforms to IT — never to the expired total_revenue.
    assert f["kind"] == "conforming_use" and f["concept"] == "net_revenue"
    assert "total_old_mv" not in f["summary"]


def test_compare_against_current_judges_current_definitions(tmp_path):
    root = tmp_path / "x"
    result = three_view_pilot(root, "SELECT MEASURE(net_revenue) AS total_revenue "
                                    "FROM main.finance.revenue_mv")
    assert result["findings"][0]["kind"] == "definition_violation"
    # same package, compared against CURRENT definitions
    pilot_yml = root / "pilot" / "pilot.yml"
    manifest = yaml.safe_load(pilot_yml.read_text())
    manifest["compare_against"] = "current"
    pilot_yml.write_text(yaml.safe_dump(manifest))
    from semantix.services.history import read_history
    from semantix.services.monitor import build_registry, monitor_history
    from semantix.adapters.catalog import load_catalog
    from semantix.adapters.sql_dir import load_sql_dir
    from semantix.store import Store

    pilot = load_pilot(root / "pilot", "databricks")
    store = Store(str(root / "s.db"))
    registry = build_registry(load_catalog(root / "pilot" / "catalog.yml"),
                              load_sql_dir(root / "models", "databricks")[0], "databricks")
    result = monitor_history(store, read_history(pilot.history), registry, "databricks",
                             min_runs=1, pilot=pilot)
    store.close()
    (f,) = result["findings"]
    assert (f["kind"], f["concept"]) == ("definition_violation", "total_revenue")
    assert "total_old_mv" not in json.dumps(f)
    assert result["reference"]["contested_concepts"] == []
    headline_consistent(result)


def test_unreadable_start_time_is_disclosed_as_unreadable(tmp_path):
    row = {"statement_id": "1", "start_time": "yesterday at noon",
           "statement_text": f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
                             f"WHERE status = 'completed'"}
    result = run(tmp_path, [row])
    assert result["history"]["executions_with_unreadable_time"] == 1
    assert result["history"]["executions_without_time"] == 0
    (f,) = result["findings"]
    assert f["kind"] == "unverifiable" and "could not be read" in f["summary"]


def test_sub_millisecond_window_boundaries_are_exact(tmp_path):
    result = three_view_pilot(tmp_path,
                              f"SELECT SUM(amount) AS total_revenue FROM main.finance.orders "
                              f"WHERE status = 'completed'",
                              start_time="2026-10-01T00:00:00.000300Z",
                              total_old_to="2026-10-01T00:00:00.000500Z",
                              total_new_from="2026-10-01T00:00:00.000500Z")
    (f,) = result["findings"]
    # 0.3 ms is BEFORE the new version: judged against the old one.
    assert "total_old_mv" in f["summary"]


def test_inverted_definition_window_is_refused(tmp_path):
    root, _ = make_pilot(tmp_path, _rows("SELECT 1"),
                         applies=("2026-10-01T00:00:00Z", "2026-09-01T00:00:00Z"))
    with pytest.raises(ValueError, match="not before applies.to"):
        load_pilot(root, "databricks")


def test_executions_outside_the_export_window_are_excluded_and_counted(tmp_path):
    root, models = make_pilot(tmp_path, _rows(
        f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders WHERE status = 'completed'",
        start_time="2026-12-01T00:00:00Z"))
    manifest = yaml.safe_load((root / "pilot.yml").read_text())
    manifest["window"] = {"from": "2026-10-01T00:00:00Z", "to": "2026-10-15T00:00:00Z"}
    (root / "pilot.yml").write_text(yaml.safe_dump(manifest))
    from semantix.services.history import read_history
    from semantix.services.monitor import build_registry, monitor_history
    from semantix.adapters.catalog import load_catalog
    from semantix.adapters.sql_dir import load_sql_dir
    from semantix.engine import run_scan

    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect="databricks")
    pilot = load_pilot(root, "databricks")
    registry = build_registry(load_catalog(root / "catalog.yml"),
                              load_sql_dir(models, "databricks")[0], "databricks")
    result = monitor_history(store, read_history(pilot.history), registry, "databricks",
                             min_runs=1, pilot=pilot)
    store.close()
    assert result["history"]["executions_outside_window"] == 1
    assert result["findings"] == [] and result["funnel"]["measure_uses"] == 0


@pytest.mark.parametrize("prefix", [
    "USE SCHEMA staging; ",
    "CREATE TEMPORARY VIEW orders AS SELECT * FROM main.finance.orders WHERE country = 'US'; ",
])
def test_session_changing_statements_end_relation_identity(tmp_path, prefix):
    sql = f"{prefix}SELECT {GOVERNED} AS net_revenue FROM orders WHERE status = 'completed'"
    result = run(tmp_path, _rows(sql, catalog="main", schema="finance"))
    (f,) = result["findings"]
    assert f["kind"] == "unverifiable" and codes(f) == ["context_unresolved"]
    assert result["history"]["statements_after_session_change"] == 1


DISCOUNT = VIEW.format(extra="").replace("filter: status = 'completed'",
                                         "filter: status = 'completed' AND discount = 0")


@pytest.mark.parametrize("cond", [
    "discount = 0.0", "discount = 0E0", "discount = CAST(0 AS DOUBLE)",
    "discount IN (0, 0.0)",
])
def test_equal_literal_values_are_not_replacements(tmp_path, cond):
    sql = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
           f"WHERE status = 'completed' AND {cond}")
    (f,) = run(tmp_path, _rows(sql), view_text=DISCOUNT)["findings"]
    assert f["kind"] != "definition_violation"


@pytest.mark.parametrize("where", [
    "status IN ('completed', NULL)",
    "status = 'completed' OR status = NULL",
    "(status = 'completed' OR (status = 'completed' AND country = 'US'))",
])
def test_conditions_admitting_nothing_more_are_not_weakenings(tmp_path, where):
    sql = f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders WHERE {where}"
    (f,) = run(tmp_path, _rows(sql))["findings"]
    assert f["kind"] != "definition_violation"


def test_a_genuinely_replaced_value_is_still_a_violation(tmp_path):
    sql = f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders WHERE status = 'pending'"
    (f,) = run(tmp_path, _rows(sql))["findings"]
    assert f["kind"] == "definition_violation" and codes(f) == ["required_filter_not_preserved"]


@pytest.mark.parametrize("projection", ["(MEASURE(net_revenue))", "((AGG(net_revenue)))"])
def test_parentheses_around_a_bare_reference(tmp_path, projection):
    sql = f"SELECT {projection} AS net_revenue FROM main.finance.revenue_mv"
    (f,) = run(tmp_path, _rows(sql))["findings"]
    assert f["kind"] == "conforming_use"


DERIVED = VIEW.format(extra="") + (
    "  - name: gross_revenue\n    expr: SUM(amount)\n"
    "  - name: refunds\n    expr: SUM(refund_amount)\n"
    "  - name: double_net\n    expr: (SUM(amount) - SUM(refund_amount)) * 2\n")


@pytest.mark.parametrize("projection, concept", [
    ("MEASURE(net_revenue) * 2 AS double_net", "double_net"),
    ("MEASURE(gross_revenue) - MEASURE(refunds) AS net_revenue", "net_revenue"),
])
def test_derived_expression_under_a_metric_name_is_not_decided(tmp_path, projection, concept):
    result = run(tmp_path, _rows(f"SELECT {projection} FROM main.finance.revenue_mv"),
                 view_text=DERIVED)
    assert result["funnel"]["violations"] == 0
    assert result["funnel"]["measure_uses"] == 1   # one projection, one use
    headline_consistent(result)


def test_written_aliases_that_look_synthesized_still_claim_a_metric(tmp_path):
    view = TWO.replace("gross_revenue", "sum_revenue")
    sql = f"SELECT {GOVERNED} AS sum_revenue FROM main.finance.orders WHERE status = 'completed'"
    (f,) = run(tmp_path, _rows(sql), view_text=view)["findings"]
    assert (f["kind"], f["concept"]) == ("definition_violation", "sum_revenue")


def test_findings_judged_against_different_versions_are_not_merged(tmp_path):
    sql = f"SELECT {GOVERNED} AS total_revenue FROM main.finance.orders WHERE status = 'completed'"
    rows = [{"statement_id": "a", "statement_text": sql, "start_time": "2026-09-15T00:00:00Z"},
            {"statement_id": "b", "statement_text": sql, "start_time": "2026-10-02T00:00:00Z"}]
    root = tmp_path
    result = three_view_pilot(root, sql)   # builds the package; rerun with both rows
    history = root / "pilot" / "history.jsonl"
    history.write_text("\n".join(json.dumps(r) for r in rows))
    manifest = root / "pilot" / "pilot.yml"
    data = yaml.safe_load(manifest.read_text())
    from semantix.services.history import read_history
    from semantix.services.monitor import build_registry, monitor_history
    from semantix.adapters.catalog import load_catalog
    from semantix.adapters.sql_dir import load_sql_dir
    from semantix.store import Store

    pilot = load_pilot(root / "pilot", "databricks")
    store = Store(str(root / "s.db"))
    registry = build_registry(load_catalog(root / "pilot" / "catalog.yml"),
                              load_sql_dir(root / "models", "databricks")[0], "databricks")
    result = monitor_history(store, read_history(pilot.history), registry, "databricks",
                             min_runs=1, pilot=pilot)
    store.close()
    assert data and len(result["findings"]) == 2
    assert {f["execution_ids"][0] for f in result["findings"]} == {"a", "b"}


def test_conforming_breakdown_excludes_contested_uses(tmp_path):
    view = VIEW.format(extra="")
    other = view.replace("SUM(amount) - SUM(refund_amount)", "SUM(amount)")
    root = tmp_path / "pilot"
    views = root / "views"
    views.mkdir(parents=True)
    (views / "a.yaml").write_text(view)
    (views / "b.yaml").write_text(other)
    from .test_monitor_conformance import CATALOG, MODEL
    (root / "catalog.yml").write_text(CATALOG)
    (root / "history.jsonl").write_text(json.dumps(
        {"statement_id": "1", "start_time": T_IN, "statement_text":
         "SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.a"}))

    def entry(n, f):
        return {"name": n, "snapshot": f"views/{f}",
                "sha256": hashlib.sha256((views / f).read_bytes()).hexdigest(),
                "applies": {"from": "2026-09-01T00:00:00Z", "to": None}}
    (root / "pilot.yml").write_text(yaml.safe_dump({
        "history": "history.jsonl", "compare_against": "as_of_execution",
        "catalog": "catalog.yml", "authority": {"system": "uc_metrics", "metric_views": [
            entry("main.finance.a", "a.yaml"), entry("main.finance.b", "b.yaml")]}}))
    models = tmp_path / "models"
    models.mkdir()
    (models / "m.sql").write_text(MODEL)
    from semantix.services.history import read_history
    from semantix.services.monitor import build_registry, monitor_history
    from semantix.adapters.catalog import load_catalog
    from semantix.adapters.sql_dir import load_sql_dir
    from semantix.engine import run_scan

    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(views),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect="databricks")
    pilot = load_pilot(root, "databricks")
    registry = build_registry(load_catalog(root / "catalog.yml"),
                              load_sql_dir(models, "databricks")[0], "databricks")
    result = monitor_history(store, read_history(pilot.history), registry, "databricks",
                             min_runs=1, pilot=pilot)
    store.close()
    fn = result["funnel"]
    assert fn["contested"] == 1 and fn["conforming"] == 0
    assert sum(fn["conforming_by_shape"].values()) == fn["conforming"]
