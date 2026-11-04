"""What reaches the advisory classifier: every candidate use, once per distinct
judgment — not only what the rules could read, and not once per execution.

The first real-history pilot (2026-11-04) measured the old export: 47 of 161
distinct statements reached a record. Ad-hoc statements (8 of 102) were cut by
the recurrence gate, statements computing the metric inside a CTE (0 of 24)
by measure extraction, and the 47 that got through became 204 records, one per
execution — one paid model call each."""

from __future__ import annotations

import json

from touchstone.adapters.catalog import load_catalog
from touchstone.adapters.sql_dir import load_sql_dir
from touchstone.assess.record import validate
from touchstone.engine import run_scan
from touchstone.services.history import read_history
from touchstone.services.monitor import _normalized_text, build_registry, monitor_history
from touchstone.services.pilot import load_pilot

from .test_monitor_conformance import D, _rows, make_pilot

GOVERNED = ("SELECT SUM(amount) - SUM(refund_amount) AS net_revenue FROM main.finance.orders "
            "WHERE status = 'completed'")
NESTED = ("WITH r AS (SELECT channel, SUM(amount) - SUM(refund_amount) AS rev "
          "FROM main.finance.orders WHERE status = 'completed' GROUP BY channel) "
          "SELECT channel, rev AS net_revenue FROM r")


def export(tmp_path, rows, min_runs=2):
    root, models = make_pilot(tmp_path, rows)
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect=D)
    try:
        pilot = load_pilot(root, D)
        registry = build_registry(load_catalog(root / "catalog.yml"),
                                  load_sql_dir(models, D)[0], D)
        plain = monitor_history(store, read_history(pilot.history), registry, D,
                                min_runs=min_runs, pilot=pilot)
        result = monitor_history(store, read_history(pilot.history), registry, D,
                                 min_runs=min_runs, pilot=pilot, export=True)
    finally:
        store.close()
    # Exporting never changes a finding, the funnel or anything else.
    assert {k: v for k, v in result.items() if k != "comparisons"} == plain
    for record in result["comparisons"]:
        assert validate(record) == []
    return result


def test_a_use_the_rules_cannot_read_and_that_ran_once_still_reaches_the_classifier(tmp_path):
    result = export(tmp_path, _rows(NESTED))
    assert result["statements_with_nested_measures"] == 1 and not result["findings"]
    (record,) = result["comparisons"]
    assert record["execution"]["statement"] == NESTED
    assert record["definition"]["name"] == "net_revenue"
    assert record["claimed_metric"]["name"] == "net_revenue"
    assert "unverified_observations" not in record          # no reading to attach
    assert record["occurrences"]["executions"] == 1


def test_executions_differing_only_in_comments_and_spacing_are_one_judgment(tmp_path):
    a = "-- Looker Query Context '{\"user_id\":412}'\n" + GOVERNED
    b = "-- Looker Query Context '{\"user_id\":77}'\n" + GOVERNED.replace(" FROM", "\n  FROM")
    result = export(tmp_path, _rows(a, b, GOVERNED), min_runs=1)
    (record,) = result["comparisons"]
    assert record["occurrences"]["executions"] == 3
    assert sorted(record["occurrences"]["execution_ids"]) == ["s0", "s1", "s2"]
    # The judged use keeps touchstone's reading; the record shows the first run.
    assert record["unverified_observations"]["finding_kind"]
    assert record["execution"]["statement"] == a


def test_a_different_literal_is_a_different_judgment(tmp_path):
    result = export(tmp_path, _rows(GOVERNED, GOVERNED.replace("'completed'", "'complete'")),
                    min_runs=1)
    assert len(result["comparisons"]) == 2


def test_a_query_that_computes_nothing_or_reads_nothing_governed_is_not_a_candidate(tmp_path):
    result = export(tmp_path, _rows("SELECT * FROM main.finance.orders WHERE status = 'x'",
                                    "SELECT COUNT(*) AS n FROM main.finance.customers"))
    assert result["comparisons"] == []


def test_an_unnamed_aggregate_over_the_governed_source_is_a_candidate(tmp_path):
    result = export(tmp_path, _rows("SELECT channel, SUM(amount) AS total FROM "
                                    "main.finance.orders GROUP BY channel"))
    names = {r["definition"]["name"] for r in result["comparisons"]}
    assert "net_revenue" in names
    assert all(r["claimed_metric"]["identity_evidence"][0]["source"] == "none"
               for r in result["comparisons"])


def test_normalized_text_keeps_quoted_text_byte_for_byte():
    sql = "SELECT  'a  --  b' AS x, /* c */ \"y  z\" FROM t -- tail\nWHERE s = 'it\\'s'"
    assert _normalized_text(sql) == "SELECT 'a  --  b' AS x, \"y  z\" FROM t WHERE s = 'it\\'s'"


def test_assess_run_judges_each_record_once_and_reuses_the_cache(tmp_path, capsys):
    from touchstone.cli import main

    result = export(tmp_path, _rows(GOVERNED, GOVERNED, NESTED))
    comparisons = tmp_path / "c.jsonl"
    comparisons.write_text("".join(json.dumps(r) + "\n" for r in result["comparisons"]))
    out, cache = tmp_path / "v.jsonl", tmp_path / "cache.jsonl"
    argv = ["assess", "run", "--comparisons", str(comparisons), "--out", str(out),
            "--cache", str(cache)]
    assert main(argv) == 0
    first = capsys.readouterr().out
    verdicts = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(verdicts) == len(result["comparisons"]) == 2
    assert sum(v["executions"] for v in verdicts) == 3
    assert "calls 2 · from cache 0" in first and "measures nothing" in first
    assert all(v["advisory"].startswith("advisory") for v in verdicts)
    assert main(argv) == 0
    assert "calls 0 · from cache 2" in capsys.readouterr().out
    assert all(json.loads(line)["from_cache"] for line in out.read_text().splitlines())
