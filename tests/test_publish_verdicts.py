"""Verdicts projection: findings as data the estate's own rails gate on.

The properties: one row per verdict per affected relation, wholesale
replacement on republish, refusal to write into a mismatched table, and the
receipts rail untouched beside it.
"""

from __future__ import annotations

import pytest

duckdb = pytest.importorskip("duckdb")

from touchstone.engine import run_scan
from touchstone.services.writeback import publish_receipts

MODEL_SQL = (
    "-- touchstone: relation=db.main.rev_mart\n"
    "SELECT SUM(amount) AS total_revenue FROM db.main.orders\n"
)
METRICS_YML = (
    "version: 1\nmetrics:\n"
    "  - name: total_revenue\n    model: db.main.orders\n"
    "    expr: \"sum(amount)\"\n"
    "  - name: phantom_metric\n    model: db.main.orders\n"
    "    expr: \"sum(ghost_col)\"\n"
)


@pytest.fixture()
def scanned_store(tmp_path):
    estate = tmp_path / "estate"
    estate.mkdir()
    (estate / "rev_mart.sql").write_text(MODEL_SQL, encoding="utf-8")
    (tmp_path / "metrics.yml").write_text(METRICS_YML, encoding="utf-8")
    (tmp_path / "catalog.yml").write_text(
        "db:\n  main:\n    orders:\n      amount: DOUBLE\n      ghost_col: DOUBLE\n",
        encoding="utf-8")
    _, store = run_scan(sql_dir=str(estate),
                        metrics_path=str(tmp_path / "metrics.yml"),
                        catalog_path=str(tmp_path / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"))
    yield store
    store.close()


def _rows(wh_path, query):
    con = duckdb.connect(str(wh_path))
    try:
        return con.execute(query).fetchall()
    finally:
        con.close()


def test_verdicts_projected_per_relation(scanned_store, tmp_path):
    wh = tmp_path / "wh.duckdb"
    summary = publish_receipts(scanned_store, str(wh))
    assert summary["verdict_rows"] > 0

    rows = _rows(wh, "SELECT concept, verdict, relation, scan_id, "
                     "CAST(scanned_at AS VARCHAR) "
                     "FROM touchstone.verdicts ORDER BY concept, verdict")
    assert rows, "projection wrote no rows"
    concepts = {r[0] for r in rows}
    assert "total_revenue" in concepts
    # Every row carries the scan identity and a real timestamp.
    assert all(r[3] == summary["scan_id"] and r[4] is not None for r in rows)
    # total_revenue touches both the mart (computed) and the declared model.
    total_rels = {r[2] for r in rows if r[0] == "total_revenue"}
    assert "db.main.rev_mart" in total_rels and "db.main.orders" in total_rels
    # Receipts rail still beside it.
    root = _rows(wh, "SELECT count(*) FROM agents.root WHERE provider = 'touchstone'")
    assert root[0][0] > 0


def test_republish_replaces_wholesale(scanned_store, tmp_path):
    wh = tmp_path / "wh.duckdb"
    publish_receipts(scanned_store, str(wh))
    first = _rows(wh, "SELECT count(*) FROM touchstone.verdicts")[0][0]
    publish_receipts(scanned_store, str(wh))
    second = _rows(wh, "SELECT count(*) FROM touchstone.verdicts")[0][0]
    assert first == second  # projection, not an append log


def test_mismatched_verdicts_table_refused(scanned_store, tmp_path):
    wh = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(wh))
    con.execute("CREATE SCHEMA touchstone")
    con.execute("CREATE TABLE touchstone.verdicts (something_else VARCHAR)")
    con.close()
    with pytest.raises(ValueError, match="missing column"):
        publish_receipts(scanned_store, str(wh))


def test_dbt_package_tests_query_the_projection(tmp_path):
    """The shipped generic tests must reference the projection through the
    findings() macro and carry the three test names the README promises."""
    from pathlib import Path

    pkg = Path(__file__).resolve().parent.parent / "integrations" / "dbt" / "touchstone"
    tests_sql = (pkg / "macros" / "tests.sql").read_text(encoding="utf-8")
    findings_sql = (pkg / "macros" / "findings.sql").read_text(encoding="utf-8")
    for name in ("no_contradictions", "certified", "fresh_scan"):
        assert f"test {name}" in tests_sql
    assert "touchstone.findings()" in tests_sql
    assert "touchstone_schema" in findings_sql
    assert (pkg / "dbt_project.yml").exists()
