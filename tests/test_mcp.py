"""Attestation MCP server: agents check the receipt before quoting a number."""

import io
import json

from touchstone.engine import run_scan
from touchstone.mcp import McpServer, serve
from touchstone.models import VerdictType

from .policy import engine_kinds


def _db(acme, tmp_path):
    db = tmp_path / "m.db"
    run_scan(
        dbt_path=str(acme / "dbt_project"),
        metrics_path=str(acme / "metrics.yml"),
        catalog_path=str(acme / "catalog.yml"),
        db_path=str(db),
    )
    return str(db)


def _call(server, name, arguments, msg_id=1):
    return server.handle({"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})


def _payload(response):
    assert "error" not in response
    return json.loads(response["result"]["content"][0]["text"])


def test_initialize_and_tool_listing(acme, tmp_path):
    server = McpServer(_db(acme, tmp_path))
    init = server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18"}})
    assert init["result"]["serverInfo"]["name"] == "touchstone"
    tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = {t["name"] for t in tools["result"]["tools"]}
    assert names == {"attest", "asof", "history", "concepts", "relationships",
                     "verify_receipt"}
    # notifications get no response
    assert server.handle({"jsonrpc": "2.0",
                          "method": "notifications/initialized"}) is None


def test_attest_returns_signed_receipt_and_flags_contradiction(acme, tmp_path):
    server = McpServer(_db(acme, tmp_path))
    receipt = _payload(_call(server, "attest", {"concept": "net_revenue"}))
    # The engine contradicts; under the conclusions policy the receipt states
    # the effective status, a suspected divergence, never CONTRADICTION.
    assert receipt["status"] == "SUSPECTED_DIVERGENCE"
    assert any(v["summary"].startswith("Suspected divergence (")
               for v in receipt["verdicts"])
    scanned = [r for r in server.store.verdicts_for_scan(receipt["scan_id"])
               if r["concept_label"] == "net_revenue"]
    assert VerdictType.CONTRADICTION in engine_kinds(scanned)
    assert receipt["signature"]["alg"] == "ed25519"
    assert receipt["known_divergences"]

    # the receipt round-trips through verify_receipt on the same server: the
    # signature verifies, but a server started without estate sources never
    # re-checked anything, so the receipt is not valid for CURRENT use.
    verdict = _payload(_call(server, "verify_receipt", {"receipt": receipt}))
    assert verdict["detail"].startswith("signature valid")
    assert verdict["current_use"] is False and verdict["valid"] is False
    assert verdict["signature_authentic"] is True
    assert verdict["estate_unchanged"] is None          # never re-checked
    # configured with every scanned source, the estate check passes — but
    # under the conclusions policy no claim is usable, so the receipt is
    # still not valid for current use.
    configured = McpServer(server.store.path,
                           estate={"dbt_path": str(acme / "dbt_project"),
                                   "metrics_path": str(acme / "metrics.yml"),
                                   "catalog_path": str(acme / "catalog.yml")})
    fresh_receipt = _payload(_call(configured, "attest", {"concept": "gmv"}))
    assert fresh_receipt["freshness"]["status"] == "fresh"
    checked = _payload(_call(configured, "verify_receipt", {"receipt": fresh_receipt}))
    assert checked["signature_authentic"] is True
    assert checked["estate_unchanged"] is True
    assert checked["semantic_claim_usable"] is False
    assert checked["current_use"] is False and checked["valid"] is False


def test_concepts_and_history_read_models(acme, tmp_path):
    server = McpServer(_db(acme, tmp_path))
    concepts = _payload(_call(server, "concepts", {}))
    assert {v["concept"] for v in concepts["verdicts"]} >= {"net_revenue", "gmv"}
    history = _payload(_call(server, "history", {"concept": "net_revenue"}))
    assert history["restatement_boundaries"] == 0
    assert len(history["intervals"]) == 2  # finance + growth sites


def test_unknown_tool_and_bad_args_are_soft_errors(acme, tmp_path):
    server = McpServer(_db(acme, tmp_path))
    bad_tool = _call(server, "does_not_exist", {})
    assert bad_tool["error"]["code"] == -32602
    missing = _call(server, "asof", {"concept": "net_revenue"})  # no "at"
    assert missing["result"]["isError"] is True
    unknown = _call(server, "attest", {"concept": "nope"})
    assert unknown["result"]["isError"] is True


def test_stdio_serve_loop(acme, tmp_path):
    db = _db(acme, tmp_path)
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "attest",
                                 "arguments": {"concept": "gmv"}}}) + "\n")
    stdout = io.StringIO()
    assert serve(db, stdin, stdout) == 0
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert len(responses) == 2  # notification produced no reply
    receipt = json.loads(responses[1]["result"]["content"][0]["text"])
    assert receipt["concept"] == "gmv"
    # The engine certifies / reports aliasing; under the conclusions policy
    # the receipt states UNRESOLVED and every verdict is an experimental one.
    assert receipt["status"] == "UNRESOLVED"
    assert receipt["verdicts"] and all(
        v["summary"].startswith(("Experimental comparison, not a conclusion — certified ",
                                 "Experimental comparison, not a conclusion — aliasing "))
        for v in receipt["verdicts"])
