"""Measurement must be meaningful or absent — never fabricated.

Divergence is computed by SUMming each definition's output column, which only
means something for an additive measure. Summing a ratio produces a number
with no interpretation, and the old code printed it as "Measured on seed
data", manufacturing exactly the false confidence this product exists to
prevent. Found by adversarial review, 2026-07-25.
"""

import pytest

from semantix.engine import run_scan
from semantix.models import VerdictType
from semantix.services.execute import _is_additive

from .policy import engine_verdict

pytest.importorskip("duckdb")

SEED = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE TABLE raw.orders (
    order_id BIGINT, status VARCHAR,
    amount DECIMAL(18,2), refund_amount DECIMAL(18,2)
);
INSERT INTO raw.orders VALUES
  (1, 'completed', 100, 10), (2, 'completed', 200, 0),
  (3, 'pending', 300, 30), (4, 'completed', 400, 40);
"""

CATALOG = (
    "raw:\n  orders:\n    order_id: bigint\n    status: varchar\n"
    "    amount: 'decimal(18,2)'\n    refund_amount: 'decimal(18,2)'\n"
)


def estate(tmp_path, sql_by_name: dict[str, str]):
    sql_dir = tmp_path / "sql"
    (sql_dir / "analytics").mkdir(parents=True)
    for name, sql in sql_by_name.items():
        (sql_dir / "analytics" / f"{name}.sql").write_text(sql)
    (tmp_path / "catalog.yml").write_text(CATALOG)
    (tmp_path / "seed.sql").write_text(SEED)
    return sql_dir


def test_is_additive_classification():
    assert _is_additive('SUM(t0."amount")')
    assert _is_additive('COUNT(t0."order_id")')
    assert not _is_additive('COUNT(DISTINCT t0."order_id")')
    assert not _is_additive('SUM(t0."refund_amount") / NULLIF(SUM(t0."amount"), 0)')
    assert not _is_additive('AVG(t0."amount")')
    assert not _is_additive("not parseable ((")


def test_contested_ratio_is_not_measured(tmp_path):
    """Two disagreeing definitions of a RATE. The contradiction is real and
    must be reported; the dollar figure is not computable and must be absent."""
    sql_dir = estate(tmp_path, {
        "finance_rate": "select sum(refund_amount) / nullif(sum(amount), 0) as refund_rate "
                        "from raw.orders where status = 'completed'",
        "growth_rate": "select sum(refund_amount) / nullif(sum(amount), 0) as refund_rate "
                       "from raw.orders",
    })
    result, _ = run_scan(sql_dir=str(sql_dir), catalog_path=str(tmp_path / "catalog.yml"),
                         db_path=str(tmp_path / "s.db"), execute_seed=str(tmp_path / "seed.sql"))

    # The engine's CONTRADICTION surfaces as a suspected divergence under the
    # conclusions policy; the measurement evidence travels with it.
    contradiction = next(v for v in result.verdicts
                         if engine_verdict(v) == VerdictType.CONTRADICTION)
    assert contradiction.verdict_type == VerdictType.SUSPECTED_DIVERGENCE
    assert contradiction.evidence["measured"]["measured"] is False
    assert "not additive" in contradiction.evidence["measured"]["reason"]
    # crucially: no fabricated number reaches the human-readable summary
    # (the policy renamed "Measured on seed data" to "Observed discrepancy on
    # seed data"; neither wording may appear)
    assert "Measured on seed data" not in contradiction.summary
    assert "Observed discrepancy on seed data" not in contradiction.summary
    assert result.execution["declined"]
    assert result.execution["measured"] == 0


def test_contested_additive_measure_is_still_measured(tmp_path):
    """The gate must not suppress legitimate measurement."""
    sql_dir = estate(tmp_path, {
        "finance_rev": "select sum(amount - refund_amount) as revenue from raw.orders "
                       "where status = 'completed'",
        "growth_rev": "select sum(amount) as revenue from raw.orders",
    })
    result, _ = run_scan(sql_dir=str(sql_dir), catalog_path=str(tmp_path / "catalog.yml"),
                         db_path=str(tmp_path / "s.db"), execute_seed=str(tmp_path / "seed.sql"))

    # The engine's CONTRADICTION surfaces as a suspected divergence under the
    # conclusions policy; the measurement evidence travels with it.
    contradiction = next(v for v in result.verdicts
                         if engine_verdict(v) == VerdictType.CONTRADICTION)
    assert contradiction.verdict_type == VerdictType.SUSPECTED_DIVERGENCE
    measured = contradiction.evidence["measured"]
    assert measured["measured"] is True
    assert measured["max_divergence"]["abs"] == pytest.approx(350.0)  # 1000 vs 650
    # (the policy renamed "Measured on seed data" to "Observed discrepancy on
    # seed data")
    assert "Observed discrepancy on seed data" in contradiction.summary
    assert result.execution["measured"] == 1
