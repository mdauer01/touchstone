"""Signed receipts: certificates, not just JSON."""

import json

from touchstone.cli import main
from touchstone.engine import run_scan
from touchstone.signing import verify_receipt_dict


def _store_with_scan(acme, tmp_path):
    _, store = run_scan(
        dbt_path=str(acme / "dbt_project"),
        metrics_path=str(acme / "metrics.yml"),
        catalog_path=str(acme / "catalog.yml"),
        db_path=str(tmp_path / "s.db"),
    )
    return store


def test_receipts_are_signed_and_verifiable(acme, tmp_path):
    store = _store_with_scan(acme, tmp_path)
    receipt = store.issue_receipt("net_revenue")
    sig = receipt.signature
    assert sig["alg"] == "ed25519"
    assert sig["key_id"].startswith("sk_")
    ok, message = verify_receipt_dict(receipt.as_dict())
    assert ok
    assert "tamper-evidence" in message  # embedded key, honest wording
    ok, message = verify_receipt_dict(receipt.as_dict(), sig["public_key"])
    assert ok and "pinned" in message


def test_tampered_receipt_fails_verification(acme, tmp_path):
    store = _store_with_scan(acme, tmp_path)
    d = store.issue_receipt("net_revenue").as_dict()
    d["status"] = "CERTIFIED"  # forge the one field that matters
    ok, message = verify_receipt_dict(d)
    assert not ok
    assert "INVALID" in message


def test_wrong_pinned_key_is_rejected(acme, tmp_path):
    store = _store_with_scan(acme, tmp_path)
    d = store.issue_receipt("net_revenue").as_dict()
    ok, message = verify_receipt_dict(d, "A" * 43)
    assert not ok
    assert "does not match the pinned" in message


def test_one_key_per_store_and_persisted_payload_is_signed(acme, tmp_path):
    store = _store_with_scan(acme, tmp_path)
    r1 = store.issue_receipt("net_revenue")
    r2 = store.issue_receipt("gmv")
    assert r1.signature["key_id"] == r2.signature["key_id"]
    row = store._conn.execute(
        "SELECT payload_json FROM receipts WHERE receipt_id = ?", (r1.receipt_id,)
    ).fetchone()
    stored = json.loads(row["payload_json"])
    ok, _ = verify_receipt_dict(stored)
    assert ok  # what is stored IS the certificate


def test_cli_verify_roundtrip(acme, tmp_path, capsys):
    from touchstone.services.freshness import check_freshness

    store = _store_with_scan(acme, tmp_path)
    # Unchecked: the signature verifies, but a receipt nobody re-checked is a
    # historical attestation, not permission for current use (round eight).
    receipt_path = tmp_path / "r.json"
    receipt_path.write_text(json.dumps(store.issue_receipt("gmv").as_dict()))
    assert main(["experimental", "verify-receipt", str(receipt_path)]) == 1
    assert "signature valid" in capsys.readouterr().out
    # Issued fresh: still only historical OFFLINE — its freshness block says
    # what was true at issuance, not now (round fourteen).
    fresh = check_freshness(store, dbt_path=str(acme / "dbt_project"),
                            metrics_path=str(acme / "metrics.yml"),
                            catalog_path=str(acme / "catalog.yml"))
    assert fresh["status"] == "fresh", fresh
    receipt_path.write_text(json.dumps(store.issue_receipt("gmv", freshness=fresh).as_dict()))
    assert main(["experimental", "verify-receipt", str(receipt_path)]) == 1
    db = str(tmp_path / "s.db")
    capsys.readouterr()
    # Re-checked NOW against the receipt's own scan: the signature is authentic
    # and the estate unchanged — but under the conclusions policy no semantic
    # claim is usable, so the receipt is still not valid for current use.
    assert main(["experimental", "verify-receipt", str(receipt_path), "--require-current", "--db", db,
                 "--dbt", str(acme / "dbt_project"),
                 "--metrics", str(acme / "metrics.yml"),
                 "--catalog", str(acme / "catalog.yml")]) == 1
    current = capsys.readouterr().out
    assert "signature authentic: yes" in current
    assert "estate unchanged: yes" in current
    assert "claim usable: no" in current
    assert main(["experimental", "pubkey", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "sk_" in out
