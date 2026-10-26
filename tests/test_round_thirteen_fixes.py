"""Round thirteen: the raw-table boundary is complete before any judgment.

Round twelve moved the boundary ahead of most violations, but two
branches still returned first:

* a condition touching a REQUIRED-FILTER column (`UPPER(status) =
  'COMPLETED'`, or `UPPER(channel)` inside an OR replacing the required
  filter) was handed to the required-filter logic as "related" and never
  syntax-checked, so its unresolved implication lost to a measure
  violation;
* a changed SOURCE returned before filters and groupings were examined,
  so `orders_archive … GROUP BY ROLLUP(channel)` was a source violation
  while the same query over `orders` was correctly unverifiable.

check_raw now runs a complete boundary pass — shape, join graph, the
syntax of every condition whatever columns it touches, dimensions of the
rest, every grouping term — before any violation is judged. This file
crosses each violation kind with each boundary condition.
"""

from __future__ import annotations

import pytest

from semantix.adapters.catalog import load_catalog
from semantix.adapters.sql_dir import load_sql_dir
from semantix.engine import run_scan
from semantix.services.history import read_history
from semantix.services.monitor import build_registry, monitor_history
from semantix.services.pilot import load_pilot

from .test_monitor_conformance import CATALOG, GOVERNED, _rows, codes, make_pilot
from .test_round_ten_fixes import headline_consistent

D = "databricks"
ARCHIVE = CATALOG + "".join(f"\n      {c}: {t}" for c, t in (
    ("amount", "DOUBLE"), ("refund_amount", "DOUBLE"), ("discount", "DOUBLE"),
    ("status", "STRING"), ("country", "STRING"), ("channel", "STRING"),
    ("order_date", "DATE"))).replace("\n      amount", "\n    orders_archive:\n      amount", 1) + "\n"
REQ = "status = 'completed'"
BOUNDARY_CODES = {"predicate_unsupported", "shape_unsupported", "grouping_undeclared",
                  "dimension_unsupported", "dimensions_not_modelled"}


def run(tmp_path, sql):
    root, models = make_pilot(tmp_path, _rows(sql))
    (root / "catalog.yml").write_text(ARCHIVE, encoding="utf-8")
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


# Each violation kind: (measure, table, conditions), and the code it gives alone.
VIOLATIONS = {
    "source":      (GOVERNED, "orders_archive", [REQ], "source_changed"),
    "measure":     ("SUM(amount)", "orders", [REQ], "measure_changed"),
    "missing":     (GOVERNED, "orders", [], "required_filter_missing"),
    "or_weakened": (GOVERNED, "orders", [f"({REQ} OR channel = 'web')"],
                    "required_filter_not_preserved"),
    "broadened":   (GOVERNED, "orders", ["status IN ('completed', 'pending')"],
                    "required_filter_not_preserved"),
    "replaced":    (GOVERNED, "orders", ["status = 'pending'"],
                    "required_filter_not_preserved"),
}
# Each boundary condition: (extra condition | None, GROUP BY | None, LIMIT tail).
BOUNDARIES = {
    "upper_required_col": ("UPPER(status) = 'COMPLETED'", None, ""),
    "upper_dimension":    ("UPPER(channel) = 'WEB'", None, ""),
    "dimension_eq_dim":   ("channel = country", None, ""),
    "rollup":             (None, "ROLLUP (channel)", ""),
    "undeclared_group":   (None, "discount", ""),
    "limit":              (None, None, " LIMIT 5"),
}


def build(violation: str, boundary: str | None) -> str:
    measure, table, conds, _ = VIOLATIONS[violation]
    extra, group, tail = BOUNDARIES[boundary] if boundary else (None, None, "")
    conds = conds + ([extra] if extra else [])
    select = ("channel, " if group == "ROLLUP (channel)" else
              "discount, " if group == "discount" else "")
    sql = f"SELECT {select}{measure} AS net_revenue FROM main.finance.{table}"
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    if group:
        sql += f" GROUP BY {group}"
    return sql + tail


@pytest.mark.parametrize("violation", sorted(VIOLATIONS))
def test_each_violation_alone_stands(tmp_path, violation):
    result = run(tmp_path, build(violation, None))
    (f,) = result["findings"]
    assert f["kind"] == "definition_violation"
    assert VIOLATIONS[violation][3] in codes(f)
    assert result["funnel"]["violations"] == 1
    headline_consistent(result)


@pytest.mark.parametrize("boundary", sorted(BOUNDARIES))
@pytest.mark.parametrize("violation", sorted(VIOLATIONS))
def test_violation_outside_the_boundary_is_unverifiable(tmp_path, violation, boundary):
    result = run(tmp_path, build(violation, boundary))
    (f,) = result["findings"]
    assert f["kind"] == "unverifiable", (build(violation, boundary), f)
    assert set(codes(f)) & BOUNDARY_CODES
    assert result["funnel"]["violations"] == 0
    # The would-be violation is kept as context, except where it is not even
    # decidable: an unsupported condition on the required filter's own column
    # may BE that filter, and one on `channel` constrains the other branch of
    # the OR that weakened it. Then there is nothing to suspect.
    # A condition on the required column itself may also restore what an OR
    # weakened (sweep 2, #39).
    undecidable = ((boundary == "upper_required_col"
                    and violation in ("missing", "broadened", "replaced", "or_weakened"))
                   or (violation == "or_weakened"
                       and boundary in ("upper_dimension", "dimension_eq_dim")))
    if not undecidable:
        assert f["suspected"], f
    headline_consistent(result)


def test_unsupported_condition_inside_an_or_replacing_the_required_filter(tmp_path):
    sql = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
           f"WHERE {REQ} OR UPPER(channel) = 'WEB'")
    result = run(tmp_path, sql)
    (f,) = result["findings"]
    assert f["kind"] == "unverifiable" and "predicate_unsupported" in codes(f)
    assert result["funnel"]["violations"] == 0
    headline_consistent(result)


def test_unsupported_condition_on_a_required_column_alone(tmp_path):
    sql = "SELECT SUM(amount) AS net_revenue FROM main.finance.orders WHERE UPPER(status) = 'COMPLETED'"
    result = run(tmp_path, sql)
    (f,) = result["findings"]
    assert f["kind"] == "unverifiable" and "predicate_unsupported" in codes(f)
    assert any("measure is" in s for s in f["suspected"])
    assert result["funnel"]["violations"] == 0
    headline_consistent(result)


def test_supported_slice_on_a_changed_source_is_still_a_source_violation(tmp_path):
    sql = (f"SELECT channel, {GOVERNED} AS net_revenue FROM main.finance.orders_archive "
           f"WHERE {REQ} AND country = 'US' GROUP BY channel")
    result = run(tmp_path, sql)
    (f,) = result["findings"]
    assert f["kind"] == "definition_violation" and codes(f) == ["source_changed"]
    headline_consistent(result)
