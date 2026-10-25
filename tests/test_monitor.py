"""Query-stream monitoring: the doctrine under test is that similarity
nominates and equality decides — exact core equality is the only claim of
sameness, near misses arrive as candidates with diffs, recurrence gates
exploration, and sub-proof-grade parses never anchor an accusation.

The estate here has no declarations, so the computed system is named
authoritative explicitly (`authority_systems={"sql"}`): authority is
declared by the operator, never inferred from whatever the scan found."""

from __future__ import annotations

import pytest

from semantix.adapters.catalog import load_catalog
from semantix.adapters.sql_dir import load_sql_dir
from semantix.engine import run_scan
from semantix.services.monitor import build_registry, monitor_history

MODEL_SQL = (
    "-- semantix: relation=db.main.rev_mart\n"
    "SELECT SUM(amount - refund_amount) AS net_revenue "
    "FROM db.main.orders WHERE status = 'completed'\n"
)
CATALOG = (
    "db:\n  main:\n    orders:\n      amount: DOUBLE\n"
    "      refund_amount: DOUBLE\n      status: VARCHAR\n"
    "      customer_id: INTEGER\n"
)


@pytest.fixture()
def scanned(tmp_path):
    estate = tmp_path / "estate"
    estate.mkdir()
    (estate / "rev_mart.sql").write_text(MODEL_SQL, encoding="utf-8")
    catalog_path = tmp_path / "catalog.yml"
    catalog_path.write_text(CATALOG, encoding="utf-8")
    _, store = run_scan(sql_dir=str(estate), catalog_path=str(catalog_path),
                        db_path=str(tmp_path / "s.db"))
    catalog = load_catalog(catalog_path)
    artifacts = load_sql_dir(estate, "duckdb")[0]
    registry = build_registry(catalog, artifacts, "duckdb")
    yield store, registry
    store.close()


def _run(store, registry, statements, min_runs=2, authority=frozenset({"sql"})):
    return monitor_history(store, ";\n".join(statements), registry, "duckdb",
                           min_runs=min_runs, authority_systems=set(authority))


def _kinds(result):
    return [f["kind"] for f in result["findings"]]


def test_exact_rerun_is_conforming_use(scanned):
    store, registry = scanned
    q = ("SELECT SUM(amount - refund_amount) AS net_revenue "
         "FROM db.main.orders WHERE status = 'completed'")
    result = _run(store, registry, [q] * 3)
    (f,) = result["findings"]
    assert f["kind"] == "conforming_use"
    assert f["concept"] == "net_revenue"
    assert f["uses"] == 3
    assert f["severity"] == 0


def test_same_name_different_math_is_definition_violation(scanned):
    store, registry = scanned
    q = ("SELECT SUM(amount - refund_amount) AS net_revenue "
         "FROM db.main.orders")  # the filter quietly dropped
    result = _run(store, registry, [q] * 3)
    (f,) = result["findings"]
    assert f["kind"] == "definition_violation"
    assert f["concept"] == "net_revenue"
    assert "filters" in f["diff"]
    assert any("completed" in x for x in f["diff"]["filters"]["reference"])
    assert f["severity"] >= 60


def test_same_math_different_slice_unnamed_is_candidate(scanned):
    store, registry = scanned
    q = ("SELECT SUM(amount - refund_amount) AS rev_check "
         "FROM db.main.orders WHERE status IN ('completed', 'pending')")
    result = _run(store, registry, [q] * 2)
    (f,) = result["findings"]
    assert f["kind"] == "candidate_alt_definition"
    assert f["concept"] == "net_revenue"
    assert "filters" in f["diff"]
    # A hypothesis, ranked below a named violation of equal usage.
    assert f["severity"] < 60 + min(30, f["uses"])


def test_unrelated_recurring_measure_is_unregistered(scanned):
    store, registry = scanned
    q = ("SELECT COUNT(DISTINCT customer_id) AS active_customers "
         "FROM db.main.orders")
    result = _run(store, registry, [q] * 2)
    (f,) = result["findings"]
    assert f["kind"] == "unregistered_recurring_measure"
    assert f["severity"] == 25


def test_recurrence_gates_exploration(scanned):
    store, registry = scanned
    once = "SELECT SUM(refund_amount) AS refunds FROM db.main.orders"
    result = _run(store, registry, [once])
    assert result["findings"] == []
    assert result["below_min_runs"] == 1
    # Raising the threshold silences even a repeated query.
    q = "SELECT SUM(refund_amount) AS refunds FROM db.main.orders"
    result = _run(store, registry, [q] * 2, min_runs=3)
    assert result["findings"] == []


def test_reruns_counted_per_execution_not_per_text(scanned):
    store, registry = scanned
    q = ("SELECT SUM(amount - refund_amount) AS net_revenue "
         "FROM db.main.orders WHERE status = 'completed'")
    result = _run(store, registry, [q, q])
    (f,) = result["findings"]
    assert f["uses"] == 2 and f["distinct_statements"] == 1


def test_disclosure_counts(scanned):
    store, registry = scanned
    statements = [
        "SELECT SUM((amount FROM broken",           # unparseable
        "INSERT INTO t VALUES (1)",                  # non-query
        "SELECT customer_id FROM db.main.orders",    # no measure
    ]
    result = _run(store, registry, statements)
    assert result["skipped_statements"] == 1
    assert result["non_query_statements"] == 1
    assert result["statements_without_measures"] == 1
    assert result["findings"] == []


def test_monitor_writes_nothing(scanned):
    store, registry = scanned
    before_fp = store._conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
    before_scans = store._conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    q = ("SELECT SUM(amount - refund_amount) AS net_revenue "
         "FROM db.main.orders")
    result = _run(store, registry, [q] * 3)
    assert result["recorded"] is False
    assert store._conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0] == before_fp
    assert store._conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == before_scans


def test_monitor_without_scan_refuses(tmp_path):
    from semantix.store import Store

    store = Store(str(tmp_path / "empty.db"))
    registry = build_registry({}, [], "duckdb")
    with pytest.raises(ValueError, match="no scans"):
        monitor_history(store, "SELECT 1", registry, "duckdb")
    store.close()
