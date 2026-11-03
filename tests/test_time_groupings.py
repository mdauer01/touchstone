"""A declared date dimension does not authorize every transformation of it.

The benchmark's stated policy — and governance generally — permits grouping by
declared dimensions exactly as declared. The monitor's rules accepted any
DATE_TRUNC of a bare declared date column as a permitted grouping, so weekly
net revenue "conformed" although only `month` was declared. Now:

* in a GROUPING, a truncation is permitted only when it IS a declared
  dimension, or the operator approved that time grain for the metric
  (pilot.yml `approved_time_groupings`);
* in a FILTER, a truncation of a declared date column stays what it is — a
  time window on that column — and is permitted.
"""

from __future__ import annotations

import pytest
import yaml

from touchstone.services.pilot import load_pilot

from .policy import engine_codes, engine_kind
from .test_monitor_conformance import GOVERNED, VIEW, _rows, make_pilot, run

pytest.importorskip("duckdb")

O = "main.finance.orders"
WEEKLY = (f"SELECT DATE_TRUNC('WEEK', order_date) AS wk, {GOVERNED} AS net_revenue FROM {O} "
          f"WHERE status = 'completed' GROUP BY 1")
MONTHLY = (f"SELECT DATE_TRUNC('MONTH', order_date) AS mo, {GOVERNED} AS net_revenue FROM {O} "
           f"WHERE status = 'completed' GROUP BY 1")
WEEK_WINDOW = (f"SELECT {GOVERNED} AS net_revenue FROM {O} WHERE status = 'completed' "
               f"AND DATE_TRUNC('WEEK', order_date) = DATE '2026-10-05'")
TWO = VIEW.format(extra="") + "  - name: gross_revenue\n    expr: SUM(amount)\n"


def _approve(root, approvals):
    manifest = root / "pilot.yml"
    data = yaml.safe_load(manifest.read_text())
    data["authority"]["metric_views"][0]["approved_time_groupings"] = approvals
    manifest.write_text(yaml.safe_dump(data))


def _run_with(tmp_path, sql, approvals=None, view_text=None):
    from touchstone.adapters.catalog import load_catalog
    from touchstone.adapters.sql_dir import load_sql_dir
    from touchstone.engine import run_scan
    from touchstone.services.history import read_history
    from touchstone.services.monitor import build_registry, monitor_history

    root, models = make_pilot(tmp_path, _rows(sql), view_text=view_text)
    if approvals is not None:
        _approve(root, approvals)
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect="databricks")
    try:
        pilot = load_pilot(root, "databricks")
        registry = build_registry(load_catalog(root / "catalog.yml"),
                                  load_sql_dir(models, "databricks")[0], "databricks")
        return monitor_history(store, read_history(pilot.history), registry, "databricks",
                               min_runs=1, pilot=pilot, export=True)
    finally:
        store.close()


def _only(result, concept="net_revenue"):
    found = [f for f in result["findings"] if f["concept"] == concept]
    assert len(found) == 1, result["findings"]
    return found[0]


def test_an_undeclared_time_grain_is_not_a_permitted_grouping(tmp_path):
    f = _only(run(tmp_path, _rows(WEEKLY)))
    assert engine_kind(f) == "unverifiable"
    assert engine_codes(f) == ["grouping_undeclared"]
    detail = f["reasons"][0]["detail"]
    assert "grouping it by WEEK is neither a declared dimension nor approved" in detail
    assert "approved_time_groupings" in detail


def test_the_declared_truncation_is_still_permitted(tmp_path):
    f = _only(run(tmp_path, _rows(MONTHLY)))
    assert engine_kind(f) == "conforming_use"          # an experimental match


def test_an_approved_time_grain_is_permitted_for_that_metric(tmp_path):
    f = _only(_run_with(tmp_path, WEEKLY, {"net_revenue": {"order_date": ["week"]}}))
    assert engine_kind(f) == "conforming_use"
    # Approval is per metric: approving gross_revenue's weekly grain says nothing
    # about net_revenue's.
    g = _only(_run_with(tmp_path / "b", WEEKLY, {"gross_revenue": {"order_date": ["WEEK"]}},
                        view_text=TWO))
    assert engine_codes(g) == ["grouping_undeclared"]
    # "*" approves it for every measure of the view.
    h = _only(_run_with(tmp_path / "c", WEEKLY, {"*": {"order_date": ["WEEK"]}}))
    assert engine_kind(h) == "conforming_use"


def test_a_truncation_in_a_filter_is_a_time_window(tmp_path):
    f = _only(run(tmp_path, _rows(WEEK_WINDOW)))
    assert engine_kind(f) == "conforming_use"


def test_a_metric_view_grouping_needs_the_same_approval_per_measure(tmp_path):
    q = ("SELECT DATE_TRUNC('WEEK', order_date) AS wk, MEASURE(net_revenue) AS net_revenue, "
         "MEASURE(gross_revenue) AS gross_revenue FROM main.finance.revenue_mv GROUP BY 1")
    result = _run_with(tmp_path, q, {"gross_revenue": {"order_date": ["WEEK"]}}, view_text=TWO)
    net, gross = _only(result), _only(result, "gross_revenue")
    assert "grouping_undeclared" in engine_codes(net)
    assert engine_kind(gross) == "conforming_use"


def test_approvals_reach_the_exported_record(tmp_path):
    result = _run_with(tmp_path, WEEKLY, {"net_revenue": {"order_date": ["WEEK", "QUARTER"]}})
    (record,) = result["comparisons"]
    policy = record["definition"]["variation_policy"]
    assert "DATE_TRUNC('WEEK', order_date) (approved for net_revenue)" in \
        policy["permitted_groupings"]
    assert "does not authorize grouping by every truncation" in policy["text"]


@pytest.mark.parametrize("approvals, message", [
    ({"no_such_metric": {"order_date": ["WEEK"]}}, "is not a measure of this view"),
    ({"net_revenue": {"region": ["WEEK"]}}, "not a plain declared column dimension"),
    ({"net_revenue": {"month": ["WEEK"]}}, "not a plain declared column dimension"),
    ({"net_revenue": {"order_date": ["FORTNIGHT"]}}, "unknown unit"),
    ({"net_revenue": {"order_date": []}}, "list the approved units"),
    ({}, "must map a measure"),
])
def test_an_approval_must_name_a_measure_a_date_column_and_a_unit(tmp_path, approvals, message):
    root, _ = make_pilot(tmp_path, _rows(WEEKLY))
    _approve(root, approvals)
    with pytest.raises(ValueError, match=message):
        load_pilot(root, "databricks")


# ------------------------------------------- round-18 review

def test_a_metric_view_grouping_by_the_declared_month_is_permitted(tmp_path):
    q = ("SELECT DATE_TRUNC('MONTH', order_date) AS mo, MEASURE(net_revenue) AS net_revenue "
         "FROM main.finance.revenue_mv GROUP BY 1")
    f = _only(_run_with(tmp_path, q))
    assert engine_kind(f) == "conforming_use"


@pytest.mark.parametrize("trunc", ["DATE_TRUNC('MM', order_date)",
                                   "DATE_TRUNC('MON', order_date)",
                                   "DATE_TRUNC('mm', order_date)",
                                   "TRUNC(order_date, 'MM')"])
def test_unit_synonyms_are_the_same_grain(tmp_path, trunc):
    sql = (f"SELECT {trunc} AS mo, {GOVERNED} AS net_revenue FROM {O} "
           f"WHERE status = 'completed' GROUP BY 1")
    assert engine_kind(_only(run(tmp_path, _rows(sql)))) == "conforming_use"


# Databricks returns NULL for a unit it does not accept — the query is one
# grand-total group, not the governed grain. The parser rewrites 'W' to WEEK
# and reads an unquoted `month` as the keyword, so these are decided from the
# unit as written.
@pytest.mark.parametrize("trunc, approvals", [
    ("DATE_TRUNC('MONTHS', order_date)", None),
    ("DATE_TRUNC('WEEKS', order_date)", {"net_revenue": {"order_date": ["WEEK"]}}),
    ("DATE_TRUNC('W', order_date)", {"net_revenue": {"order_date": ["WEEK"]}}),
    ("DATE_TRUNC('QTR', order_date)", {"net_revenue": {"order_date": ["QUARTER"]}}),
    ("TRUNC(order_date, 'DD')", {"net_revenue": {"order_date": ["DAY"]}}),
    ("TRUNC(order_date, 'DAY')", {"net_revenue": {"order_date": ["DAY"]}}),
    ("DATE_TRUNC(month, order_date)", None),
])
def test_a_unit_the_engine_does_not_accept_is_no_grain(tmp_path, trunc, approvals):
    sql = (f"SELECT {trunc} AS g, {GOVERNED} AS net_revenue FROM {O} "
           f"WHERE status = 'completed' GROUP BY 1")
    f = _only(_run_with(tmp_path, sql, approvals))
    assert engine_kind(f) != "conforming_use"
    assert "experimental" not in f or f["experimental"] is None


def test_duckdb_reads_its_own_spellings():
    """The DuckDB table is what the installed DuckDB executes: every spelling
    truncates exactly as its grain does, and the Databricks-only spellings are
    not DuckDB's."""
    duckdb = pytest.importorskip("duckdb")
    from touchstone.extraction.sqlutil import _DUCKDB_DATE_TRUNC, canonical_unit

    con = duckdb.connect()
    ts = "TIMESTAMP '2026-11-17 13:45:12.345678'"   # no two grains coincide
    for spelling, grain in _DUCKDB_DATE_TRUNC.items():
        got, want = con.execute(f"SELECT date_trunc('{spelling}', {ts}), "
                                f"date_trunc('{grain}', {ts})").fetchone()
        assert got == want, spelling
    for spelling in ("MM", "YYYY", "DD", "QTR", "WK", "HH"):
        assert canonical_unit(spelling, "duckdb") is None
        with pytest.raises(duckdb.Error):
            con.execute(f"SELECT date_trunc('{spelling}', {ts})")


def test_an_approval_may_use_a_synonym_and_keys_may_not_collide(tmp_path):
    daily = (f"SELECT DATE_TRUNC('DAY', order_date) AS d, {GOVERNED} AS net_revenue FROM {O} "
             f"WHERE status = 'completed' GROUP BY 1")
    f = _only(_run_with(tmp_path, daily, {"net_revenue": {"order_date": ["dd"]}}))
    assert engine_kind(f) == "conforming_use"
    root, _ = make_pilot(tmp_path / "wk", _rows(WEEKLY))
    _approve(root, {"net_revenue": {"order_date": ["WK"]}})    # not a Databricks unit
    with pytest.raises(ValueError, match="unknown unit"):
        load_pilot(root, "databricks")
    root, _ = make_pilot(tmp_path / "b", _rows(WEEKLY))
    _approve(root, {"net_revenue": {"order_date": ["WEEK"]},
                    "NET_REVENUE": {"order_date": ["QUARTER"]}})
    with pytest.raises(ValueError, match="listed twice"):
        load_pilot(root, "databricks")
    _approve(root, {"net_revenue": {"order_date": ["WEEK"], "ORDER_DATE": ["QUARTER"]}})
    with pytest.raises(ValueError, match="listed twice"):
        load_pilot(root, "databricks")


def test_exported_session_history_follows_time_and_carries_statement_text(tmp_path):
    from .test_monitor_conformance import T_IN

    rows = [{"statement_id": "q1", "session_id": "s1", "start_time": "2026-10-02T10:00:00Z",
             "executed_by": "ana", "statement_text":
             f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
             f"WHERE status = 'completed'"},
            {"statement_id": "u1", "session_id": "s1", "start_time": "2026-10-02T10:05:00Z",
             "executed_by": "ana", "statement_text": "USE CATALOG dev"},
            {"statement_id": "q2", "session_id": "s1", "start_time": "2026-10-02T10:10:00Z",
             "executed_by": "ana", "statement_text":
             "CREATE TEMP VIEW o AS SELECT * FROM main.finance.orders WHERE amount > 0;\n"
             f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
             f"WHERE status = 'completed' AND country = 'US'"}]
    assert T_IN
    from touchstone.adapters.catalog import load_catalog
    from touchstone.adapters.sql_dir import load_sql_dir
    from touchstone.engine import run_scan
    from touchstone.services.history import read_history
    from touchstone.services.monitor import build_registry, monitor_history

    root, models = make_pilot(tmp_path, rows)
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect="databricks")
    try:
        pilot = load_pilot(root, "databricks")
        registry = build_registry(load_catalog(root / "catalog.yml"),
                                  load_sql_dir(models, "databricks")[0], "databricks")
        result = monitor_history(store, read_history(pilot.history), registry, "databricks",
                                 min_runs=1, pilot=pilot, export=True)
    finally:
        store.close()
    by_id = {r["execution"]["id"]: r for r in result["comparisons"]}
    assert by_id["q1"]["session_evidence"]["events"] == []        # the USE ran AFTER it
    q2 = by_id["q2"]["session_evidence"]["events"]
    # Across executions the monitor's own rule says "possibly" — here the
    # times order it, and the session is the same: that is what is stated.
    assert "USE CATALOG dev (in an earlier execution of this session)" in q2
    assert "CREATE TEMP VIEW o AS SELECT * FROM main.finance.orders WHERE amount > 0" in q2


def test_the_exported_policy_says_what_is_undecided_not_required(tmp_path):
    (record,) = _run_with(tmp_path, MONTHLY)["comparisons"]
    text = record["definition"]["variation_policy"]["text"]
    assert "UNDECIDED (neither permitted nor a difference)" in text
    assert "a required filter not stated as a top-level condition" in text
    assert "must hold as a top-level condition" not in text


def test_a_key_written_twice_is_refused_not_half_read(tmp_path):
    root, _ = make_pilot(tmp_path, _rows(WEEKLY))
    manifest = root / "pilot.yml"
    # The same measure twice: YAML keeps only QUARTER and drops WEEK silently.
    text = manifest.read_text().replace(
        "    sha256:", "    approved_time_groupings:\n"
                      "      net_revenue: {order_date: [WEEK]}\n"
                      "      net_revenue: {order_date: [QUARTER]}\n    sha256:")
    assert text.count("net_revenue:") == 2
    manifest.write_text(text)
    with pytest.raises(ValueError, match="'net_revenue' appears twice"):
        load_pilot(root, "databricks")


def test_a_metric_view_grain_may_be_approved_on_any_date_dimension_of_the_column(tmp_path):
    two_dates = VIEW.format(extra="  - name: placed_on\n    expr: order_date\n")
    q = ("SELECT DATE_TRUNC('WEEK', order_date) AS wk, MEASURE(net_revenue) AS net_revenue "
         "FROM main.finance.revenue_mv GROUP BY 1")
    f = _only(_run_with(tmp_path, q, {"net_revenue": {"placed_on": ["WEEK"]}},
                        view_text=two_dates))
    assert engine_kind(f) == "conforming_use"
    g = _only(_run_with(tmp_path / "b", q, None, view_text=two_dates))
    assert engine_codes(g) == ["grouping_undeclared"]
    assert "approve it on order_date or placed_on" in g["reasons"][0]["detail"]


# ------------------------------------------- round-18 verification, second pass

def test_an_exact_core_does_not_bypass_the_written_unit(tmp_path):
    view = VIEW.format(extra="").replace(
        "filter: status = 'completed'",
        "filter: status = 'completed' AND DATE_TRUNC('WEEK', order_date) >= DATE '2026-01-05'")
    for unit in ("'W'", "week"):
        q = (f"SELECT {GOVERNED} AS net_revenue FROM {O} WHERE status = 'completed' "
             f"AND DATE_TRUNC({unit}, order_date) >= DATE '2026-01-05'")
        f = _only(_run_with(tmp_path / unit.strip("'"), q, view_text=view))
        assert engine_kind(f) != "conforming_use", unit
        assert engine_codes(f) == ["shape_unsupported"]


def test_a_grain_of_a_truncated_dimension_is_not_the_declared_grain(tmp_path):
    view = VIEW.format(extra="  - name: order_week\n    expr: DATE_TRUNC('WEEK', order_date)\n")
    q = ("SELECT DATE_TRUNC('{u}', order_week) AS g, MEASURE(net_revenue) AS net_revenue "
         "FROM main.finance.revenue_mv GROUP BY 1")
    f = _only(_run_with(tmp_path, q.format(u="MONTH"), view_text=view))
    assert engine_codes(f) == ["grouping_undeclared"]
    assert "itself a truncation" in f["reasons"][0]["detail"]
    g = _only(_run_with(tmp_path / "y", q.format(u="YEAR"), {"net_revenue": {"order_date": ["YEAR"]}},
                        view_text=view))
    assert engine_codes(g) == ["grouping_undeclared"]
    h = _only(_run_with(tmp_path / "w", q.format(u="WEEK"), view_text=view))
    assert engine_kind(h) == "conforming_use"            # its own truncation again


@pytest.mark.parametrize("unit", ["' month '", "'month '"])
def test_a_unit_is_not_trimmed(tmp_path, unit):
    sql = (f"SELECT DATE_TRUNC({unit}, order_date) AS mo, {GOVERNED} AS net_revenue FROM {O} "
           f"WHERE status = 'completed' GROUP BY 1")
    assert engine_kind(_only(run(tmp_path, _rows(sql)))) != "conforming_use"


@pytest.mark.parametrize("dialect, call, grain", [
    ("bigquery", "DATE_TRUNC(order_date, MONTH)", "MONTH"),
    ("bigquery", "TIMESTAMP_TRUNC(order_ts, HOUR)", "HOUR"),
    ("bigquery", "DATE_TRUNC(order_date, 'MONTH')", None),         # a string is not a part
    ("snowflake", "DATE_TRUNC('mm', order_date)", "MONTH"),
    ("snowflake", "DATE_TRUNC('wk', order_date)", "WEEK"),
    ("snowflake", "DATE_TRUNC(month, order_date)", None),          # undocumented form
    ("duckdb", "DATE_TRUNC('yearweek', order_date)", "WEEK"),
    ("databricks", "DATE_TRUNC('MM', order_date)", "MONTH"),
    ("postgres", "DATE_TRUNC('month', order_date)", "MONTH"),
    ("postgres", "DATE_TRUNC('mon', order_date)", None),           # no verified table
])
def test_units_are_read_per_dialect_as_written(dialect, call, grain):
    import sqlglot

    from touchstone.extraction.sqlutil import (annotate_truncations, truncation_of,
                                             unaccepted_truncation)

    sql = f"SELECT {call} FROM t"
    tree = sqlglot.parse_one(sql, read=dialect)
    annotate_truncations(tree, sql, dialect)
    node = tree.expressions[0]
    got = truncation_of(node)
    assert (got[0] if got else None) == grain
    reason = unaccepted_truncation(tree, dialect)
    assert (reason is None) is (grain is not None)
    if dialect == "snowflake" and grain is None:
        assert "quoted strings" in reason and "names a column" not in reason
    if dialect == "postgres" and grain is None:
        assert "no verified reading" in reason


def test_a_metric_view_query_names_the_unit_it_was_written_with(tmp_path):
    q = ("SELECT DATE_TRUNC('W', order_date) AS wk, MEASURE(net_revenue) AS net_revenue "
         "FROM main.finance.revenue_mv GROUP BY 1")
    f = _only(_run_with(tmp_path, q, {"net_revenue": {"order_date": ["WEEK"]}}))
    assert engine_codes(f) == ["shape_unsupported"]
    assert "by 'W'" in f["reasons"][0]["detail"]


def test_a_definition_unit_databricks_rejects_is_not_read_as_a_grain(tmp_path):
    from touchstone.adapters.uc_metrics import load_uc_metrics

    view = VIEW.format(extra="").replace(
        "filter: status = 'completed'",
        "filter: status = 'completed' AND DATE_TRUNC('W', order_date) >= DATE '2026-01-05'")
    path = tmp_path / "v.yaml"
    path.write_text(view)
    specs, _ = load_uc_metrics(path, "databricks")
    assert all("WEEK" not in " ".join(s.filters or []) for s in specs)


def test_nested_merge_overrides_load_and_duplicates_still_refuse(tmp_path):
    from touchstone.adapters.loadutil import load_yaml

    ok = tmp_path / "ok.yml"
    ok.write_text("defaults: &D {k: 0, j: 9}\nfirst: {inner: &N {<<: *D, k: 1}}\n"
                  "second: {<<: *N}\n")
    data = load_yaml(ok)
    assert data["first"]["inner"] == {"k": 1, "j": 9} and data["second"] == {"k": 1, "j": 9}
    bad = tmp_path / "bad.yml"
    bad.write_text("a: {k: 1, k: 2}\n")
    with pytest.raises(ValueError, match="appears twice"):
        load_yaml(bad)
