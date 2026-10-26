"""semantix-proof/2: certification through a small, stated grammar.

The general optimizer no longer grants proof grade. Inside the grammar a form
is produced only by the policy's RULES; outside it the optimizer's reading is
a HYPOTHESIS that can never certify, alias, conform or accuse. Positive
verdicts record the policy, rules and assumptions they rest on, and a scan
made under another policy cannot carry its verdicts into a new receipt or a
current-use answer, however unchanged the estate's files are.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
import sqlglot

from semantix.engine import run_scan
from semantix.extraction.canonicalize import canonicalize
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.proof import (ASSUMPTIONS, POLICY_ID, RULES, parenthesize,
                                       round_trips, strip_parens)
from semantix.extraction.registry import SchemaRegistry
from semantix.mcp import McpServer
from semantix.models import PROOF_LEVELS, CanonLevel, Provenance, VerdictType
from semantix.services.freshness import check_freshness
from semantix.signing import assess_receipt
from semantix.store import Store

CAT = {"db.main.t": {"amount": "DOUBLE", "refund": "DOUBLE", "status": "VARCHAR",
                     "a": "DOUBLE", "b": "DOUBLE", "c": "DOUBLE"},
       "db.main.u": {"id": "INTEGER", "amount": "DOUBLE"}}


def _fp(sql: str):
    registry = SchemaRegistry(CAT)
    canon = canonicalize(sql, registry, "duckdb")
    (fp,) = extract_fingerprints(canon, "db.main.m", Provenance("t", "m"), "duckdb", registry)
    return canon, fp


# --------------------------------------------------------------- the rules

def test_every_rule_a_form_used_is_a_stated_rule():
    for sql in ("SELECT SUM(amount) AS m FROM db.main.t WHERE (status = 'x')",
                "WITH s AS (SELECT * FROM db.main.t) SELECT SUM(amount) AS m FROM s",
                "SELECT status, SUM(amount) AS m FROM db.main.t GROUP BY 1 ORDER BY 2",
                "SELECT SUM(amount) AS m FROM db.main.t WHERE amount BETWEEN 1 AND 2"):
        canon, fp = _fp(sql)
        assert canon.level == CanonLevel.SUPPORTED and fp.rules
        assert set(fp.rules) <= set(RULES), set(fp.rules) - set(RULES)


@pytest.mark.parametrize("a, b", [
    ("(status = 'x')", "status = 'x'"),
    ("((status = 'x')) AND (amount > 0)", "amount > 0 AND status = 'x'"),
    ("amount BETWEEN 1 AND 2", "amount >= 1 AND amount <= 2"),
])
def test_redundant_syntax_is_one_form(a, b):
    sql = "SELECT SUM(amount) AS m FROM db.main.t WHERE {}"
    assert _fp(sql.format(a))[1].core.hash() == _fp(sql.format(b))[1].core.hash()


@pytest.mark.parametrize("a, b", [
    ("SUM(a - (b - c))", "SUM(a - b - c)"),
    ("SUM((a + b) * c)", "SUM(a + b * c)"),
    ("SUM(a + (b + c))", "SUM(a + b + c)"),
    ("SUM(-(a + b))", "SUM(-a + b)"),
])
def test_parentheses_that_group_are_kept(a, b):
    sql = "SELECT {} AS m FROM db.main.t"
    fa, fb = _fp(sql.format(a))[1], _fp(sql.format(b))[1]
    assert fa.canon_level in PROOF_LEVELS and fb.canon_level in PROOF_LEVELS
    assert fa.core.hash() != fb.core.hash()


def test_parenthesization_parses_back_to_the_same_tree():
    for text in ("a - (b - c)", "NOT (x = 1 OR y = 2)", "-(-a)", "(a = 1) = (b = 2)",
                 "(a OR b) AND c", "a * (b / c)", "x IN (1, 2) AND NOT y IS NULL"):
        tree = parenthesize(strip_parens(sqlglot.parse_one(f"SELECT {text}", read="duckdb")))
        assert round_trips(tree, "duckdb"), tree.sql("duckdb")


def test_between_symmetric_is_refused():
    canon, _ = _fp("SELECT SUM(amount) AS m FROM db.main.t "
                   "WHERE amount BETWEEN SYMMETRIC 2 AND 1")
    assert canon.level != CanonLevel.SUPPORTED


@pytest.mark.parametrize("sql, reason", [
    ("SELECT SUM(t.amount) AS m FROM db.main.t t JOIN db.main.u u ON t.amount = u.amount",
     "uses a join"),
    ("SELECT SUM(amount) AS m FROM db.main.t LIMIT 1", "LIMIT"),
    ("SELECT product(amount) AS m FROM db.main.t", "product"),
    ("SELECT SUM(amount) OVER () AS m FROM db.main.t", "window"),
    ("SELECT SUM(amount) AS m FROM db.main.t WHERE amount IN (SELECT amount FROM db.main.u)",
     "subquery"),
])
def test_outside_the_grammar_is_a_named_hypothesis(sql, reason):
    registry = SchemaRegistry(CAT)
    canon = canonicalize(sql, registry, "duckdb")
    assert canon.level != CanonLevel.SUPPORTED and not canon.rules
    assert any(g.startswith(f"outside the supported grammar ({POLICY_ID})") and reason in g
               for g in canon.gaps), canon.gaps
    for fp in extract_fingerprints(canon, "db.main.m", Provenance("t", "m"), "duckdb",
                                   registry):
        assert fp.canon_level not in PROOF_LEVELS and not fp.rules


# ------------------------------------------- hypotheses never decide anything

def _scan(tmp_path, models: dict[str, str], metrics: str | None = None):
    d = tmp_path / "models"
    d.mkdir(exist_ok=True)
    for name, sql in models.items():
        (d / f"{name}.sql").write_text(f"-- semantix: relation=db.main.{name}\n{sql}\n")
    (tmp_path / "catalog.yml").write_text(
        "db:\n  main:\n    t:\n      amount: DOUBLE\n      status: VARCHAR\n"
        "    u:\n      id: INTEGER\n      amount: DOUBLE\n")
    kw = {}
    if metrics:
        (tmp_path / "metrics.yml").write_text(metrics)
        kw["metrics_path"] = str(tmp_path / "metrics.yml")
    return run_scan(sql_dir=str(d), catalog_path=str(tmp_path / "catalog.yml"),
                    db_path=str(tmp_path / "s.db"), **kw)


def test_identical_hypotheses_never_certify_or_alias(tmp_path):
    joined = ("SELECT SUM(t.amount) AS {} FROM db.main.t t "
              "JOIN db.main.u u ON t.amount = u.amount")
    result, store = _scan(tmp_path, {"a": joined.format("rev"), "b": joined.format("revenue")},
                          metrics="version: 1\nmetrics:\n  - name: rev\n    model: db.main.t\n"
                                  "    expr: \"sum(amount)\"\n")
    store.close()
    kinds = {v.verdict_type for v in result.verdicts}
    assert VerdictType.CERTIFIED not in kinds and VerdictType.ALIASING not in kinds


def test_positive_verdicts_record_policy_rules_and_assumptions(tmp_path):
    result, store = _scan(
        tmp_path, {"m": "WITH s AS (SELECT * FROM db.main.t) SELECT SUM(amount) AS m FROM s "
                        "WHERE (status = 'done')"},
        metrics="version: 1\nmetrics:\n  - name: m\n    model: db.main.t\n"
                "    expr: \"sum(amount)\"\n    filters: [\"status = 'done'\"]\n")
    (v,) = [v for v in result.verdicts if v.verdict_type is VerdictType.CERTIFIED]
    proof = v.evidence["proof"]
    assert proof["policy"] == POLICY_ID
    assert {"inline", "star", "parens", "conjuncts"} <= set(proof["rules"])
    assert proof["assumptions"] == list(ASSUMPTIONS)
    assert result.stats()["proof_policy"] == POLICY_ID
    receipt = store.issue_receipt("m")
    store.close()
    assert receipt.status == "CERTIFIED" and receipt.proof_policy == POLICY_ID
    assert receipt.verdicts[0]["proof"]["policy"] == POLICY_ID


# ------------------------------------------- requalification of old evidence

@pytest.fixture()
def legacy(tmp_path):
    """A certified scan, then rewritten to look like one made before policy
    versioning — same files, same hashes, old policy."""
    result, store = _scan(tmp_path, {"m": "SELECT SUM(amount) AS m FROM db.main.t"},
                          metrics="version: 1\nmetrics:\n  - name: m\n    model: db.main.t\n"
                                  "    expr: \"sum(amount)\"\n")
    assert any(v.verdict_type is VerdictType.CERTIFIED for v in result.verdicts)
    store.close()
    db = tmp_path / "s.db"
    con = sqlite3.connect(db)
    (stats,) = con.execute("SELECT stats_json FROM scans").fetchone()
    stats = json.loads(stats)
    stats.pop("proof_policy")
    con.execute("UPDATE scans SET stats_json = ?", (json.dumps(stats),))
    con.commit()
    con.close()
    estate = {"sql_dir": str(tmp_path / "models"), "metrics_path": str(tmp_path / "metrics.yml")}
    return {"tmp": tmp_path, "db": str(db), "estate": estate}


def test_unchanged_files_do_not_carry_an_old_certification_forward(legacy):
    store = Store(legacy["db"])
    try:
        live = check_freshness(store, **legacy["estate"])
        assert live["status"] == "stale"
        assert live["proof_policy"] == {"scan": None, "current": POLICY_ID}
        assert "rescan to recertify" in live["note"]
        receipt = store.issue_receipt("m", freshness=live)
    finally:
        store.close()
    assert receipt.status == "UNRESOLVED" and receipt.proof_policy is None
    assert any("rescan to recertify" in d for d in receipt.known_divergences)
    assert all(v["verdict"] != "CERTIFIED" for v in receipt.verdicts)
    assert any(v.get("superseded") == "CERTIFIED" for v in receipt.verdicts)


def test_a_receipt_issued_under_the_old_policy_is_never_current(legacy):
    server = McpServer(legacy["db"], estate=legacy["estate"])
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "attest", "arguments": {"concept": "m"}}})
    receipt = json.loads(resp["result"]["content"][0]["text"])
    a = assess_receipt(receipt, {"status": "fresh", "scan_id": receipt["scan_id"]})
    assert a.current_use is False
    assert any("rescan and re-attest" in w for w in a.warnings)


def test_a_rescan_recertifies(legacy):
    run_scan(sql_dir=legacy["estate"]["sql_dir"], metrics_path=legacy["estate"]["metrics_path"],
             catalog_path=str(legacy["tmp"] / "catalog.yml"), db_path=legacy["db"])[1].close()
    store = Store(legacy["db"])
    try:
        live = check_freshness(store, **legacy["estate"])
        receipt = store.issue_receipt("m", freshness=live)
    finally:
        store.close()
    assert live["status"] == "fresh"
    assert receipt.status == "CERTIFIED" and receipt.proof_policy == POLICY_ID
    a = assess_receipt(receipt.as_dict(), {**live, "scan_id": receipt.scan_id})
    assert a.current_use is True


# ----------------------------------------------- the monitor on hypotheses

def test_monitor_never_conforms_or_accuses_from_a_hypothesis(tmp_path):
    from .test_monitor_conformance import GOVERNED, _rows, codes, only, run

    exact = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
             f"WHERE status = 'completed' LIMIT 5")
    f = only(run(tmp_path / "exact", _rows(exact)))
    assert f["kind"] == "unverifiable" and codes(f) == ["shape_unsupported"]
    assert "outside the supported grammar" in f["reasons"][0]["detail"]

    dropped = f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders LIMIT 5"
    result = run(tmp_path / "dropped", _rows(dropped))
    f = only(result)
    assert f["kind"] == "unverifiable" and result["funnel"]["violations"] == 0
    assert f["suspected"] and all(s.startswith("under the optimizer's reading (a hypothesis)")
                                  for s in f["suspected"])


def test_monitor_judges_an_inlined_cte_as_the_query_it_is(tmp_path):
    from .test_monitor_conformance import GOVERNED, _rows, only, run

    q = (f"WITH o AS (SELECT * FROM main.finance.orders WHERE status = 'completed') "
         f"SELECT channel, {GOVERNED} AS net_revenue FROM o WHERE country = 'US' "
         f"GROUP BY channel")
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "conforming_use", f
