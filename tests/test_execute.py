"""Execution slice: contradictions get measured, not just asserted."""

from semantix.engine import run_scan
from semantix.models import VerdictType


def _scan(acme, db, **kw):
    return run_scan(
        dbt_path=str(acme / "dbt_project"),
        metrics_path=str(acme / "metrics.yml"),
        catalog_path=str(acme / "catalog.yml"),
        db_path=str(db),
        **kw,
    )


def test_contradiction_carries_measured_divergence(acme, tmp_path):
    result, store = _scan(acme, tmp_path / "x.db",
                          execute_seed=str(acme / "seed.sql"))
    # CONCEPTS measured, not verdicts: gmv (CERTIFIED + ALIASING) is one
    # measurement, not two — counting verdicts ran the same concept twice and
    # reported `measured: 2` for it.
    assert result.execution["measured"] == 2   # net_revenue + gmv
    assert result.execution["errors"] == []
    # Nothing the engine certified was falsified by the data — the point of
    # measuring certifications at all.
    assert result.execution["collisions"] == 0

    v = next(v for v in result.verdicts if v.verdict_type == VerdictType.CONTRADICTION)
    measured = v.evidence["measured"]
    assert measured["method"] == "duckdb-seed-execution"
    assert measured["baseline"] == "analytics.fct_orders"
    totals = {t["where"]: t["total"] for t in measured["totals"]}
    assert totals["analytics.finance_net_revenue"] == 84_000.0
    assert totals["analytics.growth_net_revenue"] == 114_000.0
    worst = measured["max_divergence"]
    assert worst["vs"] == "analytics.growth_net_revenue"
    assert worst["abs"] == 30_000.0
    assert round(worst["pct"], 1) == 35.7
    assert "114,000 vs baseline 84,000 (+35.7%)" in v.summary

    # Measurement is part of the recorded verdict, not display decoration.
    stored = store.verdicts_for_scan(result.scan_id)
    stored_c = next(r for r in stored if r["verdict"] == "CONTRADICTION")
    assert stored_c["evidence"]["measured"]["max_divergence"]["abs"] == 30_000.0


def test_matching_definitions_are_not_flagged_as_divergent(acme, tmp_path):
    result, _ = _scan(acme, tmp_path / "y.db", execute_seed=str(acme / "seed.sql"))
    v = next(v for v in result.verdicts if v.verdict_type == VerdictType.CONTRADICTION)
    measured = v.evidence["measured"]
    # finance matches the declared core: identical totals, never the "worst".
    assert measured["max_divergence"]["vs"] != "analytics.finance_net_revenue"


def test_broken_seed_degrades_to_error_note(acme, tmp_path):
    bad_seed = tmp_path / "bad.sql"
    bad_seed.write_text("CREATE TABLE syntax error here")
    result, _ = _scan(acme, tmp_path / "z.db", execute_seed=str(bad_seed))
    assert result.execution["measured"] == 0
    assert any("seed failed" in e for e in result.execution["errors"])
    # The scan itself is untouched: verdicts still land, just unmeasured.
    assert any(v.verdict_type == VerdictType.CONTRADICTION for v in result.verdicts)
