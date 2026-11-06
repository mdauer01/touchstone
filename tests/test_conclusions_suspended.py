"""Acceptance: no suspended claim can authorize reliance through any consumer.

Positive verdicts are suspended (touchstone.conclusions). This file checks
trust DECISIONS, not labels, for legacy records — stored CERTIFIED and
CONTRADICTION rows, an old signed CERTIFIED receipt, an already-published
warehouse table — and for newly generated results, through the CLI, JSON,
MCP, publishing and the dbt gates. Reliance is three separate answers:
signature authentic, estate unchanged, semantic claim usable; the first two
never establish the third.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from touchstone.cli import main
from touchstone.conclusions import POSITIVE_VERDICTS, reliance
from touchstone.engine import run_scan
from touchstone.mcp import McpServer
from touchstone.models import Receipt, VerdictType
from touchstone.services.writeback import publish_receipts
from touchstone.signing import assess_receipt, sign_receipt
from touchstone.store import Store

from .policy import engine_kinds

SUSPENDED = {"CERTIFIED", "ALIASING"}
DEFINITIVE = {"CONTRADICTION"}
MACROS = Path(__file__).resolve().parent.parent / "integrations/dbt/touchstone/macros/tests.sql"


def _estate(acme, tmp_path):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    return estate


def _scan(estate, db):
    return run_scan(dbt_path=str(estate / "dbt_project"), metrics_path=str(estate / "metrics.yml"),
                    catalog_path=str(estate / "catalog.yml"), db_path=str(db))


def _estate_args(estate):
    return {"dbt_path": str(estate / "dbt_project"), "metrics_path": str(estate / "metrics.yml"),
            "catalog_path": str(estate / "catalog.yml")}


def _call(server, name, args):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
    return json.loads(resp["result"]["content"][0]["text"])


def _legacy(db: Path) -> None:
    """Rewrite the stored rows as a store written before the policy would
    hold them: raw CERTIFIED / ALIASING / CONTRADICTION verdicts."""
    con = sqlite3.connect(db)
    rows = con.execute("SELECT rowid, verdict, evidence_json FROM verdicts").fetchall()
    for rowid, verdict, evidence in rows:
        ev = json.loads(evidence or "{}")
        raw = (ev.get("experimental") or ev.get("suspected") or {}).get("verdict")
        if raw:
            con.execute("UPDATE verdicts SET verdict = ? WHERE rowid = ?", (raw, rowid))
    con.commit()
    con.close()


@pytest.fixture()
def acme_scan(acme, tmp_path):
    estate = _estate(acme, tmp_path)
    db = tmp_path / "s.db"
    result, store = _scan(estate, db)
    store.close()
    return {"estate": estate, "db": db, "result": result, "tmp": tmp_path}


# ------------------------------------------------------------- new results

def test_a_new_scan_reports_no_positive_or_definitive_verdict(acme_scan, tmp_path, capsys):
    result = acme_scan["result"]
    shown = {v.verdict_type.value for v in result.verdicts}
    assert not shown & (SUSPENDED | DEFINITIVE), shown
    # The engine's judgment is kept, as experimental / suspected evidence.
    assert {VerdictType.CERTIFIED, VerdictType.ALIASING,
            VerdictType.CONTRADICTION} <= engine_kinds(result.verdicts)
    out = tmp_path / "scan.json"
    assert main(["experimental", "scan", "--dbt", str(acme_scan["estate"] / "dbt_project"),
                 "--metrics", str(acme_scan["estate"] / "metrics.yml"),
                 "--catalog", str(acme_scan["estate"] / "catalog.yml"),
                 "--db", str(tmp_path / "x.db"), "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "[CERTIFIED" not in printed and "[ALIASING" not in printed \
        and "[CONTRADICTION" not in printed
    verdicts = {v["verdict"] for v in json.loads(out.read_text())["verdicts"]}
    assert not verdicts & (SUSPENDED | DEFINITIVE)


def test_a_new_receipt_states_no_suspended_claim_and_is_never_current(acme_scan):
    server = McpServer(str(acme_scan["db"]), estate=_estate_args(acme_scan["estate"]))
    for concept in ("gmv", "net_revenue"):
        receipt = _call(server, "attest", {"concept": concept})
        assert receipt["status"] not in SUSPENDED | DEFINITIVE
        assert all(v["verdict"] not in SUSPENDED | DEFINITIVE for v in receipt["verdicts"])
        verdict = _call(server, "verify_receipt", {"receipt": receipt})
        assert verdict["signature_authentic"] is True
        assert verdict["estate_unchanged"] is True        # fresh, bound to its scan…
        assert verdict["semantic_claim_usable"] is False  # …and still not usable
        assert verdict["current_use"] is False and verdict["valid"] is False


# --------------------------------------------------------- legacy records

def test_legacy_stored_rows_surface_mapped_on_every_read(acme_scan, capsys):
    db = acme_scan["db"]
    _legacy(db)
    con = sqlite3.connect(db)
    raw = {r[0] for r in con.execute("SELECT verdict FROM verdicts")}
    con.close()
    assert raw & SUSPENDED and raw & DEFINITIVE        # the legacy rows are really there
    store = Store(str(db))
    try:
        scan = store.latest_scan()
        mapped = {v["verdict"] for v in store.verdicts_for_scan(scan["scan_id"])}
        assert not mapped & (SUSPENDED | DEFINITIVE)
        receipt = store.issue_receipt("gmv")
        assert receipt.status not in SUSPENDED
    finally:
        store.close()
    assert main(["experimental", "concepts", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "CERTIFIED" not in out and "ALIASING" not in out and "CONTRADICTION" not in out
    concepts = _call(McpServer(str(db)), "concepts", {})
    rows = concepts if isinstance(concepts, list) else concepts.get("concepts", concepts)
    assert '"verdict": "CERTIFIED"' not in json.dumps(rows)
    assert '"verdict": "CONTRADICTION"' not in json.dumps(rows)


def _legacy_receipt(db: Path, status: str = "CERTIFIED") -> dict:
    """An old receipt exactly as the store signed and persisted it."""
    store = Store(str(db))
    try:
        scan = store.latest_scan()
        r = Receipt(receipt_id="rcpt_legacy0001", concept="gmv", status=status,
                    definition_version=[], lineage=[], known_divergences=[], as_of=None,
                    issued_at=scan["started_at"], scan_id=scan["scan_id"],
                    verdicts=[{"verdict": status, "severity": 5, "summary": "legacy"}],
                    scope={}, verified_as_of=scan["started_at"],
                    freshness={"status": "fresh"}, proof_policy="touchstone-proof/2")
        sign_receipt(store, r)
        store._conn.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                            (r.receipt_id, r.concept, r.as_of, r.issued_at,
                             json.dumps(r.as_dict())))
        store._conn.commit()
        return r.as_dict()
    finally:
        store.close()


def test_an_old_signed_certified_receipt_is_authentic_but_never_usable(acme_scan, capsys):
    receipt = _legacy_receipt(acme_scan["db"])
    before = json.dumps(receipt, sort_keys=True)
    server = McpServer(str(acme_scan["db"]), estate=_estate_args(acme_scan["estate"]))
    verdict = _call(server, "verify_receipt", {"receipt": receipt})
    assert verdict["signature_authentic"] is True
    assert verdict["estate_unchanged"] is True
    assert verdict["semantic_claim_usable"] is False and verdict["current_use"] is False
    assert verdict["policy_assessment"]["note"].endswith("the signed receipt is unchanged")
    path = acme_scan["tmp"] / "legacy.json"
    path.write_text(json.dumps(receipt))
    assert main(["experimental", "verify-receipt", str(path), "--require-current", "--db", str(acme_scan["db"]),
                 "--dbt", str(acme_scan["estate"] / "dbt_project"),
                 "--metrics", str(acme_scan["estate"] / "metrics.yml"),
                 "--catalog", str(acme_scan["estate"] / "catalog.yml")]) == 1
    out = capsys.readouterr().out
    assert "signature authentic: yes" in out and "estate unchanged: yes" in out
    assert "claim usable: no" in out
    # Verification never rewrites the signed record.
    assert json.dumps(receipt, sort_keys=True) == before
    store = Store(str(acme_scan["db"]))
    try:
        stored = store._conn.execute("SELECT payload_json FROM receipts WHERE receipt_id = ?",
                                     ("rcpt_legacy0001",)).fetchone()[0]
    finally:
        store.close()
    assert json.loads(stored) == receipt


def test_reliance_never_follows_from_authenticity_and_an_unchanged_estate():
    for status in ("CERTIFIED", "ALIASING", "UNRESOLVED", "SUSPECTED_DIVERGENCE",
                   "CONTRADICTION", None):
        r = reliance(status, signature_authentic=True, estate_unchanged=True)
        assert r.semantic_claim_usable is False and r.current_use is False, status
    a = assess_receipt({"status": "CERTIFIED", "scan_id": "s", "proof_policy": "touchstone-proof/2",
                        "freshness": {"status": "fresh"}}, {"status": "fresh", "scan_id": "s"})
    assert (a.signature_authentic, a.estate_unchanged, a.semantic_claim_usable,
            a.current_use) == (True, True, False, False)


# ------------------------------------------------------------- publishing

def test_publish_writes_effective_verdicts_and_policy_rows(acme_scan):
    duckdb = pytest.importorskip("duckdb")
    _legacy(acme_scan["db"])            # even from legacy rows
    warehouse = acme_scan["tmp"] / "wh.duckdb"
    store = Store(str(acme_scan["db"]))
    try:
        publish_receipts(store, str(warehouse))
    finally:
        store.close()
    con = duckdb.connect(str(warehouse))
    rows = con.execute("SELECT verdict, policy_status, usable_for_reliance "
                       "FROM touchstone.verdicts").fetchall()
    root = dict(con.execute("SELECT key, content FROM agents.root "
                            "WHERE provider = 'touchstone'").fetchall())
    con.close()
    assert rows and not {r[0] for r in rows} & (SUSPENDED | DEFINITIVE)
    assert {r[1] for r in rows} == {POSITIVE_VERDICTS} and {r[2] for r in rows} == {False}
    assert json.loads(root["policy/conclusions"])["usable_for_reliance"] is False
    for key in [k for k in root if k.startswith("receipt/")]:
        assessment = json.loads(root["assessment/" + key.split("/", 1)[1]])
        assert assessment["semantic_claim_usable"] is False and assessment["current_use"] is False


def test_an_already_published_table_is_refreshed_without_touching_receipts(acme_scan, capsys):
    duckdb = pytest.importorskip("duckdb")
    warehouse = acme_scan["tmp"] / "old.duckdb"
    con = duckdb.connect(str(warehouse))
    # The projection as an older touchstone wrote it.
    con.execute("CREATE SCHEMA touchstone")
    con.execute("CREATE TABLE touchstone.verdicts (scan_id VARCHAR, scanned_at TIMESTAMPTZ, "
                "published_at TIMESTAMPTZ, concept VARCHAR, verdict VARCHAR, severity INTEGER, "
                "summary VARCHAR, relation VARCHAR)")
    con.execute("INSERT INTO touchstone.verdicts VALUES ('s', now(), now(), 'gmv', 'CERTIFIED', "
                "5, 'identical', 'analytics.total_gmv'), ('s', now(), now(), 'net_revenue', "
                "'CONTRADICTION', 94, 'differs', NULL)")
    legacy = _legacy_receipt(acme_scan["db"])
    con.execute("CREATE SCHEMA agents")
    con.execute("CREATE TABLE agents.root (provider VARCHAR, key VARCHAR, content VARCHAR)")
    content = json.dumps(legacy)
    con.execute("INSERT INTO agents.root VALUES ('touchstone', 'receipt/gmv', ?)", (content,))
    con.close()

    assert main(["experimental", "publish", "--refresh-policy", "--warehouse", str(warehouse),
                 "--db", str(acme_scan["db"])]) == 0
    con = duckdb.connect(str(warehouse))
    rows = con.execute("SELECT concept, verdict, published_verdict, usable_for_reliance "
                       "FROM touchstone.verdicts ORDER BY concept").fetchall()
    root = dict(con.execute("SELECT key, content FROM agents.root").fetchall())
    con.close()
    assert rows == [("gmv", "UNRESOLVED", "CERTIFIED", False),
                    ("net_revenue", "SUSPECTED_DIVERGENCE", "CONTRADICTION", False)]
    assert root["receipt/gmv"] == content            # signed record byte-identical
    assessment = json.loads(root["assessment/gmv"])
    assert assessment["signature_authentic"] is True
    assert assessment["semantic_claim_usable"] is False


def test_dbt_gates_cannot_pass_on_a_suspended_claim():
    duckdb = pytest.importorskip("duckdb")
    text = MACROS.read_text()
    certified = text[text.index("{% test certified"):text.index("{% endtest %}",
                                                                  text.index("{% test certified"))]
    # Fails closed: no condition on the projection's contents at all.
    assert "where" not in certified.lower() and "findings()" not in certified
    con = duckdb.connect()
    body = certified.split("%}", 1)[1].replace("{{ concept }}", "gmv").replace(
        "{{ dbt.type_string() }}", "VARCHAR")
    assert con.execute(body).fetchall(), "the certified test must return a failing row"
    gate = text[text.index("{% test no_contradictions"):]
    assert "'SUSPECTED_DIVERGENCE'" in gate and "'CONTRADICTION'" in gate


# ------------------------------------------------------------------ gates

def test_scan_and_diff_gates_still_fail_on_suspected_divergence(acme_scan, tmp_path):
    estate = acme_scan["estate"]
    args = ["experimental", "scan", "--dbt", str(estate / "dbt_project"), "--metrics", str(estate / "metrics.yml"),
            "--catalog", str(estate / "catalog.yml"), "--db", str(tmp_path / "g.db")]
    assert main(args + ["--fail-on", "contradiction"]) == 1


def test_a_legacy_baseline_does_not_make_mapped_verdicts_new(acme_scan):
    from touchstone.services.diff import diff_verdicts

    _legacy(acme_scan["db"])           # baseline holds raw legacy rows
    _scan(acme_scan["estate"], acme_scan["db"])[1].close()
    store = Store(str(acme_scan["db"]))
    try:
        diff = diff_verdicts(store)
    finally:
        store.close()
    assert diff["new"] == [] and diff["resolved"] == []


# ------------------------------- found by the conversion audit (round 17)

def test_receipt_guards_still_disclose_on_a_suspended_claim(acme_scan):
    """The guards decide on the engine's verdict; checking the already-mapped
    status (UNRESOLVED) silently dropped their disclosures."""
    store = Store(str(acme_scan["db"]))
    try:
        future = store.issue_receipt("gmv", as_of="2099-01-01T00:00:00Z")
        current = store.issue_receipt("gmv")
    finally:
        store.close()
    assert any("that instant is in the future" in d for d in future.known_divergences)
    entry = next(v for v in current.verdicts if v.get("experimental"))
    assert entry["verdict"] == "UNRESOLVED"
    assert entry["experimental"]["verdict"] in SUSPENDED
    assert entry["experimental"]["proof"]["policy"]


def test_breaking_an_experimental_equality_is_lost_not_resolved(acme_scan, tmp_path):
    from touchstone.services.diff import diff_verdicts, markdown_comment

    estate, db = acme_scan["estate"], acme_scan["db"]
    gmv = next((estate / "dbt_project/target/compiled").rglob("total_gmv.sql"))
    gmv.write_text(gmv.read_text().replace("sum(", "sum(2 * ", 1))
    _scan(estate, db)[1].close()
    store = Store(str(db))
    try:
        md = markdown_comment(diff_verdicts(store))
    finally:
        store.close()
    assert "LOST by this change" in md and "under the experimental rules" in md
    gone_line = next((ln for ln in md.splitlines() if "no longer reported" in ln), "")
    assert "`gmv`" not in gone_line and "resolved" not in md


def test_attest_without_estate_sources_says_not_checked(acme_scan, capsys):
    assert main(["experimental", "attest", "gmv", "--db", str(acme_scan["db"])]) == 0
    err = capsys.readouterr().err
    assert "estate unchanged: not checked" in err and "claim usable: no" in err


def test_monitor_evidence_never_names_a_match_as_conforming():
    from touchstone.conclusions import effective_finding

    f = effective_finding({"kind": "definition_violation", "conformance": "violation",
                           "summary": "x", "calculation_matches":
                           "main.finance.revenue_mv/net_revenue (conforming)"})
    assert f["kind"] == "suspected_difference"
    assert "(conforming)" not in f["calculation_matches"]
    assert "(experimental match)" in f["calculation_matches"]


# --------------------------- found by the second surface audit (round 17)

def _as_of_receipt(db: Path, *, issued_at: str | None = None,
                   as_of: str = "2026-09-01T00:00:00Z") -> dict:
    """A legacy as-of CERTIFIED receipt as the store signed and kept it."""
    store = Store(str(db))
    try:
        scan = store.latest_scan()
        r = Receipt(receipt_id="rcpt_legacyasof", concept="gmv", status="CERTIFIED",
                    definition_version=[], lineage=[], known_divergences=[], as_of=as_of,
                    issued_at=issued_at or scan["started_at"], scan_id=scan["scan_id"],
                    verdicts=[{"verdict": "CERTIFIED", "severity": 5, "summary": "legacy"}],
                    scope={}, verified_as_of=scan["started_at"],
                    freshness={"status": "fresh"}, proof_policy="touchstone-proof/2")
        sign_receipt(store, r)
        store._conn.execute("INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
                            (r.receipt_id, r.concept, r.as_of, r.issued_at,
                             json.dumps(r.as_dict())))
        store._conn.commit()
        return r.as_dict()
    finally:
        store.close()


def test_an_as_of_certified_receipt_never_passes_verification(acme_scan, capsys):
    """The as-of branch returned `historical` alone, so verify-receipt exited
    0 and MCP said valid/trusted for an as-of CERTIFIED receipt."""
    receipt = _as_of_receipt(acme_scan["db"])
    path = acme_scan["tmp"] / "asof.json"
    path.write_text(json.dumps(receipt))
    for extra in ([], ["--db", str(acme_scan["db"])]):
        assert main(["experimental", "verify-receipt", str(path), *extra]) == 1, extra
    captured = capsys.readouterr()
    assert "claim usable: no" in captured.out
    # The signed status is named as signed, never as a current conclusion.
    assert "signed status CERTIFIED (as signed; suspended" in captured.out
    assert "status CERTIFIED ·" not in captured.out
    # Self-reported freshness is never presented as a check made now.
    assert "still matches" not in captured.out + captured.err
    for estate in (None, _estate_args(acme_scan["estate"])):
        verdict = _call(McpServer(str(acme_scan["db"]), estate=estate),
                        "verify_receipt", {"receipt": receipt})
        assert verdict["historically_valid"] is True
        assert verdict["valid"] is False and verdict["trusted"] is False
        assert verdict["semantic_claim_usable"] is False and "error" in verdict


def test_a_receipt_the_store_did_not_issue_is_not_a_historical_record(acme_scan, capsys):
    receipt = _as_of_receipt(acme_scan["db"])
    forged = acme_scan["tmp"] / "forged.db"
    _scan(acme_scan["estate"], forged)[1].close()     # a different store
    path = acme_scan["tmp"] / "asof.json"
    path.write_text(json.dumps(receipt))
    assert main(["experimental", "verify-receipt", str(path), "--db", str(forged)]) == 1
    out = capsys.readouterr().out
    assert "signature authentic: no" in out and "NOT a valid attestation" in out
    verdict = _call(McpServer(str(forged)), "verify_receipt", {"receipt": receipt})
    assert verdict["signature_authentic"] is False
    assert verdict["historically_valid"] is False
    assert verdict["valid"] is False and verdict["trusted"] is False


def test_an_as_of_instant_after_issuance_records_nothing():
    a = assess_receipt({"status": "UNRESOLVED", "as_of": "2099-01-01T00:00:00Z",
                        "issued_at": "2026-10-01T00:00:00Z"})
    assert a.historical is False and a.valid_for_stated_use is False
    assert "after it was issued" in a.warnings[0]


def test_a_pre_policy_store_signs_no_raw_engine_verdict(tmp_path, acme):
    """Downgrading before the policy mapping skipped it: entries said
    "[downgraded] … canonically identical" with "superseded: CERTIFIED"."""
    estate = _estate(acme, tmp_path)
    db = tmp_path / "old.db"
    _scan(estate, db)[1].close()
    con = sqlite3.connect(db)
    (stats,) = con.execute("SELECT stats_json FROM scans").fetchone()
    stats = json.loads(stats)
    stats.pop("proof_policy")
    con.execute("UPDATE scans SET stats_json = ?", (json.dumps(stats),))
    con.commit()
    con.close()
    _legacy(db)
    store = Store(str(db))
    try:
        receipts = [store.issue_receipt(c) for c in ("gmv", "net_revenue")]
    finally:
        store.close()
    for receipt in receipts:
        text = json.dumps(receipt.as_dict())
        assert receipt.status == "UNRESOLVED"
        for v in receipt.verdicts:
            assert v["verdict"] not in SUSPENDED | DEFINITIVE
            assert v.get("superseded") not in SUSPENDED | DEFINITIVE
            assert v["summary"].startswith("[downgraded] ")
            assert v.get("experimental") or v.get("suspected") or \
                v["summary"].startswith("[downgraded] ") and v["verdict"] == "UNRESOLVED"
        assert "recertify" not in text
        assert "Declared definition matches " not in text
    assert any(v.get("experimental", {}).get("verdict") == "CERTIFIED"
               and "[downgraded] Experimental comparison" in v["summary"]
               for v in receipts[0].verdicts)


def test_a_suspected_divergence_states_no_equality_as_fact(acme_scan, tmp_path):
    from touchstone.conclusions import effective_scan_fields

    store = Store(str(acme_scan["db"]))
    try:
        rows = store.verdicts_for_scan(store.latest_scan()["scan_id"])
        receipt = store.issue_receipt("net_revenue")
    finally:
        store.close()
    suspected = [v for v in rows if v["verdict"] == "SUSPECTED_DIVERGENCE"]
    assert suspected
    for text in [v["summary"] for v in suspected] + list(receipt.known_divergences):
        assert "Declared definition matches " not in text
    assert any("appears to match (experimental comparison)" in v["summary"]
               for v in suspected)
    # Mapping is idempotent on its own text (a re-run of --refresh-policy).
    once = effective_scan_fields("CONTRADICTION", 9, "Declared definition matches x; y", {})
    twice = effective_scan_fields("CONTRADICTION", 9, once[2], {})
    assert once[2] == twice[2] and twice[2].count("Suspected divergence (") == 1
    pos = effective_scan_fields("CERTIFIED", 0, "identical", {})
    assert effective_scan_fields("CERTIFIED", 0, pos[2], {})[2] == pos[2]


def test_propose_memo_never_clears_a_site_on_a_suspended_equality(acme_scan, tmp_path, capsys):
    estate = acme_scan["estate"]
    out = tmp_path / "proposals"
    assert main(["experimental", "propose", "--db", str(acme_scan["db"]), "--out", str(out),
                 "--dbt", str(estate / "dbt_project"),
                 "--catalog", str(estate / "catalog.yml")]) == 0
    printed = capsys.readouterr().out
    memo = (out / "RESOLUTIONS.md").read_text()
    for text in (memo, printed):
        for phrase in ("no change needed", "already match", "round trip verified",
                       "round trips verified", "computes today", "ITSELF",
                       "contradiction resolution"):
            assert phrase not in text, phrase
    assert "experimental comparison, not a conclusion" in memo


def test_refresh_policy_refuses_a_target_with_nothing_published(acme_scan, capsys):
    pytest.importorskip("duckdb")
    typo = acme_scan["tmp"] / "typo.duckdb"
    assert main(["experimental", "publish", "--refresh-policy", "--warehouse", str(typo),
                 "--db", str(acme_scan["db"])]) == 2
    assert not typo.exists()                       # a refresh never creates one
    assert main(["experimental", "publish", "--refresh-policy", "--warehouse", "duckdb://",
                 "--db", str(acme_scan["db"])]) == 2
    import duckdb
    empty = acme_scan["tmp"] / "empty.duckdb"
    duckdb.connect(str(empty)).close()
    assert main(["experimental", "publish", "--refresh-policy", "--warehouse", str(empty),
                 "--db", str(acme_scan["db"])]) == 2
    assert "nothing published there" in capsys.readouterr().err


def test_refresh_policy_survives_odd_rows_and_is_idempotent(acme_scan):
    duckdb = pytest.importorskip("duckdb")
    from touchstone.services.writeback import refresh_published_policy

    warehouse = acme_scan["tmp"] / "old.duckdb"
    con = duckdb.connect(str(warehouse))
    con.execute("CREATE SCHEMA touchstone")
    con.execute("CREATE TABLE touchstone.verdicts (scan_id VARCHAR, concept VARCHAR, "
                "verdict VARCHAR, severity INTEGER, summary VARCHAR)")
    con.execute("INSERT INTO touchstone.verdicts VALUES ('s', 'gmv', 'CERTIFIED', 5, "
                "'identical'), ('s', 'net_revenue', 'CONTRADICTION', 94, "
                "'Declared definition matches a; b diverges')")
    con.execute("CREATE SCHEMA agents")
    con.execute("CREATE TABLE agents.root (provider VARCHAR, key VARCHAR, content VARCHAR)")
    con.execute("INSERT INTO agents.root VALUES ('touchstone', 'receipt/x', '[1, 2]'), "
                "('touchstone', 'receipt/y', 'not json')")
    con.close()
    for _ in range(2):
        refresh_published_policy(str(warehouse))
    con = duckdb.connect(str(warehouse))
    rows = dict(con.execute("SELECT concept, summary FROM touchstone.verdicts").fetchall())
    keys = {r[0] for r in con.execute("SELECT key FROM agents.root").fetchall()}
    con.close()
    assert rows["gmv"].count("Experimental comparison, not a conclusion") == 1
    assert rows["net_revenue"].count("Suspected divergence (") == 1
    assert "Declared definition matches " not in rows["net_revenue"]
    assert {"assessment/x", "assessment/y"} <= keys


def test_monitor_states_no_match_or_dispute_as_fact(tmp_path):
    from .test_monitor_conformance import GOVERNED, VIEW, _rows, run

    two = VIEW.format(extra="") + "  - name: gross_revenue\n    expr: SUM(amount)\n"
    q = "SELECT MEASURE(net_revenue) AS gross_revenue FROM main.finance.revenue_mv"
    result = run(tmp_path / "a", _rows(q), view_text=two)
    assert result["policy"]["positive_verdicts"] == POSITIVE_VERDICTS
    for f in result["findings"]:
        text = f.get("calculation_matches") or ""
        assert "(experimental match)" in text or "under the experimental rules" in text
    # An unnamed near miss is a suspected candidate, its observations unverified.
    cand = run(tmp_path / "b", _rows(f"SELECT {GOVERNED} AS rev_check FROM main.finance.orders"))
    (f,) = [f for f in cand["findings"] if f["kind"] == "candidate_alt_definition"]
    assert f["observations_unverified"] is True and f["summary"].startswith("candidate, suspected")


def test_withheld_and_contested_wording_is_qualified():
    from touchstone.conclusions import effective_finding
    from touchstone.services.monitor import _withhold

    contested = {"kind": "contested_use", "conformance": "conforming", "severity": 55,
                 "summary": "conforms to X, but …", "concept": "c"}
    w = effective_finding(_withhold(contested, ["sqlglot 0.0 (not qualified)"]))
    assert w["kind"] == "unverifiable" and w["severity"] == 55
    assert w["withheld_kind"] == "contested_use"
    assert w["withheld"].startswith("experimental at most, not a conclusion")
    text = Path(__file__).resolve().parent.parent.joinpath(
        "src/touchstone/services/monitor.py").read_text()
    assert "disagree with each other — " not in text and "open contradiction" not in text


def test_dbt_package_docs_promise_no_certification():
    readme = MACROS.parent.parent / "README.md"
    text = readme.read_text()
    assert "affirmatively certified" not in text
    assert "Positive verdicts are suspended" in text and "--refresh-policy" in text
