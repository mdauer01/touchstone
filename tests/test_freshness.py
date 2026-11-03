"""Receipt freshness: verification decays honestly.

A receipt's status is a statement about the estate at scan time; these tests
pin the four freshness statuses and that the freshness claim itself is
covered by the receipt signature.
"""

import json
import shutil

from touchstone.cli import main
from touchstone.engine import run_scan
from touchstone.services.freshness import check_freshness
from touchstone.signing import verify_receipt_dict

GROWTH = "dbt_project/target/compiled/acme/models/growth/growth_net_revenue.sql"


def scan(estate, db_path, **kw):
    return run_scan(
        dbt_path=str(estate / "dbt_project"),
        metrics_path=str(estate / "metrics.yml"),
        catalog_path=str(estate / "catalog.yml"),
        db_path=str(db_path),
        **kw,
    )


def estate_kwargs(estate):
    return {"dbt_path": str(estate / "dbt_project"),
            "metrics_path": str(estate / "metrics.yml"),
            "catalog_path": str(estate / "catalog.yml")}


def test_fresh_when_estate_unchanged(acme, tmp_path):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    _, store = scan(estate, tmp_path / "f.db")

    fr = check_freshness(store, **estate_kwargs(estate))
    assert fr["status"] == "fresh"
    assert fr["estate_drift"] == {"changed": [], "added": [], "removed": []}
    assert fr["unchecked_systems"] == []

    receipt = store.issue_receipt("net_revenue", freshness=fr)
    assert receipt.freshness["status"] == "fresh"
    assert receipt.verified_as_of == fr["verified_as_of"]


def test_stale_when_a_model_changed_after_the_scan(acme, tmp_path):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    _, store = scan(estate, tmp_path / "f.db")

    (estate / GROWTH).write_text(
        "select sum(amount) as net_revenue from analytics.stg_orders\n"
    )
    fr = check_freshness(store, **estate_kwargs(estate))
    assert fr["status"] == "stale"
    assert fr["estate_drift"]["changed"] == ["dbt:model.acme.growth_net_revenue"]
    assert "rescan" in fr["note"]


def test_added_and_removed_artifacts_are_stale(acme, tmp_path):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    _, store = scan(estate, tmp_path / "f.db")

    # remove a metric from the declared ontology
    metrics = estate / "metrics.yml"
    metrics.write_text(metrics.read_text().replace("customer_ltv", "customer_value"))
    fr = check_freshness(store, **estate_kwargs(estate))
    assert fr["status"] == "stale"
    assert "metrics_yaml:customer_ltv" in fr["estate_drift"]["removed"]
    assert "metrics_yaml:customer_value" in fr["estate_drift"]["added"]


def test_partial_when_a_scanned_system_is_not_rechecked(acme, tmp_path):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    _, store = scan(estate, tmp_path / "f.db")

    fr = check_freshness(store, dbt_path=str(estate / "dbt_project"))
    assert fr["status"] == "partial"
    # The catalog is a source too: the verdicts depend on its columns and types.
    assert fr["unchecked_systems"] == ["catalog", "metrics_yaml"]
    # crucially: unchecked claims are NOT counted as removed
    assert fr["estate_drift"]["removed"] == []


def test_unknown_without_sources_or_snapshot(acme, tmp_path):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    _, store = scan(estate, tmp_path / "f.db")

    assert check_freshness(store)["status"] == "unknown"

    # a legacy db whose scan predates snapshot recording
    store._conn.execute("DELETE FROM scan_artifacts")  # test-only simulation
    store._conn.commit()
    fr = check_freshness(store, **estate_kwargs(estate))
    assert fr["status"] == "unknown"
    assert "predates" in fr["note"]

    receipt = store.issue_receipt("net_revenue")  # no freshness passed
    # "unchecked", not "unknown": no check was ATTEMPTED. Checked-but-
    # inconclusive ("unknown") blocks certification; the plain attest flow
    # must not, or the documented default workflow always refuses.
    assert receipt.freshness["status"] == "unchecked"


def test_freshness_is_inside_the_signature(acme, tmp_path):
    """Tampering must flip a value to something DIFFERENT — the earlier
    version of this test 'tampered' a fresh receipt to fresh, proving
    nothing."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    _, store = scan(estate, tmp_path / "f.db")

    fr = check_freshness(store, **estate_kwargs(estate))
    assert fr["status"] == "fresh"
    d = store.issue_receipt("net_revenue", freshness=fr).as_dict()
    ok, _ = verify_receipt_dict(d)
    assert ok

    forged_stale_to_fresh = json.loads(json.dumps(d))
    forged_stale_to_fresh["freshness"]["status"] = "stale"
    ok, message = verify_receipt_dict(forged_stale_to_fresh)
    assert not ok and "INVALID" in message

    forged_asof = json.loads(json.dumps(d))
    forged_asof["verified_as_of"] = "2030-01-01T00:00:00Z"
    ok, message = verify_receipt_dict(forged_asof)
    assert not ok and "INVALID" in message


def test_filters_only_change_is_stale(acme, tmp_path):
    """A declared metric whose FILTERS change while its expr stays put has
    changed — spec content hashing must catch it (Tim's silent-drift case)."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    _, store = scan(estate, tmp_path / "f.db")

    metrics = estate / "metrics.yml"
    metrics.write_text(
        metrics.read_text().replace("status <> 'cancelled'", "status <> 'void'")
    )
    fr = check_freshness(store, **estate_kwargs(estate))
    assert fr["status"] == "stale"
    assert fr["estate_drift"]["changed"] == ["metrics_yaml:gmv"]


def test_emptied_source_is_stale_not_unchecked(acme, tmp_path):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    _, store = scan(estate, tmp_path / "f.db")

    (estate / "metrics.yml").write_text("version: 1\nmetrics: []\n")
    fr = check_freshness(store, **estate_kwargs(estate))
    assert fr["status"] == "stale"
    assert set(fr["estate_drift"]["removed"]) == {
        "metrics_yaml:net_revenue", "metrics_yaml:gmv", "metrics_yaml:customer_ltv",
    }
    assert "metrics_yaml" not in fr["unchecked_systems"]


def test_loader_failure_degrades_to_unknown(acme, tmp_path):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    _, store = scan(estate, tmp_path / "f.db")

    fr = check_freshness(store, dbt_path=str(tmp_path / "does-not-exist"))
    assert fr["status"] == "unknown"
    assert "re-check failed" in fr["note"]


def test_scan_landing_mid_attest_degrades_freshness(acme, tmp_path):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "f.db"
    _, store = scan(estate, db)

    fr = check_freshness(store, **estate_kwargs(estate))
    scan(estate, db, label="landed in between")
    receipt = store.issue_receipt("net_revenue", freshness=fr)
    assert receipt.freshness["status"] == "unknown"
    assert "re-attest" in receipt.freshness["note"]


def test_cli_attest_with_estate_sources(acme, tmp_path, capsys):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "f.db"
    scan(estate, db)

    assert main([
        "attest", "net_revenue", "--db", str(db),
        "--dbt", str(estate / "dbt_project"),
        "--metrics", str(estate / "metrics.yml"),
        "--catalog", str(estate / "catalog.yml"),
    ]) == 0
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)  # stdout stays pure JSON
    assert receipt["freshness"]["status"] == "fresh"
    assert "freshness: fresh" in captured.err

    (estate / GROWTH).write_text(
        "select sum(amount) as net_revenue from analytics.stg_orders\n"
    )
    assert main([
        "attest", "net_revenue", "--db", str(db),
        "--dbt", str(estate / "dbt_project"),
        "--metrics", str(estate / "metrics.yml"),
        "--catalog", str(estate / "catalog.yml"),
    ]) == 0
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert receipt["freshness"]["status"] == "stale"
    ok, _ = verify_receipt_dict(receipt)
    assert ok  # the stale claim is signed too


def test_mcp_attest_carries_freshness(acme, tmp_path):
    from touchstone.mcp import McpServer

    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "f.db"
    scan(estate, db)

    server = McpServer(str(db), estate=estate_kwargs(estate))
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "attest", "arguments": {"concept": "net_revenue"}},
    })
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["freshness"]["status"] == "fresh"

    (estate / GROWTH).write_text(
        "select sum(amount) as net_revenue from analytics.stg_orders\n"
    )
    response = server.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "attest", "arguments": {"concept": "net_revenue"}},
    })
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["freshness"]["status"] == "stale"
