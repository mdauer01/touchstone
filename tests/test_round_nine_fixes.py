"""Round nine: follow-ups from the external review of 8dc9a83.

Historical validity and current use were one boolean, so a stale as-of
receipt reached agents as `current_use: true` and the CLI told an unchecked
historical receipt it had been "re-checked and matches". And the execution
oracle's comparator rounded every number to nine significant digits, so it
could have called 1,000,000,001 and 1,000,000,002 the same result. These
tests exercise the ENDPOINTS (MCP tool, CLI commands), not only the helper.
"""

from __future__ import annotations

import decimal
import json

import pytest

from touchstone.cli import main
from touchstone.engine import run_scan
from touchstone.mcp import McpServer


@pytest.fixture()
def edited_estate(tmp_path):
    """A scanned estate whose model was then edited to double its result."""
    models = tmp_path / "models"
    models.mkdir()
    model = models / "m.sql"
    model.write_text("-- touchstone: relation=db.main.m\nSELECT SUM(amount) AS m FROM db.main.t\n")
    (tmp_path / "catalog.yml").write_text("db:\n  main:\n    t:\n      amount: DOUBLE\n")
    (tmp_path / "metrics.yml").write_text(
        "version: 1\nmetrics:\n  - name: m\n    model: db.main.t\n    expr: \"sum(amount)\"\n")
    db = tmp_path / "s.db"
    _, store = run_scan(sql_dir=str(models), catalog_path=str(tmp_path / "catalog.yml"),
                        metrics_path=str(tmp_path / "metrics.yml"), db_path=str(db))
    as_of = store.latest_scan()["started_at"]
    store.close()
    model.write_text("-- touchstone: relation=db.main.m\nSELECT SUM(amount) * 2 AS m FROM db.main.t\n")
    return {"db": str(db), "as_of": as_of, "sql_dir": str(models),
            "metrics": str(tmp_path / "metrics.yml"), "tmp": tmp_path}


def _call(server, name, args):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
    return json.loads(resp["result"]["content"][0]["text"])


# ------------------------------------------------------------- MCP endpoint

def test_mcp_stale_historical_receipt_is_never_current_use(edited_estate):
    server = McpServer(edited_estate["db"], estate={
        "sql_dir": edited_estate["sql_dir"], "metrics_path": edited_estate["metrics"]})
    receipt = _call(server, "attest", {"concept": "m", "as_of": edited_estate["as_of"]})
    assert receipt["freshness"]["status"] == "stale"
    verdict = _call(server, "verify_receipt", {"receipt": receipt})
    assert verdict["describes"] == "as_of"
    assert verdict["historically_valid"] is True
    assert verdict["current_use"] is False
    # A valid historical record, but its claim is not usable under the
    # conclusions policy — so not valid for its stated use, an error result.
    assert verdict["semantic_claim_usable"] is False
    assert verdict["valid"] is False and verdict["trusted"] is False


def test_mcp_unchecked_current_receipt_is_neither_current_nor_valid(edited_estate):
    server = McpServer(edited_estate["db"])
    receipt = _call(server, "attest", {"concept": "m"})
    verdict = _call(server, "verify_receipt", {"receipt": receipt})
    assert verdict["describes"] == "current"
    assert verdict["current_use"] is False and verdict["valid"] is False


def test_mcp_fresh_current_receipt_is_current_use(acme, tmp_path):
    db = tmp_path / "m.db"
    run_scan(dbt_path=str(acme / "dbt_project"), metrics_path=str(acme / "metrics.yml"),
             catalog_path=str(acme / "catalog.yml"), db_path=str(db))
    server = McpServer(str(db), estate={"dbt_path": str(acme / "dbt_project"),
                                        "metrics_path": str(acme / "metrics.yml"),
                                        "catalog_path": str(acme / "catalog.yml")})
    receipt = _call(server, "attest", {"concept": "gmv"})
    verdict = _call(server, "verify_receipt", {"receipt": receipt})
    # The fresh live re-check still establishes what it can — an authentic
    # signature over an unchanged estate — but under the conclusions policy
    # positive claims are suspended, so the receipt is never usable now.
    assert verdict["signature_authentic"] is True
    assert verdict["estate_unchanged"] is True
    assert verdict["semantic_claim_usable"] is False
    assert verdict["current_use"] is False and verdict["valid"] is False


# ------------------------------------------------------------- CLI endpoints

def test_cli_attest_never_calls_a_historical_receipt_current(edited_estate, capsys):
    assert main(["attest", "m", "--as-of", edited_estate["as_of"], "--db", edited_estate["db"]]) == 0
    reliance = [line for line in capsys.readouterr().err.splitlines() if line.startswith("reliance:")]
    assert reliance and "CURRENT" not in reliance[0] and "re-checked" not in reliance[0]
    # Wording under the reliance policy: a record of the instant, with the
    # three reliance answers spelled out.
    assert "a record of what was said about that instant only" in reliance[0]
    assert "claim usable: no" in reliance[0]


def test_cli_attest_unchecked_current_receipt_is_historical(edited_estate, capsys):
    assert main(["attest", "m", "--db", edited_estate["db"]]) == 0
    err = capsys.readouterr().err
    # The reliance line now reads "NOT valid for current use (… claim usable: no)".
    assert "NOT valid for current use" in err and "valid for CURRENT use" not in err
    assert "claim usable: no" in err


def test_cli_verify_receipt_require_current_rejects_historical(edited_estate, capsys):
    assert main(["attest", "m", "--as-of", edited_estate["as_of"], "--db", edited_estate["db"],
                 "--sql-dir", edited_estate["sql_dir"], "--metrics", edited_estate["metrics"]]) == 0
    receipt_path = edited_estate["tmp"] / "r.json"
    receipt_path.write_text(capsys.readouterr().out)
    # Historically valid, but its claim is suspended under the conclusions
    # policy: not valid for its stated use either.
    assert main(["verify-receipt", str(receipt_path)]) == 1
    captured = capsys.readouterr()
    assert "a record of what was said about that instant only" in captured.out
    assert "a record of that instant only" in captured.err   # the stated-use warning
    assert main(["verify-receipt", str(receipt_path), "--require-current"]) == 1


# --------------------------------------------------- oracle comparator

def test_oracle_comparator_is_exact_for_every_numeric_type():
    pytest.importorskip("duckdb")
    from touchstone.oracle import _norm, _same

    def same(a, b):
        return _same(("rows", [(_norm(a),)]), ("rows", [(_norm(b),)]))

    D = decimal.Decimal
    assert not same(1_000_000_001, 1_000_000_002)
    assert not same(D("1.0000000001"), D("1.0000000002"))
    assert not same(0.30000000000000004, 0.3)
    assert not same(0.1, D("0.1"))          # a DOUBLE 0.1 is not the decimal 0.1
    assert same(5, 5.0) and same(D("1.10"), D("1.1"))
    assert same(float("nan"), float("nan"))
