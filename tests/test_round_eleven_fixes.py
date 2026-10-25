"""Round eleven: judgment order on the metric-view naming path.

Round ten's "presented under another metric's name" branch ran LAST and
overwrote an established "unverifiable" with a violation: a query run in
2020, against definitions evidenced only from September 2026, came back as
a violation whose own evidence said `net_revenue (unverifiable)`. The same
happened with a missing timestamp and with TABLESAMPLE.

The order is now fixed: (1) definition evidence for the referenced AND the
claimed metric, (2) the supported-SQL boundary, (3) the claimed metric,
(4) the primary finding from those conclusions. A suspected naming
mismatch is kept as context when an earlier step fails.
"""

from __future__ import annotations

import hashlib
import json

import pytest
import yaml

from semantix.adapters.catalog import load_catalog
from semantix.adapters.sql_dir import load_sql_dir
from semantix.engine import run_scan
from semantix.services.history import read_history
from semantix.services.monitor import build_registry, monitor_history
from semantix.services.pilot import load_pilot

from .test_monitor_conformance import CATALOG, MODEL, T_IN, VIEW, codes, run
from .test_round_ten_fixes import headline_consistent

D = "databricks"
TWO = VIEW.format(extra="") + "  - name: gross_revenue\n    expr: SUM(amount)\n"
VIEW_RENAMED = "SELECT MEASURE(net_revenue) AS gross_revenue FROM main.finance.revenue_mv"
RAW_RENAMED = "SELECT SUM(amount) AS net_revenue FROM main.finance.orders WHERE status = 'completed'"
OUTSIDE = "2020-01-01T00:00:00Z"


def one(sql, start_time=T_IN):
    r = {"statement_id": "x1", "statement_text": sql, "executed_by": "ana"}
    if start_time:
        r["start_time"] = start_time
    return [r]


# ------------------------------------------------- the four combined cases

@pytest.mark.parametrize("sql, start_time, kind, code", [
    (VIEW_RENAMED, T_IN, "definition_violation", "presented_under_other_name"),
    (VIEW_RENAMED, OUTSIDE, "unverifiable", "no_definition_evidence"),
    (VIEW_RENAMED, None, "unverifiable", "no_definition_evidence"),
    (VIEW_RENAMED + " TABLESAMPLE (50 PERCENT)", T_IN, "unverifiable", "shape_unsupported"),
    ("SELECT MEASURE(net_revenue) * 2 AS gross_revenue FROM main.finance.revenue_mv",
     T_IN, "definition_violation", "presented_under_other_name"),
    ("SELECT MEASURE(net_revenue) * 2 AS gross_revenue FROM main.finance.revenue_mv",
     OUTSIDE, "unverifiable", "no_definition_evidence"),
])
def test_wrong_metric_name_on_a_metric_view(tmp_path, sql, start_time, kind, code):
    result = run(tmp_path, one(sql, start_time), view_text=TWO)
    (f,) = result["findings"]
    assert (f["kind"], f["concept"], codes(f)) == (kind, "gross_revenue", [code])
    if kind == "unverifiable":
        # The suspicion is kept as context, and no evidence claims more than
        # was established.
        assert f["suspected"].startswith("presented as gross_revenue but computed as")
        assert "calculation_matches" not in f
        assert result["funnel"]["violations"] == 0
    else:
        assert "(conforming)" in f["calculation_matches"] or "* 2" in f["reasons"][0]["detail"]
    if start_time is None:
        assert result["history"]["executions_without_time"] == 1
    headline_consistent(result)


@pytest.mark.parametrize("sql, start_time, kind", [
    (RAW_RENAMED, T_IN, "definition_violation"),
    (RAW_RENAMED, OUTSIDE, "unverifiable"),
    (RAW_RENAMED, None, "unverifiable"),
    (RAW_RENAMED.replace("main.finance.orders", "main.finance.orders TABLESAMPLE (50 PERCENT)"),
     T_IN, "unverifiable"),
])
def test_wrong_metric_name_on_the_raw_table(tmp_path, sql, start_time, kind):
    result = run(tmp_path, one(sql, start_time), view_text=TWO)
    (f,) = result["findings"]
    assert f["kind"] == kind
    if kind == "definition_violation":
        assert f["calculation_matches"].endswith("revenue_mv/gross_revenue")
    elif "TABLESAMPLE" in sql:
        assert codes(f) == ["below_proof_grade"] and "calculation_matches" not in f
    else:
        # Evidence about the matched definition is qualified when that
        # definition is not evidenced for the execution time.
        assert codes(f) == ["no_definition_evidence"]
        assert "not evidenced for the execution time" in f["calculation_matches"]
    headline_consistent(result)


# ---------------------- evidence for BOTH the referenced and claimed metric

GROSS_VIEW = """version: 1.1
source: main.finance.orders
filter: status = 'completed'
dimensions:
  - name: country
    expr: country
measures:
  - name: gross_revenue
    expr: SUM(amount)
"""


def _two_view_pilot(tmp_path, net_from, gross_from, start_time=T_IN):
    root = tmp_path / "pilot"
    views = root / "views"
    views.mkdir(parents=True)
    (views / "revenue_mv.yaml").write_text(VIEW.format(extra=""), encoding="utf-8")
    (views / "gross_mv.yaml").write_text(GROSS_VIEW, encoding="utf-8")
    (root / "catalog.yml").write_text(CATALOG, encoding="utf-8")
    (root / "history.jsonl").write_text(json.dumps(one(VIEW_RENAMED, start_time)[0]),
                                        encoding="utf-8")

    def entry(name, file, start):
        return {"name": name, "snapshot": f"views/{file}",
                "sha256": hashlib.sha256((views / file).read_bytes()).hexdigest(),
                "applies": {"from": start, "to": None}}
    (root / "pilot.yml").write_text(yaml.safe_dump({
        "history": "history.jsonl", "compare_against": "as_of_execution",
        "catalog": "catalog.yml",
        "authority": {"system": "uc_metrics", "metric_views": [
            entry("main.finance.revenue_mv", "revenue_mv.yaml", net_from),
            entry("main.finance.gross_mv", "gross_mv.yaml", gross_from)]}}), encoding="utf-8")
    models = tmp_path / "models"
    models.mkdir()
    (models / "channel_mart.sql").write_text(MODEL, encoding="utf-8")
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(views),
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


@pytest.mark.parametrize("net_from, gross_from, missing", [
    ("2026-09-01T00:00:00Z", "2026-10-10T00:00:00Z", "gross_revenue"),       # claimed
    ("2026-10-10T00:00:00Z", "2026-09-01T00:00:00Z", "revenue_mv/net_revenue"),  # referenced
])
def test_both_the_referenced_and_the_claimed_definition_must_apply(
        tmp_path, net_from, gross_from, missing):
    result = _two_view_pilot(tmp_path, net_from, gross_from)
    (f,) = result["findings"]
    assert (f["kind"], codes(f)) == ("unverifiable", ["no_definition_evidence"])
    assert missing in f["reasons"][0]["detail"]
    assert f["suspected"]
    headline_consistent(result)


def test_both_definitions_apply_then_the_name_is_judged(tmp_path):
    result = _two_view_pilot(tmp_path, "2026-09-01T00:00:00Z", "2026-09-01T00:00:00Z")
    (f,) = result["findings"]
    assert (f["kind"], f["concept"]) == ("definition_violation", "gross_revenue")
    assert codes(f) == ["presented_under_other_name"]
    headline_consistent(result)
