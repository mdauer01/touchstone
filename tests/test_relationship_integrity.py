"""An unverified join is not a verified one.

Round-two adversarial review, 2026-07-26. The relationship layer's whole
claim is that it CHECKS joins instead of asserting them, so every path where
an unchecked or unproven edge reads as certified is the product failing at
the one thing that distinguishes it.
"""

import pytest

from semantix.cli import main
from semantix.extraction.registry import SchemaRegistry
from semantix.services.relationships import (
    graph_with_status,
    mine_sql_text,
    record_declared,
    relationships_for_agent,
    verify_edges,
)
from semantix.store import Store
from semantix.warehouse import DuckDBWarehouse

pytest.importorskip("duckdb")

CATALOG = {
    "raw.orders": {"order_id": "bigint", "customer_id": "bigint"},
    "raw.customers": {"customer_id": "bigint"},
}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "rel.db")


@pytest.fixture
def registry():
    return SchemaRegistry(CATALOG)


def mine(sql, registry):
    return mine_sql_text(sql, registry, "duckdb")[0]


def wh(seed, label="test"):
    return DuckDBWarehouse.ephemeral(seed, label=label)


GOOD = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE TABLE raw.orders (order_id BIGINT, customer_id BIGINT);
INSERT INTO raw.orders VALUES (1, 10), (2, 10), (3, 20);
CREATE TABLE raw.customers (customer_id BIGINT);
INSERT INTO raw.customers VALUES (10), (20);
"""

MOSTLY_NULL = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE TABLE raw.orders (order_id BIGINT, customer_id BIGINT);
INSERT INTO raw.orders
  SELECT i, CASE WHEN i = 1 THEN 10 ELSE NULL END FROM range(1, 1001) t(i);
CREATE TABLE raw.customers (customer_id BIGINT);
INSERT INTO raw.customers VALUES (10);
"""


def test_a_single_non_null_row_does_not_prove_a_key(store, registry):
    """1000 orders, one non-NULL customer_id. `COUNT == COUNT(DISTINCT)` is
    trivially true over one value and demonstrates nothing about the key —
    and joining through it silently drops 99.9% of the rows."""
    warehouse = wh(MOSTLY_NULL, "sparse")
    try:
        record_declared(store, [("raw.orders.customer_id", "raw.customers.customer_id")], "c.yml")
        row = verify_edges(store, warehouse)[0]
        # Not certified — and not `failed` either: nothing was demonstrated
        # about this key in EITHER direction, so calling it broken would be
        # an accusation the data does not support.
        assert row["status"] == "unverified"
        evidence = row["check"]["evidence"]
        assert evidence["passed"] is False        # a stale CERTIFIED cannot survive
        assert evidence["inconclusive"] is True
        assert "reason" in evidence
        assert evidence["left"]["null_fraction"] > 0.9 or \
               evidence["right"]["null_fraction"] > 0.9
    finally:
        warehouse.close()


def test_a_genuinely_sound_key_still_certifies(store, registry):
    """The floor must not make real relationships uncertifiable."""
    warehouse = wh(GOOD, "good")
    try:
        record_declared(store, [("raw.orders.customer_id", "raw.customers.customer_id")], "c.yml")
        row = verify_edges(store, warehouse)[0]
        assert row["status"] == "checks_passed_on_observed_data"
        assert row["check"]["evidence"]["cardinality"] == "1:N"
    finally:
        warehouse.close()


def test_an_unrunnable_recheck_does_not_leave_a_stale_certification(store, registry):
    """Verified once against a good warehouse, then re-checked against one
    where the table is gone. The agent view must stop saying certified."""
    good = wh(GOOD, "good")
    try:
        record_declared(store, [("raw.orders.customer_id", "raw.customers.customer_id")], "c.yml")
        assert verify_edges(store, good)[0]["status"] == "checks_passed_on_observed_data"
    finally:
        good.close()

    gone = wh("CREATE SCHEMA IF NOT EXISTS raw;", "gone")
    try:
        verify_edges(store, gone)
    finally:
        gone.close()

    row = graph_with_status(store)[0]
    assert row["status"] != "checks_passed_on_observed_data", "a failed re-check left the old verdict standing"
    view = relationships_for_agent(store)
    assert not view["checks_passed_on_observed_data"], "agent still told to join through an unverified edge"


def test_cli_never_counts_unverified_edges_as_certified(store, tmp_path, capsys, acme):
    """The summary line reported `len(results) - len(failed)` as certified,
    silently counting every edge it could not check."""
    db = str(tmp_path / "cli.db")
    s = Store(db)
    record_declared(s, [("raw.ghost.id", "raw.orders.order_id")], "c.yml")
    seed = tmp_path / "seed.sql"
    seed.write_text(GOOD)

    assert main(["verify-relationships", "--seed", str(seed), "--db", db]) == 0
    out = capsys.readouterr().out
    assert "0 checks passed on observed data" in out
    assert "unverified" in out.lower()


def test_schema_names_are_quoted_when_building_an_ephemeral_warehouse():
    """CREATE SCHEMA used a hand-rolled quote instead of qident, so a schema
    needing escaping was silently skipped and every model in it failed."""
    from semantix.adapters.base import ComputedArtifact

    artifact = ComputedArtifact(
        system="sql", object_id="m", relation="raw.orders_v",
        sql="select order_id from raw.orders",
    )
    warehouse = DuckDBWarehouse.ephemeral(GOOD, [artifact], label="quoted")
    try:
        assert warehouse.query("SELECT COUNT(*) FROM raw.orders_v")[0][0] == 3
    finally:
        warehouse.close()
