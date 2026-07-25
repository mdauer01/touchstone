"""Signed receipts: certificates, not just JSON."""

import json

from semantix.cli import main
from semantix.engine import run_scan
from semantix.signing import verify_receipt_dict


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
    store = _store_with_scan(acme, tmp_path)
    receipt_path = tmp_path / "r.json"
    receipt_path.write_text(json.dumps(store.issue_receipt("net_revenue").as_dict()))
    assert main(["verify-receipt", str(receipt_path)]) == 0
    db = str(tmp_path / "s.db")
    assert main(["pubkey", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "sk_" in out
