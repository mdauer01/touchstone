"""Store and interface integrity.

Round-two adversarial review, 2026-07-26. The remaining critical here is
`publish`, the one code path that WRITES into a customer's warehouse — a bug
there corrupts their data rather than merely misreporting ours.
"""

import json
import shutil

import pytest

import semantix.store as store_mod
from semantix.engine import run_scan
from semantix.mcp import McpServer
from semantix.store import Store, normalize_ts

pytest.importorskip("duckdb")


def scan(estate, db, **kw):
    return run_scan(
        dbt_path=str(estate / "dbt_project"),
        metrics_path=str(estate / "metrics.yml"),
        catalog_path=str(estate / "catalog.yml"),
        db_path=str(db),
        **kw,
    )


# ------------------------------------------------------------- timestamps

def test_unparseable_timestamps_are_refused_not_compared_as_text():
    """Valid time is compared as TEXT. Passing an unparseable timestamp
    straight through meant `03/01/2026` sorted before every ISO value, so
    as-of silently answered with the wrong definition — and signed it."""
    with pytest.raises(ValueError):
        normalize_ts("03/01/2026")
    with pytest.raises(ValueError):
        normalize_ts("last Tuesday")
    assert normalize_ts("2026-03-01T00:00:00+00:00") == "2026-03-01T00:00:00Z"


def test_as_of_with_a_bad_timestamp_fails_loudly(acme, tmp_path):
    _, store = scan(acme, tmp_path / "s.db")
    with pytest.raises(ValueError):
        store.as_of("net_revenue", "not a timestamp")


# ------------------------------------------------------------ scan atomicity

def test_an_aborted_scan_does_not_retire_definitions(acme, tmp_path, monkeypatch):
    """A crashed scan left a `scans` row with coverage but no observations,
    so the next history read treated every definition as absent — a whole
    estate retired by one interrupted run."""
    times = iter(["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"])
    now = {"t": next(times)}
    monkeypatch.setattr(store_mod, "utcnow", lambda: now["t"])

    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"
    scan(estate, db)

    # Simulate a scan that began and died before recording anything.
    store = Store(str(db))
    now["t"] = next(times)
    store.begin_scan("aborted", "duckdb")
    store._conn.commit()

    live = {i.relation for i in store.as_of("net_revenue", "2026-03-01T00:00:00Z")}
    assert "analytics.finance_net_revenue" in live
    assert "analytics.growth_net_revenue" in live


# ------------------------------------------------------------------ publish

def test_publish_does_not_corrupt_a_pre_existing_agents_root(acme, tmp_path):
    """The one path that writes to a customer warehouse. A positional
    3-column INSERT into a table whose real shape differs transposes every
    value into the wrong column."""
    import duckdb

    from semantix.services.writeback import publish_receipts

    _, store = scan(acme, tmp_path / "s.db")

    # Named agents.duckdb deliberately: the catalog and the schema then share
    # a name, which is the realistic case writeback already qualifies for.
    warehouse = tmp_path / "agents.duckdb"
    con = duckdb.connect(str(warehouse))
    con.execute('CREATE SCHEMA "agents".agents')
    # A customer's table with extra columns and a different column order.
    con.execute(
        'CREATE TABLE "agents".agents.root ('
        "  id BIGINT, key VARCHAR, content VARCHAR, provider VARCHAR, updated_at VARCHAR)"
    )
    con.execute(
        'INSERT INTO "agents".agents.root VALUES (1, \'k\', \'c\', \'someone_else\', \'x\')'
    )
    con.close()

    publish_receipts(store, str(warehouse))

    con = duckdb.connect(str(warehouse))
    rows = con.execute(
        'SELECT provider, key, content FROM "agents".agents.root '
        "WHERE provider = 'semantix'"
    ).fetchall()
    other = con.execute(
        'SELECT provider, key FROM "agents".agents.root '
        "WHERE provider = 'someone_else'"
    ).fetchall()
    con.close()

    assert rows, "nothing published"
    receipt_rows = 0
    for provider, key, content in rows:
        # Each value must be in ITS OWN column, which a positional insert
        # into this 5-column table would have scrambled.
        assert provider == "semantix"
        assert key and provider not in key
        assert content
        if key.startswith("receipt/"):
            receipt_rows += 1
            payload = json.loads(content)
            assert payload["concept"] == key.split("/", 1)[1]
            assert payload["signature"]["alg"] == "ed25519"
    assert receipt_rows, "no receipts published"
    assert other == [("someone_else", "k")], "another provider's row was damaged"


# ---------------------------------------------------------------------- MCP

def test_mcp_survives_a_json_rpc_batch(acme, tmp_path):
    """A batch is a JSON array. The loop assumed an object and died, taking
    attestation down for the rest of the session."""
    _, _ = scan(acme, tmp_path / "s.db")
    server = McpServer(str(tmp_path / "s.db"))
    response = server.handle([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert response is not None


def test_mcp_survives_non_object_json(acme, tmp_path):
    scan(acme, tmp_path / "s.db")
    server = McpServer(str(tmp_path / "s.db"))
    for payload in ("a string", 42, None, []):
        assert server.handle(payload) is not None or payload is None


def test_mcp_marks_an_invalid_signature_as_an_error(acme, tmp_path):
    """`verify_receipt` returned isError:false for a receipt whose signature
    does not verify — an agent checking only the transport-level flag would
    treat a forged receipt as good."""
    _, store = scan(acme, tmp_path / "s.db")
    receipt = store.issue_receipt("net_revenue").as_dict()
    receipt["status"] = "CERTIFIED"          # forge the field that matters

    server = McpServer(str(tmp_path / "s.db"))
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "verify_receipt", "arguments": {"receipt": receipt}},
    })
    assert response["result"]["isError"] is True
    assert json.loads(response["result"]["content"][0]["text"])["valid"] is False
