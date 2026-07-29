"""Relationship layer: mined usage is a claim; the warehouse decides validity."""

import json

import pytest

from semantix.adapters.catalog import (
    load_catalog,
    load_declared_relationships,
    load_primary_keys,
)
from semantix.cli import main
from semantix.extraction.registry import SchemaRegistry
from semantix.services.relationships import (
    Edge,
    graph_with_status,
    mine_sql_text,
    record_declared,
    record_mined,
    relationships_for_agent,
    verify_edges,
)
from semantix.store import Store
from semantix.warehouse import DuckDBWarehouse, UnsupportedWarehouseError, connect_warehouse

pytest.importorskip("duckdb")

CATALOG = {
    "raw.orders": {"order_id": "bigint", "customer_id": "bigint", "channel": "varchar"},
    "raw.customers": {"customer_id": "bigint", "segment": "varchar"},
    "raw.payments": {"payment_id": "bigint", "order_id": "bigint", "method": "varchar"},
}

SEED = """
CREATE SCHEMA IF NOT EXISTS raw;
CREATE TABLE raw.orders (order_id BIGINT, customer_id BIGINT, channel VARCHAR);
INSERT INTO raw.orders VALUES (1, 1, 'web'), (2, 1, 'web'), (3, 2, 'web');
CREATE TABLE raw.customers (customer_id BIGINT, segment VARCHAR);
INSERT INTO raw.customers VALUES (1, 'consumer'), (2, 'smb');
CREATE TABLE raw.payments (payment_id BIGINT, order_id BIGINT, method VARCHAR);
INSERT INTO raw.payments VALUES (10, 1, 'card'), (11, 1, 'card'), (12, 99, 'ach');
"""


@pytest.fixture
def registry():
    return SchemaRegistry(CATALOG)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "rel.db")


@pytest.fixture
def warehouse():
    wh = DuckDBWarehouse.ephemeral(SEED, label="test")
    yield wh
    wh.close()


def mine(sql, registry):
    return mine_sql_text(sql, registry, "duckdb")[0]


# ------------------------------------------------------------ edge identity


def test_edge_identity_is_direction_and_alias_free():
    a = Edge.make("raw.orders", "customer_id", "raw.customers", "customer_id")
    b = Edge.make("raw.customers", "customer_id", "raw.orders", "customer_id")
    assert a == b and a.edge_id == b.edge_id

    different = Edge.make("raw.orders", "order_id", "raw.customers", "customer_id")
    assert different.edge_id != a.edge_id


def test_edge_from_qualified_handles_multipart_and_garbage():
    edge = Edge.from_qualified("raw.orders.customer_id", "raw.customers.customer_id")
    assert edge.left_relation == "raw.customers"  # sorted endpoints
    assert Edge.from_qualified("nodots", "raw.customers.customer_id") is None


# ------------------------------------------------------------------ mining


def test_mining_collapses_authoring_styles(registry):
    sql = """
    SELECT 1 FROM raw.orders o JOIN raw.customers c ON o.customer_id = c.customer_id;
    SELECT 1 FROM raw.orders o JOIN raw.customers c ON c.customer_id = o.customer_id;
    SELECT 1 FROM raw.orders AS ord, raw.customers AS cust
     WHERE ord.customer_id = cust.customer_id;
    """
    mined = mine(sql, registry)
    assert len(mined) == 1
    assert next(iter(mined.values()))["queries"] == 3


def test_mining_skips_unparseable_and_self_joins(registry):
    sql = """
    SELECT * FROM WHERE;
    SELECT 1 FROM raw.orders a JOIN raw.orders b ON a.order_id = b.order_id;
    SELECT 1 FROM raw.orders o JOIN raw.customers c ON o.customer_id = c.customer_id;
    """
    mined, meta = mine_sql_text(sql, registry, "duckdb")
    assert meta["skipped_statements"] == 1
    assert len(mined) == 1  # self-join produced no edge


def test_mining_ignores_non_column_predicates(registry):
    sql = """SELECT 1 FROM raw.orders o JOIN raw.customers c
             ON o.customer_id = c.customer_id AND o.channel = 'web'"""
    mined = mine(sql, registry)
    assert len(mined) == 1  # the literal predicate is not an edge


# ---------------------------------------------------------- classification


def test_usage_axis_is_independent_of_validity(store, registry):
    mined = mine(
        "SELECT 1 FROM raw.orders o JOIN raw.customers c ON o.customer_id = c.customer_id",
        registry,
    )
    record_mined(store, mined, "history.sql")
    record_declared(store, [("raw.payments.order_id", "raw.orders.order_id")], "catalog.yml")

    rows = {r["label"]: r for r in graph_with_status(store)}
    used = rows["raw.customers.customer_id = raw.orders.customer_id"]
    declared_only = rows["raw.orders.order_id = raw.payments.order_id"]

    assert (used["status"], used["usage"]) == ("unverified", "shadow")
    assert (declared_only["status"], declared_only["usage"]) == ("unverified", "orphan")


def test_declared_and_mined_edge_is_declared_plus_used(store, registry):
    mined = mine(
        "SELECT 1 FROM raw.orders o JOIN raw.customers c ON o.customer_id = c.customer_id",
        registry,
    )
    record_mined(store, mined, "history.sql")
    record_declared(store, [("raw.orders.customer_id", "raw.customers.customer_id")], "cat.yml")
    assert graph_with_status(store)[0]["usage"] == "declared+used"


# ----------------------------------------------------------- verification


def _verified(store, registry, sql, warehouse):
    record_mined(store, mine(sql, registry), "history.sql")
    return {r["label"]: r for r in verify_edges(store, warehouse)}


def test_valid_edge_is_certified_with_measured_cardinality(store, registry, warehouse):
    rows = _verified(
        store, registry,
        "SELECT 1 FROM raw.orders o JOIN raw.customers c ON o.customer_id = c.customer_id",
        warehouse,
    )
    row = rows["raw.customers.customer_id = raw.orders.customer_id"]
    assert row["status"] == "certified"
    ev = row["check"]["evidence"]
    assert ev["cardinality"] == "1:N"
    assert ev["parent"] == "raw.customers.customer_id"
    assert ev["left"]["unique"] is True and ev["right"]["unique"] is False


def test_orphans_fail_the_edge_with_counts(store, registry, warehouse):
    rows = _verified(
        store, registry,
        "SELECT 1 FROM raw.payments p JOIN raw.orders o ON p.order_id = o.order_id",
        warehouse,
    )
    row = rows["raw.orders.order_id = raw.payments.order_id"]
    assert row["status"] == "failed"
    assert "1 orphan" in row["check"]["evidence"]["reason"]


def test_n_to_m_edge_fails_on_cardinality(store, registry, warehouse):
    rows = _verified(
        store, registry,
        "SELECT 1 FROM raw.payments p JOIN raw.orders o ON p.method = o.channel",
        warehouse,
    )
    row = rows["raw.orders.channel = raw.payments.method"]
    assert row["status"] == "failed"
    assert row["check"]["evidence"]["cardinality"] == "N:M"
    assert row["check"]["evidence"]["parent"] is None


def test_declared_primary_key_that_is_not_unique_fails_the_edge(store, registry, warehouse):
    """The catalog claims payments.order_id is the PK; the warehouse disagrees
    (order 1 paid twice). A key-level contradiction, caught even though the
    edge's other side is unique."""
    record_mined(
        store,
        mine("SELECT 1 FROM raw.payments p JOIN raw.orders o ON p.order_id = o.order_id",
             registry),
        "history.sql",
    )
    rows = verify_edges(store, warehouse,
                        declared_pks={"raw.payments": ["order_id"]})
    evidence = rows[0]["check"]["evidence"]
    assert rows[0]["status"] == "failed"
    assert evidence["pk_violations"] == [
        "declared primary key raw.payments.order_id is NOT unique"
    ]
    assert "NOT unique" in evidence["reason"]


def test_valid_declared_primary_key_does_not_trip_the_check(store, registry, warehouse):
    record_mined(
        store,
        mine("SELECT 1 FROM raw.orders o JOIN raw.customers c ON o.customer_id = c.customer_id",
             registry),
        "history.sql",
    )
    rows = verify_edges(store, warehouse, declared_pks={"raw.customers": ["customer_id"]})
    assert rows[0]["status"] == "certified"
    assert "pk_violations" not in rows[0]["check"]["evidence"]


def test_mining_from_a_warehouse_query_log(tmp_path, capsys):
    """The real intake shape: history lives in a warehouse table, not a file."""
    import duckdb

    wh_path = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(wh_path))
    con.execute(SEED)
    con.execute("CREATE SCHEMA meta; CREATE TABLE meta.query_history (query_text VARCHAR)")
    con.execute(
        "INSERT INTO meta.query_history VALUES "
        "('SELECT 1 FROM raw.orders o JOIN raw.customers c ON o.customer_id = c.customer_id'),"
        "('SELECT 1 FROM raw.payments p JOIN raw.orders o ON p.order_id = o.order_id')"
    )
    con.close()

    db = str(tmp_path / "wh-rel.db")
    assert main(["mine-relationships", "--warehouse", str(wh_path),
                 "--history-query", "SELECT query_text FROM meta.query_history",
                 "--db", db]) == 0
    assert "mined 2 edge(s)" in capsys.readouterr().out

    assert main(["verify-relationships", "--warehouse", str(wh_path), "--db", db]) == 0
    assert "1 certified, 1 failed" in capsys.readouterr().out


def test_missing_relation_stays_unverified_not_failed(store, registry, warehouse):
    """"Could not check" is not "checked and found broken". Recording an
    unrunnable check as FAILED libels a relationship that may be perfectly
    sound, and pollutes the store with a verdict nothing supports."""
    record_declared(store, [("raw.ghost.id", "raw.orders.order_id")], "cat.yml")
    rows = {r["label"]: r for r in verify_edges(store, warehouse)}
    row = rows["raw.ghost.id = raw.orders.order_id"]
    assert row["status"] == "unverified"
    assert row["check"] is None
    assert "error" in row["unrunnable"]
    assert row["unrunnable"].get("cardinality") is None  # no fabricated verdict
    # The attempt IS recorded, so it supersedes any earlier CERTIFIED — an
    # edge the warehouse can no longer answer for must stop reading as
    # verified — but it surfaces as `unverified`, never as `failed`.
    stored = store._conn.execute(
        "SELECT evidence_json FROM relationship_checks"
    ).fetchall()
    assert len(stored) == 1
    assert '"unrunnable": true' in stored[0]["evidence_json"]
    assert graph_with_status(store)[0]["status"] == "unverified"


def test_empty_table_is_not_certified_as_a_key(store, registry, tmp_path):
    """COUNT ignores NULLs, so 0 == 0 once "proved" a unique key with zero
    orphans — certifying a relationship on no evidence whatsoever."""
    empty = DuckDBWarehouse.ephemeral(
        """
        CREATE SCHEMA IF NOT EXISTS raw;
        CREATE TABLE raw.orders (order_id BIGINT, customer_id BIGINT);
        CREATE TABLE raw.customers (customer_id BIGINT);
        """,
        label="empty",
    )
    try:
        record_declared(store, [("raw.orders.customer_id", "raw.customers.customer_id")], "c.yml")
        row = verify_edges(store, empty)[0]
        assert row["status"] == "unverified"   # no evidence is not evidence of a fault
        evidence = row["check"]["evidence"]
        assert evidence["passed"] is False
        assert evidence["inconclusive"] is True
        assert "nothing was demonstrated" in evidence["reason"]
        assert evidence["matched_rows"] == 0
    finally:
        empty.close()


def test_all_null_join_column_is_not_certified(store, registry, tmp_path):
    populated = DuckDBWarehouse.ephemeral(
        """
        CREATE SCHEMA IF NOT EXISTS raw;
        CREATE TABLE raw.orders (order_id BIGINT, customer_id BIGINT);
        INSERT INTO raw.orders VALUES (1, NULL), (2, NULL);
        CREATE TABLE raw.customers (customer_id BIGINT);
        INSERT INTO raw.customers VALUES (1), (2);
        """,
        label="nulls",
    )
    try:
        record_declared(store, [("raw.orders.customer_id", "raw.customers.customer_id")], "c.yml")
        row = verify_edges(store, populated)[0]
        assert row["status"] == "unverified"
        assert row["check"]["evidence"]["passed"] is False
        assert "no non-null values" in row["check"]["evidence"]["reason"]
    finally:
        populated.close()


def test_cte_names_are_not_mined_as_physical_relations(store, registry):
    """A CTE alias is not a table. Mining it as one yields an edge naming a
    relation that may not exist — or a real table that merely shares the
    name, which verification would then certify while describing a join that
    never happens."""
    mined = mine(
        """with customers as (select customer_id from raw.orders)
           select 1 from raw.orders o join customers c on o.customer_id = c.customer_id""",
        registry,
    )
    assert mined == {}


def test_or_branch_equalities_are_not_join_edges(registry):
    """An equality inside an OR is not an assertion that the tables join on
    those columns."""
    mined = mine(
        "select 1 from raw.orders o, raw.customers c "
        "where o.customer_id = c.customer_id or o.order_id = c.customer_id",
        registry,
    )
    assert mined == {}


def test_identifier_quotes_cannot_break_out(store, warehouse):
    """Identifiers arrive from estate SQL and catalog YAML — files we do not
    control — and are interpolated into checks run against a customer
    warehouse."""
    from semantix.services.relationships import qcol
    from semantix.warehouse import qident

    assert qident('raw.or"ders') == '"raw"."or""ders"'
    assert qcol('id" ; DROP TABLE x --') == '"id"" ; DROP TABLE x --"'
    # and an injected identifier fails the check rather than executing
    record_declared(store, [('raw.orders."; DROP TABLE raw.orders; --', "raw.orders.order_id")],
                    "evil.yml")
    row = verify_edges(store, warehouse)[0]
    assert row["status"] in ("unverified", "failed")
    assert warehouse.query("SELECT COUNT(*) FROM raw.orders")[0][0] == 3  # still there


def test_usage_ranks_verification_but_never_certifies(store, registry, warehouse):
    sql = """
    SELECT 1 FROM raw.payments p JOIN raw.orders o ON p.method = o.channel;
    SELECT 1 FROM raw.payments p JOIN raw.orders o ON p.method = o.channel;
    SELECT 1 FROM raw.orders o JOIN raw.customers c ON o.customer_id = c.customer_id;
    """
    record_mined(store, mine(sql, registry), "history.sql")
    # The junk join is the MOST used, so it is verified first...
    checked = verify_edges(store, warehouse, top=1)
    assert checked[0]["mined_queries"] == 2
    # ...and popularity still buys it nothing.
    assert checked[0]["status"] == "failed"


def test_latest_check_wins_and_history_is_append_only(store, registry, warehouse):
    _verified(
        store, registry,
        "SELECT 1 FROM raw.payments p JOIN raw.orders o ON p.order_id = o.order_id",
        warehouse,
    )
    verify_edges(store, warehouse)
    assert store._conn.execute("SELECT COUNT(*) FROM relationship_checks").fetchone()[0] == 2
    assert len(graph_with_status(store)) == 1  # one edge, latest check


# --------------------------------------------------------------- surfaces


def test_agent_view_separates_certified_from_hypotheses(store, registry, warehouse):
    _verified(
        store, registry,
        """SELECT 1 FROM raw.orders o JOIN raw.customers c ON o.customer_id = c.customer_id;
           SELECT 1 FROM raw.payments p JOIN raw.orders o ON p.order_id = o.order_id;""",
        warehouse,
    )
    view = relationships_for_agent(store)
    assert [e["join"] for e in view["certified"]] == [
        "raw.customers.customer_id = raw.orders.customer_id"
    ]
    assert view["failed"][0]["cardinality"] == "1:N"
    assert "hypothesis" in view["guidance"]

    scoped = relationships_for_agent(store, table="raw.payments")
    assert not scoped["certified"] and len(scoped["failed"]) == 1


def test_catalog_reserved_sections_are_not_schemas(acme):
    catalog = load_catalog(acme / "catalog.yml")
    assert not any(rel.startswith(("primary_keys", "relationships")) for rel in catalog)
    assert "raw.channels" in catalog
    assert load_primary_keys(acme / "catalog.yml")["raw.orders"] == ["order_id"]
    assert len(load_declared_relationships(acme / "catalog.yml")) == 2


def test_unsupported_warehouse_names_the_seam():
    with pytest.raises(UnsupportedWarehouseError, match="no snowflake:// driver"):
        connect_warehouse("snowflake://acct/db")


# ------------------------------------------------------------------- CLI


def test_cli_mine_verify_relationships_roundtrip(acme, tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    assert main(["mine-relationships", "--history-file", str(acme / "query_history.sql"),
                 "--catalog", str(acme / "catalog.yml"), "--db", db]) == 0
    out = capsys.readouterr().out
    assert "1 unparseable, skipped" in out
    assert "UNVERIFIED" in out and "orphan" in out

    assert main(["verify-relationships", "--seed", str(acme / "seed.sql"),
                 "--dbt", str(acme / "dbt_project"), "--db", db]) == 0
    out = capsys.readouterr().out
    assert "checked 4 edge(s): 2 certified, 2 failed" in out
    assert "orphan value(s) violate referential integrity" in out
    assert "N:M" in out

    json_out = tmp_path / "graph.json"
    assert main(["relationships", "--table", "raw.payments",
                 "--db", db, "--json", str(json_out)]) == 0
    graph = json.loads(json_out.read_text())
    assert len(graph) == 2 and all(e["status"] == "failed" for e in graph)


def test_cli_requires_a_source(tmp_path, capsys):
    db = str(tmp_path / "empty.db")
    assert main(["mine-relationships", "--db", db]) == 2
    assert "needs --history-file" in capsys.readouterr().err

    # Read commands refuse a nonexistent store rather than silently creating
    # one — a typo'd --db used to leave a stray empty .db behind and make the
    # error look like a data problem instead of a path problem.
    missing = str(tmp_path / "typo.db")
    assert main(["relationships", "--db", missing]) == 2
    assert "no store at" in capsys.readouterr().err
    assert not (tmp_path / "typo.db").exists()

    Store(db)  # now the store exists but holds nothing
    assert main(["verify-relationships", "--db", db]) == 2
    assert "needs --warehouse" in capsys.readouterr().err
    assert main(["relationships", "--db", db]) == 2
    assert "no relationships recorded" in capsys.readouterr().err


def test_mcp_relationships_tool(acme, tmp_path):
    from semantix.mcp import McpServer

    db = str(tmp_path / "mcp.db")
    main(["mine-relationships", "--history-file", str(acme / "query_history.sql"),
          "--catalog", str(acme / "catalog.yml"), "--db", db])
    main(["verify-relationships", "--seed", str(acme / "seed.sql"),
          "--dbt", str(acme / "dbt_project"), "--db", db])

    server = McpServer(db)
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "relationships",
                   "arguments": {"table": "analytics.fct_orders"}},
    })
    payload = json.loads(response["result"]["content"][0]["text"])
    assert [e["join"] for e in payload["certified"]] == [
        "analytics.fct_orders.customer_id = raw.customers.customer_id"
    ]
    assert len(payload["failed"]) == 2
    assert response["result"]["isError"] is False


def test_mcp_relationships_without_data_is_an_error(tmp_path):
    from semantix.mcp import McpServer

    server = McpServer(str(tmp_path / "bare.db"))
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "relationships", "arguments": {}},
    })
    assert response["result"]["isError"] is True
