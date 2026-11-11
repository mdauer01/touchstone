"""The 0.1.2 correctness patch, end to end through `touchstone check`: which
output names a metric, what the final output presents, how many parts a full
name has, which definition applied when a statement ran, and where a
statement ends."""

from __future__ import annotations

import hashlib
import json

import yaml

from touchstone.assess.criteria import finalize, load
from touchstone.extraction.sqlutil import statement_chunks
from touchstone.naming import names_metric, words
from touchstone.services.check import export_records

LINES = """version: 1.1
source: cat.sales.lineitem
filter: l_returnflag <> 'R'
measures:
  - name: revenue
    expr: SUM(l_extendedprice * (1 - l_discount))
"""
ORDERS = """version: 1.1
source: cat.sales.orders
measures:
  - name: average_order_value
    expr: AVG(o_totalprice)
  - name: active_customers
    expr: COUNT(DISTINCT o_custkey)
"""
CATALOG = {"cat": {"sales": {"lineitem": {"l_extendedprice": "decimal", "l_discount": "decimal",
                                          "l_returnflag": "string"}}}}
V6 = load(6)


def package(tmp_path, rows, views=(("cat.sales.revenue_mv", LINES, "2026-11-01T00:00:00Z", None),),
            compare="as_of_execution", catalog=True):
    root = tmp_path / "pilot"
    (root / "views").mkdir(parents=True)
    entries = []
    for name, text, start, end in views:
        path = root / "views" / (name.rsplit(".", 1)[-1] + ".yaml")
        path.write_text(text, encoding="utf-8")
        entries.append({"name": name, "snapshot": f"views/{path.name}",
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "applies": {"from": start, "to": end}})
    with open(root / "history.jsonl", "w", encoding="utf-8") as f:
        for i, row in enumerate(rows):
            row = {"sql": row} if isinstance(row, str) else row
            f.write(json.dumps({"statement_id": f"s{i}", "statement_text": row["sql"],
                                "start_time": row.get("start", "2026-11-10T09:00:00Z"),
                                "client_application": "editor", "session_id": f"x{i}",
                                "catalog": row.get("catalog"), "schema": row.get("schema")})
                    + "\n")
    manifest = {"history": "history.jsonl", "compare_against": compare,
                "window": {"from": "2026-09-01T00:00:00Z", "to": "2026-12-01T00:00:00Z"},
                "authority": {"system": "uc_metrics", "metric_views": entries}}
    if catalog:
        (root / "catalog.yml").write_text(yaml.safe_dump(CATALOG), encoding="utf-8")
        manifest["catalog"] = "catalog.yml"
    (root / "pilot.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return root


def compared(root):
    _, _, records, gaps = export_records(root)
    return records, gaps


def answer(comparison, presented_as="", explanation="", definition_clause=""):
    return {"comparison": comparison, "presented_as": presented_as, "statement_clause": "",
            "definition_clause": definition_clause, "explanation": explanation}


# --- 1. which output names a metric -------------------------------------------------

def test_abbreviations_name_the_metric_and_other_words_are_kept():
    assert words("avgOrderValue") == ("average", "order", "value")
    assert names_metric("avg_order_value", "average_order_value")
    assert names_metric("avg_order_value_1996", "average_order_value")
    assert names_metric("eu_revenue", "revenue") and names_metric("revenue_1996", "revenue")
    assert not names_metric("gross_revenue", "net_revenue")
    assert not names_metric("revenue1996", "revenue")
    assert words("avg_revenue") == ("average", "revenue")   # still says it is an average


def test_an_abbreviated_output_is_compared_like_the_full_name(tmp_path):
    views = (("cat.sales.orders_mv", ORDERS, "2026-11-01T00:00:00Z", None),)
    out = {}
    for alias in ("avg_order_value", "average_order_value"):
        root = package(tmp_path / alias, [f"SELECT AVG(o_totalprice) AS {alias} "
                                          "FROM cat.sales.orders"], views=views)
        records, _ = compared(root)
        out[alias] = [(r["definition"]["name"], r["presented"]) for r in records]
        record = records[0]
        state, _ = finalize("review_needed", answer(
            "departs", alias, "drops the filter", "AVG(o_totalprice)"), record, V6)
        assert state == "review_needed"
    assert [m for m, _ in out["avg_order_value"]] == [m for m, _ in out["average_order_value"]] \
        == ["average_order_value"]


# --- 2. what the final output presents ------------------------------------------------

MONTHLY = ("WITH monthly AS (SELECT DATE_TRUNC('MONTH', ship_date) AS month, "
           "MEASURE(revenue) AS revenue FROM cat.sales.revenue_mv GROUP BY 1) "
           "SELECT AVG(revenue) AS {alias} FROM monthly")


def test_a_metric_referenced_inside_does_not_override_a_separately_named_output(tmp_path):
    root = package(tmp_path, [MONTHLY.format(alias="average_monthly_revenue")])
    [record], _ = compared(root)
    assert record["presented"] == ["average_monthly_revenue"]
    state, _ = finalize("no_issue_flagged", answer("preserved"), record, V6)
    assert state == "no_issue_flagged"


def test_the_same_calculation_presented_as_the_metric_stays_assessable(tmp_path):
    root = package(tmp_path, [MONTHLY.format(alias="revenue")])
    [record], _ = compared(root)
    assert record["presented"] == ["revenue"]
    assert finalize("no_issue_flagged", answer("preserved"), record, V6)[0] == "could_not_assess"
    state, _ = finalize("review_needed", answer(
        "departs", "revenue", "averages monthly totals", "SUM(l_extendedprice * (1 - l_discount))"),
        record, V6)
    assert state == "review_needed"


def test_an_unreadable_final_output_keeps_the_uncertainty(tmp_path):
    root = package(tmp_path, [MONTHLY.format(alias="revenue").replace(
        "SELECT AVG(revenue) AS revenue FROM monthly", "SELECT * FROM monthly")])
    [record], _ = compared(root)
    assert record["presented"] is None
    assert finalize("no_issue_flagged", answer("preserved"), record, V6)[0] == "could_not_assess"


# --- 3. how many parts a full name has -------------------------------------------------

SHORT = ("SELECT SUM(l_extendedprice * (1 - l_discount)) AS revenue FROM lineitem "
         "WHERE l_returnflag <> 'R'")


def test_a_short_name_resolves_from_its_row_with_or_without_a_catalog(tmp_path):
    # Its output names no metric, so only reading the governed source makes it a use.
    unnamed = SHORT.replace("AS revenue", "AS total")
    seen = []
    for with_catalog in (True, False):
        root = package(tmp_path / str(with_catalog),
                       [{"sql": unnamed, "catalog": "cat", "schema": "sales"}],
                       catalog=with_catalog)
        [record], gaps = compared(root)
        assert not gaps and record["session_evidence"]["unresolved"] == []
        seen.append((record["definition"]["view"], sorted(record["schemas"])))
    assert seen == [("cat.sales.revenue_mv", ["cat.sales.lineitem"])] * 2


def test_without_context_the_unresolved_name_is_recorded(tmp_path):
    [record], _ = compared(package(tmp_path, [SHORT], catalog=False))
    assert record["session_evidence"]["unresolved"] == ["lineitem"]


# --- 4. which definition applied when the statement ran ---------------------------------

SOURCE_READ = "SELECT SUM(l_extendedprice * (1 - l_discount)) AS revenue FROM cat.sales.lineitem"
VERSIONS = (("cat.sales.revenue_v1", LINES, "2026-09-01T00:00:00Z", "2026-11-01T00:00:00Z"),
            ("cat.sales.revenue_v2", LINES, "2026-11-01T00:00:00Z", None))


def test_the_definition_that_applied_is_chosen_and_the_expired_one_is_no_rival(tmp_path):
    root = package(tmp_path, [{"sql": SOURCE_READ, "start": "2026-11-10T09:00:00Z"},
                              {"sql": SOURCE_READ + " ", "start": "2026-10-10T09:00:00Z"}],
                   views=VERSIONS)
    records, gaps = compared(root)
    assert not gaps
    assert sorted(r["definition"]["view"] for r in records) == ["cat.sales.revenue_v1",
                                                                "cat.sales.revenue_v2"]


def test_overlapping_definitions_stay_ambiguous(tmp_path):
    overlapping = (("cat.sales.revenue_v1", LINES, "2026-09-01T00:00:00Z", None),
                   ("cat.sales.revenue_v2", LINES, "2026-11-01T00:00:00Z", None))
    records, gaps = compared(package(tmp_path, [SOURCE_READ], views=overlapping))
    assert not records and "more than one approved definition" in gaps[0]["error"]


def test_an_expired_view_the_statement_names_is_not_swapped_for_another(tmp_path):
    sql = "SELECT MEASURE(revenue) AS revenue FROM cat.sales.revenue_v1"
    records, gaps = compared(package(tmp_path, [sql], views=VERSIONS))
    assert not records and "did not apply" in gaps[0]["error"]
    current = package(tmp_path / "current", [sql], views=VERSIONS, compare="current")
    records, gaps = compared(current)
    assert not records and "no longer current" in gaps[0]["error"]


def test_a_missing_start_time_is_not_compared(tmp_path):
    records, gaps = compared(package(tmp_path, [{"sql": SOURCE_READ, "start": None}],
                                     views=VERSIONS))
    assert not records and "start time" in gaps[0]["error"]


# --- 5. where a statement ends ---------------------------------------------------------

DDL = ("CREATE OR REPLACE VIEW cat.sales.revenue_mv WITH METRICS LANGUAGE YAML AS $$\n"
       "version: 1.1\ncomment: >-\n  each line matches one order; each order one customer "
       "-- the customer's nation\nsource: cat.sales.lineitem\n$$")


def test_a_metric_view_body_with_semicolons_is_one_statement():
    chunks = statement_chunks(DDL + ";\nSELECT MEASURE(revenue) AS revenue FROM "
                              "cat.sales.revenue_mv; -- touchstone: metric=revenue\nSELECT 2",
                              "databricks")
    assert [type(t).__name__ for _, t in chunks] == ["Command", "Select", "Select"]
    assert chunks[0][0] == DDL
    assert chunks[1][0].endswith("-- touchstone: metric=revenue")


def test_quotes_and_comments_open_no_body_and_an_unclosed_body_is_one_error():
    chunks = statement_chunks("SELECT '$$;' AS a; SELECT 1 -- $$ ;\n; SELECT 3", "databricks")
    assert [t is not None for _, t in chunks] == [True, True, True]
    chunks = statement_chunks("SELECT 1; CREATE VIEW v WITH METRICS LANGUAGE YAML AS $$ "
                              "version: 1.1; source: t", "databricks")
    assert [t is None for _, t in chunks] == [False, True]


def test_metric_view_ddl_in_a_history_is_not_a_use_left_unassessed(tmp_path):
    records, gaps = compared(package(tmp_path, [DDL, SOURCE_READ + " WHERE l_returnflag <> 'R'"]))
    assert not gaps and len(records) == 1
