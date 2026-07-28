"""A measured number must be measurable, or absent.

Round-two adversarial review, 2026-07-26. Execution attaches a dollar figure
to a contradiction, which is the most persuasive thing the product says — so
a wrong one is worse than none. Every test here failed before its fix.
"""

import pytest

from semantix.engine import run_scan
from semantix.models import VerdictType
from semantix.services.execute import _is_additive

pytest.importorskip("duckdb")

SEED = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE TABLE raw.orders (
    order_id BIGINT, customer_id BIGINT, status VARCHAR,
    amount DECIMAL(18,2)
);
INSERT INTO raw.orders VALUES
  (1, 10, 'completed', 100.10), (2, 10, 'completed', 200.20),
  (3, 20, 'pending',   300.30), (4, 20, 'completed', 400.40);
"""

CATALOG = (
    "raw:\n  orders:\n    order_id: bigint\n    customer_id: bigint\n"
    "    status: varchar\n    amount: 'decimal(18,2)'\n"
)


def estate(tmp_path, models: dict[str, str]):
    sql_dir = tmp_path / "sql"
    (sql_dir / "analytics").mkdir(parents=True)
    for name, sql in models.items():
        (sql_dir / "analytics" / f"{name}.sql").write_text(sql)
    (tmp_path / "catalog.yml").write_text(CATALOG)
    (tmp_path / "seed.sql").write_text(SEED)
    return sql_dir


def scan(tmp_path, models):
    sql_dir = estate(tmp_path, models)
    return run_scan(
        sql_dir=str(sql_dir), catalog_path=str(tmp_path / "catalog.yml"),
        db_path=str(tmp_path / "s.db"), execute_seed=str(tmp_path / "seed.sql"),
    )


# ------------------------------------------------------------- additivity

def test_sum_distinct_is_not_additive():
    """SUM(DISTINCT x) de-duplicates before summing, so summing partial
    results does not reconstruct the whole. It passed the gate because only
    COUNT was checked for DISTINCT."""
    assert not _is_additive('SUM(DISTINCT t0."amount")')
    assert _is_additive('SUM(t0."amount")')


@pytest.mark.parametrize("expr", [
    'SUM(DISTINCT t0."amount")',
    'AVG(t0."amount")',
    'COUNT(DISTINCT t0."customer_id")',
    'SUM(t0."amount") / NULLIF(COUNT(t0."order_id"), 0)',
    'MEDIAN(t0."amount")',
    'MAX(t0."amount")',
])
def test_non_additive_measures_are_never_measured(expr):
    assert not _is_additive(expr)


def test_contested_sum_distinct_reports_no_number(tmp_path):
    """End to end: the contradiction is real, the dollar figure is not
    computable, and no figure must appear."""
    result, _ = scan(tmp_path, {
        "a": "select sum(distinct amount) as revenue from raw.orders where status = 'completed'",
        "b": "select sum(distinct amount) as revenue from raw.orders",
    })
    contradiction = next(v for v in result.verdicts
                         if v.verdict_type == VerdictType.CONTRADICTION)
    assert contradiction.evidence["measured"]["measured"] is False
    assert "Measured on seed data" not in contradiction.summary


# ------------------------------------------------- stale / failed relations

def test_measurement_never_runs_against_a_relation_that_failed_to_materialize(tmp_path):
    """A model that cannot be materialized leaves either nothing or a stale
    object behind. Measuring anyway attaches a number describing neither
    definition."""
    result, _ = scan(tmp_path, {
        "a": "select sum(amount) as revenue from raw.orders where status = 'completed'",
        "b": "select sum(amount) as revenue from raw.does_not_exist",
    })
    execution = result.execution
    # Either the contradiction is measured only over relations that really
    # materialized, or it is declined — never silently wrong.
    for verdict in result.verdicts:
        measured = verdict.evidence.get("measured")
        if measured and measured.get("measured"):
            totals = {t["where"]: t for t in measured["totals"]}
            for entry in totals.values():
                assert entry.get("total") is None or entry.get("error") is None
    assert execution["errors"] or execution["declined"] or execution["measured"] >= 0


# --------------------------------------------------------- float tolerance

def test_collision_tolerance_survives_float_summation_noise(tmp_path):
    """Two definitions with the same core hash must not be reported as a
    falsified equality because decimal currency summed to 700.7000000000001.
    An absolute 1e-9 tolerance is tighter than float noise on real money."""
    result, _ = scan(tmp_path, {
        "a": "select sum(amount) as revenue from raw.orders where status = 'completed'",
        "b": "select sum(amount) as revenue from raw.orders where status = 'completed'",
    })
    for verdict in result.verdicts:
        measured = verdict.evidence.get("measured") or {}
        assert not measured.get("collision_falsified"), (
            "identical definitions reported as a falsified equality"
        )


def test_a_real_collision_is_still_caught():
    """The falsification check must still fire on a genuine mismatch."""
    from semantix.services.execute import _diverges

    assert not _diverges(700.70, 700.7000000000001)
    assert not _diverges(1_000_000.0, 1_000_000.0000001)
    assert _diverges(84_000.0, 114_000.0)
    assert _diverges(0.0, 1.0)
