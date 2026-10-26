"""Bounded conformance: the mandatory cases.

The monitor must tell a governed metric USED correctly (a country filter, a
channel grouping) from a definition quietly changed, and must say
"unverifiable" — never "conforming with a warning" — for everything outside
its rules. Each case below is one acceptance criterion; none may be
weakened to make an implementation pass.

Authority: the approved metric view main.finance.revenue_mv,
    net_revenue = SUM(amount) - SUM(refund_amount), view filter
    status = 'completed', dimensions country, channel, order_date and
    month = DATE_TRUNC('MONTH', order_date).
"""

from __future__ import annotations

import hashlib
import json

import pytest
import yaml

from semantix.adapters.catalog import load_catalog
from semantix.adapters.sql_dir import load_sql_dir
from semantix.cli import main
from semantix.engine import run_scan
from semantix.services.history import read_history
from semantix.services.monitor import build_registry, monitor_history
from semantix.services.pilot import load_pilot

D = "databricks"
CATALOG = """main:
  finance:
    orders:
      amount: DOUBLE
      refund_amount: DOUBLE
      discount: DOUBLE
      status: STRING
      country: STRING
      channel: STRING
      order_date: DATE
    targets:
      channel: STRING
      target: DOUBLE
"""
VIEW = """version: 1.1
source: main.finance.orders
filter: status = 'completed'
dimensions:
  - name: country
    expr: country
  - name: channel
    expr: channel
  - name: order_date
    expr: order_date
  - name: month
    expr: DATE_TRUNC('MONTH', order_date)
{extra}measures:
  - name: net_revenue
    expr: SUM(amount) - SUM(refund_amount)
"""
MODEL = ("-- semantix: relation=main.finance.channel_mart\n"
         "SELECT channel, SUM(amount) AS gross FROM main.finance.orders GROUP BY channel\n")
GOVERNED = "SUM(amount) - SUM(refund_amount)"
T_IN = "2026-10-02T10:00:00Z"


def _rows(*queries, start_time=T_IN, **extra):
    return [{"statement_id": f"s{i}", "statement_text": q, "start_time": start_time,
             "executed_by": "ana@example.com", **extra}
            for i, q in enumerate(queries)]


def make_pilot(tmp_path, rows, *, extra_dims="", compare="as_of_execution",
               applies=("2026-09-01T00:00:00Z", None), context=None,
               view_name="main.finance.revenue_mv", sha=None, view_text=None):
    root = tmp_path / "pilot"
    views = root / "views"
    views.mkdir(parents=True, exist_ok=True)
    snap = views / "revenue_mv.yaml"
    snap.write_text(view_text if view_text is not None else VIEW.format(extra=extra_dims),
                    encoding="utf-8")
    (root / "catalog.yml").write_text(CATALOG, encoding="utf-8")
    (root / "history.jsonl").write_text("\n".join(json.dumps(r) for r in rows),
                                        encoding="utf-8")
    entry = {"name": view_name, "snapshot": "views/revenue_mv.yaml",
             "sha256": sha or hashlib.sha256(snap.read_bytes()).hexdigest()}
    if applies is not None:
        entry["applies"] = {"from": applies[0], "to": applies[1]}
    manifest = {"history": "history.jsonl", "compare_against": compare,
                "catalog": "catalog.yml",
                "authority": {"system": "uc_metrics", "metric_views": [entry]}}
    if context:
        manifest["execution_context"] = context
    (root / "pilot.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    models = tmp_path / "models"
    models.mkdir(exist_ok=True)
    (models / "channel_mart.sql").write_text(MODEL, encoding="utf-8")
    return root, models


def run(tmp_path, rows, **kw):
    root, models = make_pilot(tmp_path, rows, **kw)
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect=D)
    try:
        pilot = load_pilot(root, D)
        registry = build_registry(load_catalog(root / "catalog.yml"),
                                  load_sql_dir(models, D)[0], D)
        return monitor_history(store, read_history(pilot.history), registry, D,
                               min_runs=1, pilot=pilot)
    finally:
        store.close()


def only(result, concept="net_revenue"):
    found = [f for f in result["findings"] if f["concept"] == concept]
    assert len(found) == 1, result["findings"]
    return found[0]


def codes(f):
    return [r["code"] for r in f.get("reasons") or []]


# --------------------------------------------------- legitimate use conforms

def test_approved_slice_and_grouping_conform_raw_table(tmp_path):
    q = (f"SELECT channel, {GOVERNED} AS net_revenue FROM main.finance.orders "
         f"WHERE status = 'completed' AND country = 'US' GROUP BY channel")
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "conforming_use" and f["conformance"] == "conforming"
    assert f["shape"] == "A" and f["governed_reference"] is False
    assert any("country" in s for s in f["variation"]["slices"])
    assert any("channel" in g for g in f["variation"]["grain"])
    assert f["severity"] == 0 and f["identity_source"] == "alias"


def test_approved_slice_and_grouping_conform_metric_view(tmp_path):
    q = ("SELECT channel, MEASURE(net_revenue) AS net_revenue "
         "FROM main.finance.revenue_mv WHERE country = 'US' GROUP BY channel")
    result = run(tmp_path, _rows(q))
    f = only(result)
    assert f["kind"] == "conforming_use" and f["shape"] == "B"
    assert f["governed_reference"] is True
    assert f["identity_source"] == "measure_invocation"
    assert result["funnel"]["governed_references"] == 1
    assert result["funnel"]["conforming_by_shape"]["B"] == 1


def test_month_dimension_and_relative_date_slice_conform(tmp_path):
    q = (f"SELECT DATE_TRUNC('MONTH', order_date) AS m, {GOVERNED} AS net_revenue "
         f"FROM main.finance.orders WHERE status = 'completed' "
         f"AND order_date >= CURRENT_DATE - INTERVAL 30 DAYS GROUP BY 1")
    assert only(run(tmp_path, _rows(q)))["kind"] == "conforming_use"


def test_exact_definition_conforms_whatever_the_syntax(tmp_path):
    q = (f"WITH o AS (SELECT * FROM main.finance.orders) "
         f"SELECT {GOVERNED} AS net_revenue FROM o WHERE status = 'completed'")
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "conforming_use" and f["shape"] == "exact"


# -------------------------------------------- definition changes are caught

def test_required_status_weakened_through_or_never_conforms(tmp_path):
    q = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
         f"WHERE status = 'completed' OR country = 'US'")
    f = only(run(tmp_path, _rows(q)))
    assert f["conformance"] != "conforming"
    assert f["kind"] == "definition_violation"
    assert codes(f) == ["required_filter_not_preserved"]
    assert "OR" in f["reasons"][0]["detail"]


def test_refund_subtraction_removed_is_a_violation(tmp_path):
    q = "SELECT SUM(amount) AS net_revenue FROM main.finance.orders WHERE status = 'completed'"
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "definition_violation"
    assert codes(f) == ["measure_changed"]
    assert f["severity"] >= 60


def test_transformed_governed_measure_never_conforms(tmp_path):
    q = "SELECT MEASURE(net_revenue) * 2 AS net_revenue FROM main.finance.revenue_mv"
    f = only(run(tmp_path, _rows(q)))
    assert f["conformance"] != "conforming"
    assert f["kind"] == "definition_violation"
    assert codes(f) == ["transformed_governed_measure"]
    assert f["governed_reference"] is True   # references, does not preserve


def test_raw_table_transformation_is_named_as_such(tmp_path):
    q = (f"SELECT ({GOVERNED}) * 2 AS net_revenue FROM main.finance.orders "
         f"WHERE status = 'completed'")
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "definition_violation"
    assert codes(f) == ["transformed_governed_measure"]


@pytest.mark.parametrize("wrapper", ["ROUND({}, 2)", "CAST({} AS DOUBLE)", "({}) * 1",
                                     "-(-({}))"])
def test_a_wrapper_that_may_preserve_the_value_is_not_an_accusation(tmp_path, wrapper):
    """Sweep 2, #41/#57: a cast, a rounding, × 1 or a double negation may
    leave the value unchanged — refused honestly, never a violation."""
    q = (f"SELECT {wrapper.format(GOVERNED)} AS net_revenue FROM main.finance.orders "
         f"WHERE status = 'completed'")
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "unverifiable" and codes(f) == ["transformation_undecided"]


@pytest.mark.parametrize("where, code", [
    ("country = 'US'", "required_filter_missing"),
    ("status IN ('completed', 'pending')", "required_filter_not_preserved"),
    ("status = 'pending'", "required_filter_not_preserved"),
])
def test_required_filter_missing_broadened_or_replaced(tmp_path, where, code):
    q = f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders WHERE {where}"
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "definition_violation" and codes(f) == [code]


def test_slice_on_declared_numeric_dimension_conforms(tmp_path):
    q = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
         f"WHERE status = 'completed' AND amount > 0")
    extra = "  - name: amount\n    expr: amount\n"
    assert only(run(tmp_path, _rows(q), extra_dims=extra))["kind"] == "conforming_use"


def test_slice_on_undeclared_column_is_unpermitted(tmp_path):
    q = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
         f"WHERE status = 'completed' AND amount > 0")
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "definition_violation"
    assert codes(f) == ["unpermitted_slice"]


def test_unnamed_violating_near_miss_is_only_a_candidate(tmp_path):
    q = f"SELECT {GOVERNED} AS rev_check FROM main.finance.orders"
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "candidate_alt_definition"
    assert f["severity"] < 60 + 1


# --------------------------------------------------- outside the rules: unverifiable

def test_join_after_governed_calculation_is_unverifiable(tmp_path):
    q = ("WITH g AS (SELECT channel, MEASURE(net_revenue) AS net_revenue "
         "FROM main.finance.revenue_mv GROUP BY channel) "
         "SELECT g.channel, g.net_revenue, t.target FROM g "
         "JOIN main.finance.targets t ON g.channel = t.channel")
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "unverifiable" and codes(f) == ["consumed_downstream"]
    assert f["governed_reference"] is True


def test_short_table_name_with_unknown_context_is_unverifiable(tmp_path):
    q = f"SELECT {GOVERNED} AS net_revenue FROM orders WHERE status = 'completed'"
    result = run(tmp_path, _rows(q))
    (f,) = result["findings"]
    assert f["kind"] == "unverifiable" and codes(f) == ["context_unresolved"]
    assert result["funnel"]["unverifiable_by_reason"] == {"execution context": 1}


def test_short_table_name_resolves_from_its_own_row(tmp_path):
    q = f"SELECT {GOVERNED} AS net_revenue FROM orders WHERE status = 'completed'"
    f = only(run(tmp_path, _rows(q, catalog="main", schema="finance")))
    assert f["kind"] == "conforming_use"


def test_short_table_name_resolves_from_asserted_context_only(tmp_path):
    q = f"SELECT {GOVERNED} AS net_revenue FROM orders WHERE status = 'completed'"
    stated = {"catalog": "main", "schema": "finance"}
    f = only(run(tmp_path, _rows(q), context={**stated, "asserted_for_all_executions": True}))
    assert f["kind"] == "conforming_use"
    (tmp_path / "s.db").unlink()
    result = run(tmp_path, _rows(q), context={**stated, "asserted_for_all_executions": False})
    assert result["findings"][0]["kind"] == "unverifiable"


@pytest.mark.parametrize("tail, code", [
    (" GROUP BY channel HAVING SUM(amount) > 100", "shape_unsupported"),
    (" LIMIT 10", "shape_unsupported"),
])
def test_having_and_limit_are_unverifiable(tmp_path, tail, code):
    q = (f"SELECT {'channel, ' if 'GROUP' in tail else ''}{GOVERNED} AS net_revenue "
         f"FROM main.finance.orders WHERE status = 'completed'{tail}")
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "unverifiable" and codes(f) == [code]


def test_case_dimension_is_unverifiable(tmp_path):
    extra = ("  - name: size\n    expr: CASE WHEN amount > 100 THEN 'big' "
             "ELSE 'small' END\n")
    q = ("SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv "
         "WHERE size = 'big'")
    f = only(run(tmp_path, _rows(q), extra_dims=extra))
    assert f["kind"] == "unverifiable" and codes(f) == ["dimension_unsupported"]
    # The unsupported dimension also means a raw filter on an undeclared
    # column can no longer be called unpermitted.
    (tmp_path / "s.db").unlink()
    q = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
         f"WHERE status = 'completed' AND discount > 0")
    f = only(run(tmp_path, _rows(q), extra_dims=extra))
    assert f["kind"] == "unverifiable" and codes(f) == ["dimensions_not_modelled"]


def test_sql_source_view_is_unverifiable_not_accused(tmp_path):
    view = VIEW.format(extra="").replace(
        "source: main.finance.orders",
        "source: SELECT * FROM main.finance.orders WHERE amount > 0")
    q = f"SELECT SUM(amount) AS net_revenue FROM main.finance.orders"
    f = only(run(tmp_path, _rows(q), view_text=view))
    assert f["kind"] == "unverifiable"
    assert "could not be read" in f["summary"]


# ------------------------------------------------- definition-time evidence

def test_execution_outside_definition_window_is_unverifiable(tmp_path):
    q = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
         f"WHERE status = 'completed'")
    f = only(run(tmp_path, _rows(q, start_time="2026-08-01T00:00:00Z")))
    assert f["kind"] == "unverifiable" and codes(f) == ["no_definition_evidence"]


def test_execution_without_start_time_is_unverifiable(tmp_path):
    q = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
         f"WHERE status = 'completed'")
    rows = [{"statement_id": "s0", "statement_text": q}]
    result = run(tmp_path, rows)
    assert only(result)["kind"] == "unverifiable"
    assert result["history"]["executions_without_time"] == 1


def test_compare_against_current_labels_every_finding(tmp_path):
    q = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
         f"WHERE status = 'completed'")
    result = run(tmp_path, _rows(q, "SELECT SUM(amount) AS net_revenue FROM main.finance.orders",
                                 start_time="2020-01-01T00:00:00Z"),
                 compare="current", applies=None)
    assert result["findings"]
    for f in result["findings"]:
        assert "not verified as of execution time" in f["compare_against"]
    assert {f["kind"] for f in result["findings"]} == {"conforming_use", "definition_violation"}


# ------------------------------------------------------------ intake, pilot

def test_same_statement_id_counts_once(tmp_path):
    q = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
         f"WHERE status = 'completed'")
    rows = _rows(q) * 2
    result = run(tmp_path, rows)
    assert only(result)["uses"] == 1
    assert result["history"]["duplicate_executions"] == 1


def test_pilot_refuses_snapshot_hash_mismatch(tmp_path):
    root, _ = make_pilot(tmp_path, _rows("SELECT 1"), sha="0" * 64)
    with pytest.raises(ValueError, match="does not match its declared sha256"):
        load_pilot(root, D)


def test_pilot_refuses_short_view_name(tmp_path):
    root, _ = make_pilot(tmp_path, _rows("SELECT 1"), view_name="revenue_mv")
    with pytest.raises(ValueError, match="not fully qualified"):
        load_pilot(root, D)


def test_pilot_refuses_as_of_without_window(tmp_path):
    root, _ = make_pilot(tmp_path, _rows("SELECT 1"), applies=None)
    with pytest.raises(ValueError, match="no applies.from"):
        load_pilot(root, D)


def test_pilot_refuses_definition_the_scan_did_not_read(tmp_path):
    root, models = make_pilot(tmp_path, _rows("SELECT 1"))
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect=D)
    snap = root / "views" / "revenue_mv.yaml"
    snap.write_text(snap.read_text().replace("status = 'completed'", "status <> 'void'"))
    manifest = yaml.safe_load((root / "pilot.yml").read_text())
    manifest["authority"]["metric_views"][0]["sha256"] = hashlib.sha256(
        snap.read_bytes()).hexdigest()
    (root / "pilot.yml").write_text(yaml.safe_dump(manifest))
    pilot = load_pilot(root, D)
    registry = build_registry(load_catalog(root / "catalog.yml"),
                              load_sql_dir(models, D)[0], D)
    with pytest.raises(ValueError, match="did not read the pilot's definition"):
        monitor_history(store, read_history(pilot.history), registry, D, pilot=pilot)
    store.close()


def test_pilot_refuses_unknown_keys(tmp_path):
    root, _ = make_pilot(tmp_path, _rows("SELECT 1"))
    manifest = yaml.safe_load((root / "pilot.yml").read_text())
    manifest["tolerance"] = 0.01
    (root / "pilot.yml").write_text(yaml.safe_dump(manifest))
    with pytest.raises(ValueError, match="unknown key"):
        load_pilot(root, D)


# ------------------------------------------------------------- denominators

def test_headline_counts_partition_every_measure_use(tmp_path):
    queries = [
        f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders WHERE status = 'completed' AND country = 'US'",
        "SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv",
        "SELECT SUM(amount) AS net_revenue FROM main.finance.orders WHERE status = 'completed'",
        f"SELECT {GOVERNED} AS net_revenue FROM orders WHERE status = 'completed'",
    ]
    result = run(tmp_path, _rows(*queries))
    fn = result["funnel"]
    assert fn["measure_uses"] == 4
    assert fn["governed_references"] == 1
    assert fn["conforming"] == 2 and fn["violations"] == 1 and fn["unverifiable"] == 1
    assert fn["checked"] == 3
    partition = (fn["conforming"] + fn["contested"] + fn["violations"] + fn["candidates"]
                 + fn["unregistered"] + fn["unverifiable"] + fn["below_threshold"])
    assert partition == fn["measure_uses"]


def test_cli_pilot_run_reports_denominators_and_gates_on_violations(tmp_path, capsys):
    queries = [
        f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders WHERE status = 'completed' AND country = 'US'",
        "SELECT SUM(amount) AS net_revenue FROM main.finance.orders WHERE status = 'completed'",
    ]
    root, models = make_pilot(tmp_path, _rows(*queries))
    db = str(tmp_path / "cli.db")
    assert main(["scan", "--sql-dir", str(models), "--uc-metrics", str(root / "views"),
                 "--catalog", str(root / "catalog.yml"), "--dialect", D, "--db", db]) == 0
    capsys.readouterr()
    args = ["monitor", "--pilot", str(root), "--sql-dir", str(models), "--db", db,
            "--min-runs", "1"]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "measure uses: 2" in out
    assert "conforming" in out and "1/2" in out
    assert "checked = conforming + violations" in out
    assert "[VIOLATION" in out and "[CONFORMING" in out
    assert "not covered:" in out
    assert main(args + ["--fail-on-alt"]) == 1


# ------------------------------------------------- found by the oracle

def test_contradictory_slice_is_never_a_missing_filter(tmp_path):
    """`channel = 'web' AND NOT channel = 'web'` may fold the WHERE to FALSE in
    canonicalization (sqlglot 30.12 does, 30.24 does not), taking the required
    conjunct with it. Reporting "the required filter is not applied" would be
    false (the conformance oracle found it). Folded, it is unverifiable;
    unfolded, it is the definition under an empty slice — never a violation."""
    q = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders WHERE "
         f"status = 'completed' AND channel = 'web' AND NOT channel = 'web'")
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] != "definition_violation"
    if f["kind"] == "unverifiable":
        assert codes(f) == ["predicate_unsupported"]
    else:
        assert f["kind"] == "conforming_use" and len(f["variation"]["slices"]) == 2


def test_required_filter_moved_into_the_measure_is_not_an_accusation(tmp_path):
    """Found by adversarial review: the same numbers, computed with FILTER
    clauses instead of a WHERE, were reported as "required filter not
    applied" — a false statement."""
    for measure in ("SUM(amount) FILTER (WHERE status = 'completed') - "
                    "SUM(refund_amount) FILTER (WHERE status = 'completed')",
                    "SUM(CASE WHEN status = 'completed' THEN amount END) - "
                    "SUM(CASE WHEN status = 'completed' THEN refund_amount END)"):
        q = f"SELECT {measure} AS net_revenue FROM main.finance.orders"
        f = only(run(tmp_path, _rows(q)))
        assert f["kind"] == "unverifiable" and codes(f) == ["implication_undecided"]
        (tmp_path / "s.db").unlink()


def test_short_metric_view_name_is_a_context_question(tmp_path):
    q = "SELECT MEASURE(net_revenue) AS net_revenue FROM revenue_mv"
    f = only_any(run(tmp_path, _rows(q)))
    assert f["kind"] == "unverifiable" and codes(f) == ["context_unresolved"]
    (tmp_path / "s.db").unlink()
    f = only(run(tmp_path, _rows(q, catalog="main", schema="finance")))
    assert f["kind"] == "conforming_use" and f["governed_reference"] is True


def test_rollup_is_an_unsupported_shape_not_an_undeclared_dimension(tmp_path):
    q = (f"SELECT channel, {GOVERNED} AS net_revenue FROM main.finance.orders "
         f"WHERE status = 'completed' GROUP BY ROLLUP (channel)")
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "unverifiable" and "shape_unsupported" in codes(f)


def only_any(result):
    (f,) = result["findings"]
    return f


# ------------------------------------------------- identity and naming

def test_explicit_tag_outranks_the_alias(tmp_path):
    q = (f"-- semantix: metric=net_revenue\nSELECT {GOVERNED} AS rev FROM "
         f"main.finance.orders WHERE status = 'completed' AND country = 'US'")
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "conforming_use" and f["identity_source"] == "tag"


def test_asset_map_names_the_metric_of_a_dashboard(tmp_path):
    q = f"SELECT SUM(amount) AS total FROM main.finance.orders WHERE status = 'completed'"
    root, models = make_pilot(tmp_path, _rows(q, dashboard_id="d42"))
    manifest = yaml.safe_load((root / "pilot.yml").read_text())
    manifest["asset_map"] = {"dashboard:d42": "net_revenue"}
    (root / "pilot.yml").write_text(yaml.safe_dump(manifest))
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect=D)
    pilot = load_pilot(root, D)
    registry = build_registry(load_catalog(root / "catalog.yml"),
                              load_sql_dir(models, D)[0], D)
    result = monitor_history(store, read_history(pilot.history), registry, D,
                             min_runs=1, pilot=pilot)
    store.close()
    f = only(result)
    assert f["kind"] == "definition_violation" and f["identity_source"] == "asset_map"
    assert f["assets"] == ["dashboard:d42"]


def test_governed_measure_under_another_metrics_name_is_flagged(tmp_path):
    view = VIEW.format(extra="") + "  - name: gross_revenue\n    expr: SUM(amount)\n"
    q = ("SELECT MEASURE(net_revenue) AS gross_revenue FROM main.finance.revenue_mv")
    result = run(tmp_path, _rows(q), view_text=view)
    (f,) = result["findings"]
    assert (f["kind"], f["concept"]) == ("definition_violation", "gross_revenue")
    assert codes(f) == ["presented_under_other_name"]
    assert f["calculation_matches"] == "main.finance.revenue_mv/net_revenue (conforming)"
    assert f["governed_reference"] is True
    assert result["funnel"]["violations"] == 1 and result["funnel"]["conforming"] == 0


def test_metric_view_outside_a_pilot_is_not_a_governed_reference(tmp_path):
    root, models = make_pilot(tmp_path, [])
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect=D)
    registry = build_registry(load_catalog(root / "catalog.yml"),
                              load_sql_dir(models, D)[0], D)
    q = "SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv"
    result = monitor_history(store, q + ";\n" + q, registry, D, min_runs=1)
    store.close()
    (f,) = result["findings"]
    assert f["kind"] == "unverifiable" and codes(f) == ["unapproved_view"]
    assert f["governed_reference"] is False
    assert result["funnel"]["governed_references"] == 0


def test_without_a_pilot_slices_are_unverifiable_and_drops_are_violations(tmp_path):
    """Outside a pilot the authority comes from the scan, whose claims do not
    carry dimensions: no slice can be judged — but a dropped required filter
    is still a violation."""
    root, models = make_pilot(tmp_path, [])
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect=D)
    registry = build_registry(load_catalog(root / "catalog.yml"),
                              load_sql_dir(models, D)[0], D)
    sliced = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
              f"WHERE status = 'completed' AND country = 'US'")
    dropped = f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders"
    result = monitor_history(store, ";\n".join([sliced, dropped]), registry, D, min_runs=1)
    store.close()
    by_kind = {f["kind"]: f for f in result["findings"]}
    assert codes(by_kind["unverifiable"]) == ["dimensions_not_modelled"]
    assert codes(by_kind["definition_violation"]) == ["required_filter_missing"]
    assert "not verified as of execution time" in by_kind["unverifiable"]["compare_against"]


# ------------------------------------------------------------ intake

def test_history_formats_and_disclosed_columns(tmp_path):
    from semantix.services.history import read_history as rh

    jl = tmp_path / "h.jsonl"
    jl.write_text("\n".join(json.dumps(r) for r in [
        {"statement_id": "a", "statement_text": "SELECT 1", "start_time": T_IN,
         "query_source": {"dashboard_id": "d1", "notebook_id": None},
         "compute": {"warehouse_id": "w"}},
        {"statement_id": "a", "statement_text": "SELECT 1"},
        {"statement_id": "b", "statement_text": "  "},
    ]))
    intake = rh(jl)
    assert len(intake.executions) == 1 and intake.duplicate_executions == 1
    assert intake.empty_rows == 1 and intake.ignored_columns == ["compute"]
    assert intake.executions[0].asset == "dashboard:d1"
    csv_path = tmp_path / "h.csv"
    csv_path.write_text("QUERY_ID,QUERY_TEXT,DATABASE_NAME,SCHEMA_NAME,USER_NAME\n"
                        "q1,SELECT 1,MAIN,FINANCE,bo\n")
    ex = rh(csv_path).executions[0]
    assert (ex.statement_id, ex.catalog, ex.schema, ex.executed_by) == ("q1", "MAIN", "FINANCE", "bo")


def test_cli_pulls_history_from_a_warehouse_with_named_columns(tmp_path, capsys):
    duckdb = pytest.importorskip("duckdb")
    q = (f"SELECT {GOVERNED} AS net_revenue FROM orders WHERE status = 'completed'")
    root, models = make_pilot(tmp_path, [])
    db = str(tmp_path / "w.db")
    assert main(["scan", "--sql-dir", str(models), "--uc-metrics", str(root / "views"),
                 "--catalog", str(root / "catalog.yml"), "--dialect", D, "--db", db]) == 0
    wh = tmp_path / "hist.duckdb"
    con = duckdb.connect(str(wh))
    con.execute("CREATE TABLE h (statement_id VARCHAR, statement_text VARCHAR, "
                "catalog VARCHAR, schema VARCHAR)")
    con.execute("INSERT INTO h VALUES ('1', ?, 'main', 'finance'), ('2', ?, NULL, NULL)", [q, q])
    con.close()
    capsys.readouterr()
    assert main(["monitor", "--warehouse", str(wh), "--history-query", "SELECT * FROM h",
                 "--sql-dir", str(models), "--catalog", str(root / "catalog.yml"),
                 "--db", db, "--min-runs", "1", "--json", str(tmp_path / "f.json")]) == 0
    result = json.loads((tmp_path / "f.json").read_text())
    kinds = sorted(f["kind"] for f in result["findings"])
    # Row 1 resolves from its own context; row 2 has none and stays unverifiable.
    assert kinds == ["conforming_use", "unverifiable"]


def test_cli_context_assertion_is_explicit(tmp_path, capsys):
    q = f"SELECT {GOVERNED} AS net_revenue FROM orders WHERE status = 'completed'"
    root, models = make_pilot(tmp_path, [])
    hist = tmp_path / "h.sql"
    hist.write_text(q + ";\n")
    db = str(tmp_path / "c.db")
    assert main(["scan", "--sql-dir", str(models), "--uc-metrics", str(root / "views"),
                 "--catalog", str(root / "catalog.yml"), "--dialect", D, "--db", db]) == 0
    base = ["monitor", "--history-file", str(hist), "--sql-dir", str(models),
            "--catalog", str(root / "catalog.yml"), "--db", db, "--min-runs", "1",
            "--default-database", "main", "--default-schema", "finance"]
    capsys.readouterr()
    main(base + ["--json", str(tmp_path / "a.json")])
    assert json.loads((tmp_path / "a.json").read_text())["findings"][0]["kind"] == "unverifiable"
    main(base + ["--assert-context-for-all-executions", "--json", str(tmp_path / "b.json")])
    assert json.loads((tmp_path / "b.json").read_text())["findings"][0]["kind"] == "conforming_use"


# ---------------------------------------------------------- adapter

def test_uc_dimensions_resolve_to_columns_or_say_why_not(tmp_path):
    from semantix.adapters.uc_metrics import load_uc_metrics

    view = tmp_path / "mv.yaml"
    view.write_text("""version: 1.1
source: main.finance.orders
joins:
  - name: customer
    source: main.finance.customers
    on: source.customer_id = customer.id
dimensions:
  - name: country
    expr: billing_country
  - name: channel
    expr: source.channel
  - name: month
    expr: DATE_TRUNC('MONTH', order_date)
  - name: segment
    expr: customer.segment
  - name: size
    expr: CASE WHEN amount > 100 THEN 'big' ELSE 'small' END
measures:
  - name: m
    expr: SUM(amount)
""")
    (spec,), _ = load_uc_metrics(view, D)
    dims = {d.name: d for d in spec.dimensions}
    assert (dims["country"].column, dims["country"].supported) == ("billing_country", True)
    assert dims["channel"].column == "channel" and dims["channel"].supported
    assert (dims["month"].column, dims["month"].truncation) == ("order_date", "MONTH")
    assert not dims["segment"].supported and "joined table" in dims["segment"].reason
    assert not dims["size"].supported and "not a bare column" in dims["size"].reason
    sql_source = tmp_path / "sq.yaml"
    sql_source.write_text(view.read_text().replace(
        "source: main.finance.orders", "source: SELECT * FROM main.finance.orders"))
    (spec,), _ = load_uc_metrics(sql_source, D)
    assert spec.dimensions and not any(d.supported for d in spec.dimensions)


def test_declarations_without_dimensions_keep_their_content_identity():
    from semantix.adapters.base import DeclaredSpec, DimensionSpec, spec_content_text

    plain = DeclaredSpec(system="metrics_yaml", name="m", model="t", expr="sum(a)")
    assert "dimensions" not in spec_content_text(plain)
    with_dims = DeclaredSpec(system="uc_metrics", name="m", model="t", expr="sum(a)",
                             dimensions=[DimensionSpec("c", "c", "c", supported=True)])
    assert spec_content_text(with_dims) != spec_content_text(plain)
