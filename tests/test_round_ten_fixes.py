"""Round ten: external review of ba52094's monitor.

Four reproduced defects, each tested end to end — history rows in, through
attribution and findings, to the headline counts — not only at the
conformance helper:

1. Executions with the same calculation under different metric names were
   one observation, so the violation of the misnamed query was attributed
   to BOTH analysts and both execution ids, with the correctly named
   query's SQL as its sample.
2. The metric-view path checked a list of known-bad shapes, so TABLESAMPLE
   (and time travel) came out conforming.
3. The headline counted an observation's first finding, so a calculation
   match to gross_revenue hid the violation of the net_revenue the query
   claimed to compute.
4. AGG(), Databricks' second spelling of MEASURE(), vanished from the
   measure denominator.
"""

from __future__ import annotations

import json

import pytest

from semantix.cli import main
from .test_monitor_conformance import GOVERNED, T_IN, VIEW, _rows, codes, make_pilot, run

TWO = VIEW.format(extra="") + "  - name: gross_revenue\n    expr: SUM(amount)\n"
GROSS = "SELECT SUM(amount) AS {name} FROM main.finance.orders WHERE status = 'completed'"
_KINDS = {"conforming_use": "conforming", "contested_use": "contested",
          "definition_violation": "violations", "candidate_alt_definition": "candidates",
          "unregistered_recurring_measure": "unregistered", "unverifiable": "unverifiable"}


def headline_consistent(result):
    """The headline is exactly the primary findings, and partitions the uses."""
    fn = result["funnel"]
    for kind, bucket in _KINDS.items():
        primary = sum(f["uses"] for f in result["findings"]
                      if f["role"] == "primary" and f["kind"] == kind)
        assert fn[bucket] == primary, (kind, fn, result["findings"])
    assert sum(fn[b] for b in _KINDS.values()) + fn["below_threshold"] == fn["measure_uses"]
    # A violation finding is never hidden behind a different headline bucket.
    violated = sum(f["uses"] for f in result["findings"]
                   if f["kind"] == "definition_violation" and f["role"] == "primary")
    assert fn["violations"] == violated


def row(sid, sql, who):
    return {"statement_id": sid, "statement_text": sql, "start_time": T_IN, "executed_by": who}


# ------------------------------------------------------ 1. attribution

def test_same_math_under_different_names_is_attributed_per_execution(tmp_path):
    rows = [row("A1", GROSS.format(name="gross_revenue"), "analyst_a"),
            row("B1", GROSS.format(name="net_revenue"), "analyst_b")]
    result = run(tmp_path, rows, view_text=TWO)
    by_concept = {f["concept"]: f for f in result["findings"]}
    assert set(by_concept) == {"gross_revenue", "net_revenue"}
    good, bad = by_concept["gross_revenue"], by_concept["net_revenue"]
    assert good["kind"] == "conforming_use"
    assert (good["executed_by"], good["execution_ids"]) == (["analyst_a"], ["A1"])
    assert "gross_revenue" in good["sample_sql"] and "net_revenue" not in good["sample_sql"]
    assert bad["kind"] == "definition_violation"
    assert (bad["executed_by"], bad["execution_ids"]) == (["analyst_b"], ["B1"])
    assert "net_revenue" in bad["sample_sql"]
    assert "gross_revenue" in bad["calculation_matches"]
    assert result["funnel"]["conforming"] == 1 and result["funnel"]["violations"] == 1
    headline_consistent(result)


def test_recurrence_still_gates_on_the_calculation_not_the_name(tmp_path):
    """Splitting by claimed identity must not let a recurring calculation
    hide below the threshold because each name was used once."""
    rows = [row("A1", GROSS.format(name="gross_revenue"), "analyst_a"),
            row("B1", GROSS.format(name="net_revenue"), "analyst_b")]
    root, models = make_pilot(tmp_path, rows, view_text=TWO)
    from semantix.adapters.catalog import load_catalog
    from semantix.adapters.sql_dir import load_sql_dir
    from semantix.engine import run_scan
    from semantix.services.history import read_history
    from semantix.services.monitor import build_registry, monitor_history
    from semantix.services.pilot import load_pilot

    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect="databricks")
    pilot = load_pilot(root, "databricks")
    registry = build_registry(load_catalog(root / "catalog.yml"),
                              load_sql_dir(models, "databricks")[0], "databricks")
    result = monitor_history(store, read_history(pilot.history), registry, "databricks",
                             min_runs=2, pilot=pilot)
    store.close()
    assert result["funnel"]["below_threshold"] == 0
    assert {f["concept"] for f in result["findings"]} == {"gross_revenue", "net_revenue"}
    headline_consistent(result)


def test_identical_verdicts_are_aggregated_and_nothing_else(tmp_path):
    rows = [row("A1", GROSS.format(name="gross_revenue"), "analyst_a"),
            row("C1", GROSS.format(name="gross_revenue"), "analyst_c"),
            row("B1", GROSS.format(name="net_revenue"), "analyst_b")]
    result = run(tmp_path, rows, view_text=TWO)
    (good,) = [f for f in result["findings"] if f["concept"] == "gross_revenue"]
    assert good["uses"] == 2 and good["executed_by"] == ["analyst_a", "analyst_c"]
    assert sorted(good["execution_ids"]) == ["A1", "C1"]
    (bad,) = [f for f in result["findings"] if f["concept"] == "net_revenue"]
    assert bad["uses"] == 1 and bad["executed_by"] == ["analyst_b"]
    headline_consistent(result)


def test_cli_json_carries_per_execution_attribution(tmp_path, capsys):
    rows = [row("A1", GROSS.format(name="gross_revenue"), "analyst_a"),
            row("B1", GROSS.format(name="net_revenue"), "analyst_b")]
    root, models = make_pilot(tmp_path, rows, view_text=TWO)
    db = str(tmp_path / "cli.db")
    assert main(["scan", "--sql-dir", str(models), "--uc-metrics", str(root / "views"),
                 "--catalog", str(root / "catalog.yml"), "--dialect", "databricks",
                 "--db", db]) == 0
    out = tmp_path / "f.json"
    assert main(["monitor", "--pilot", str(root), "--sql-dir", str(models), "--db", db,
                 "--min-runs", "1", "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "its calculation: declared definition uc_metrics:main.finance.revenue_mv/gross_revenue" in printed
    result = json.loads(out.read_text())
    bad = next(f for f in result["findings"] if f["kind"] == "definition_violation")
    assert bad["executed_by"] == ["analyst_b"] and bad["execution_ids"] == ["B1"]
    headline_consistent(result)


# ------------------------------------------- 2. metric-view allow-list

@pytest.mark.parametrize("sql, fragment", [
    ("SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv "
     "TABLESAMPLE (50 PERCENT)", "TABLESAMPLE"),
    ("SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv "
     "VERSION AS OF 3", "time travel"),
    ("SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv "
     "TIMESTAMP AS OF '2026-01-01'", "time travel"),
    ("SELECT channel, MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv "
     "GROUP BY ROLLUP (channel)", ""),
    ("SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv "
     "WHERE country IN (SELECT country FROM main.finance.orders)", "subquery"),
    ("SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv "
     "WHERE UPPER(country) = 'US'", ""),
    ("SELECT channel, MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv "
     "GROUP BY channel QUALIFY RANK() OVER (ORDER BY channel) = 1", ""),
])
def test_metric_view_constructs_outside_the_allow_list_never_conform(tmp_path, sql, fragment):
    result = run(tmp_path, _rows(sql))
    (f,) = result["findings"]
    assert f["kind"] == "unverifiable" and f["governed_reference"] is True
    assert fragment in json.dumps(f["reasons"])
    assert result["funnel"]["conforming"] == 0
    headline_consistent(result)


def test_metric_view_tablesample_matches_the_raw_table_verdict(tmp_path):
    raw = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
           f"TABLESAMPLE (50 PERCENT) WHERE status = 'completed'")
    view = ("SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv "
            "TABLESAMPLE (50 PERCENT)")
    result = run(tmp_path, _rows(raw, view))
    assert {f["kind"] for f in result["findings"]} == {"unverifiable"}
    assert result["funnel"]["unverifiable"] == 2
    headline_consistent(result)


def test_raw_aggregates_beside_governed_ones_are_disclosed(tmp_path):
    q = ("SELECT MEASURE(net_revenue) AS net_revenue, COUNT(*) AS n "
         "FROM main.finance.revenue_mv")
    result = run(tmp_path, _rows(q))
    (f,) = result["findings"]
    assert f["kind"] == "unverifiable" and "raw aggregate" in json.dumps(f["reasons"])
    assert result["statements_mixing_raw_aggregates"] == 1
    headline_consistent(result)


# ----------------------------------------------------- 3. headline counts

def test_calculation_match_never_hides_the_claimed_metrics_violation(tmp_path):
    result = run(tmp_path, _rows(GROSS.format(name="net_revenue")), view_text=TWO)
    (f,) = result["findings"]
    assert (f["kind"], f["concept"], f["role"]) == ("definition_violation", "net_revenue",
                                                    "primary")
    assert "gross_revenue" in f["calculation_matches"]
    fn = result["funnel"]
    assert (fn["measure_uses"], fn["conforming"], fn["violations"]) == (1, 0, 1)
    headline_consistent(result)


def test_governed_reference_under_another_name_counts_as_a_violation(tmp_path):
    q = "SELECT MEASURE(net_revenue) AS gross_revenue FROM main.finance.revenue_mv"
    result = run(tmp_path, _rows(q), view_text=TWO)
    fn = result["funnel"]
    assert (fn["measure_uses"], fn["conforming"], fn["violations"]) == (1, 0, 1)
    assert fn["governed_references"] == 1
    headline_consistent(result)


def test_mixed_stream_headline_matches_its_findings(tmp_path):
    queries = [GROSS.format(name="gross_revenue"), GROSS.format(name="net_revenue"),
               "SELECT MEASURE(net_revenue) AS gross_revenue FROM main.finance.revenue_mv",
               "SELECT AGG(net_revenue) AS net_revenue FROM main.finance.revenue_mv",
               "SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv "
               "TABLESAMPLE (10 PERCENT)",
               f"SELECT {GOVERNED} AS net_revenue FROM orders WHERE status = 'completed'"]
    result = run(tmp_path, _rows(*queries), view_text=TWO)
    fn = result["funnel"]
    assert fn["measure_uses"] == 6
    assert (fn["conforming"], fn["violations"], fn["unverifiable"]) == (2, 2, 2)
    assert fn["governed_references"] == 3
    headline_consistent(result)


# ------------------------------------------------------------ 4. AGG()

def test_agg_spelling_is_a_measure_reference(tmp_path):
    q = ("SELECT channel, AGG(net_revenue) AS net_revenue FROM main.finance.revenue_mv "
         "WHERE country = 'US' GROUP BY channel")
    result = run(tmp_path, _rows(q))
    (f,) = result["findings"]
    assert f["kind"] == "conforming_use" and f["shape"] == "B"
    assert result["funnel"]["measure_uses"] == 1
    assert result["funnel"]["governed_references"] == 1
    headline_consistent(result)


def test_agg_transformed_and_unapproved_are_counted(tmp_path):
    transformed = "SELECT AGG(net_revenue) * 2 AS net_revenue FROM main.finance.revenue_mv"
    unapproved = "SELECT AGG(net_revenue) AS net_revenue FROM main.finance.other_mv"
    result = run(tmp_path, _rows(transformed, unapproved))
    kinds = sorted((f["kind"], tuple(codes(f))) for f in result["findings"])
    assert kinds == [("definition_violation", ("transformed_governed_measure",)),
                     ("unverifiable", ("unapproved_view",))]
    assert result["funnel"]["measure_uses"] == 2
    headline_consistent(result)


def test_agg_is_read_in_both_parser_shapes():
    """sqlglot 30.12 parses AGG(x) as an anonymous call, 30.24 as exp.Agg
    (found by requalification). Both shapes must read as the same reference,
    whichever parser is installed."""
    from sqlglot import exp

    from semantix.services.conformance import measure_calls, measure_ref

    shapes = [exp.Anonymous(this="AGG", expressions=[exp.column("net_revenue")]),
              exp.Anonymous(this="MEASURE", expressions=[exp.column("net_revenue")])]
    if hasattr(exp, "Agg"):
        shapes.append(exp.Agg(this=exp.column("net_revenue")))
    for node in shapes:
        fn, arg = measure_ref(node)
        assert fn in ("AGG", "MEASURE") and arg.name == "net_revenue"
        assert measure_calls(exp.select(node.as_("n")).from_("main.finance.revenue_mv"))
