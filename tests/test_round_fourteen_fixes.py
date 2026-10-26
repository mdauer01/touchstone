"""Round fourteen: external review of 0d613af across the trust guarantees.

1. ORDER BY named another projection by alias or ordinal; the core recorded
   the name, not the expression, so a bound measure whose ordering key
   changed (revenue 100 vs 900) kept its proof-grade fingerprint.
2. Plain DISTINCT recorded a bare tag, not the projection tuple that decides
   which values survive (false ALIASING; exact "conforming" in the monitor).
3. A receipt issued fresh stayed valid for CURRENT use after the estate it
   certified changed: verification trusted the freshness recorded at issue.
4. Two approved metrics with identical definitions: a reference to one under
   the other's name was a violation, on names alone.
5. Monitoring granted positive verdicts under an unqualified parser.
6. The "exact" oracle comparator rounded decimals to 28 significant digits.
"""

from __future__ import annotations

import decimal
import json

import pytest

from semantix.adapters.catalog import load_catalog
from semantix.adapters.sql_dir import load_sql_dir
from semantix.cli import main
from semantix.engine import run_scan
from semantix.extraction.canonicalize import canonicalize
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.mcp import McpServer
from semantix.models import PROOF_LEVELS, Provenance, VerdictType
from semantix.services.monitor import build_registry, monitor_history

from .policy import engine_kinds, engine_codes, engine_funnel, engine_kind
from .test_monitor_conformance import GOVERNED, VIEW, _rows, codes, run
from .test_round_ten_fixes import headline_consistent

duckdb = pytest.importorskip("duckdb")

CAT = {"raw.orders": {"amount": "DOUBLE", "refund": "DOUBLE", "country": "VARCHAR"}}
SEED = ("CREATE SCHEMA raw; CREATE TABLE raw.orders(amount DOUBLE, refund DOUBLE, "
        "country VARCHAR); INSERT INTO raw.orders VALUES (100, 1, 'US'), (900, 9, 'CA')")
CATALOG_YML = "raw:\n  orders:\n    amount: DOUBLE\n    refund: DOUBLE\n    country: VARCHAR\n"


def _fp(sql, name="revenue"):
    registry = SchemaRegistry(CAT)
    registry.set_estate_depth(["raw.orders"])
    canon = canonicalize(sql, registry, "duckdb")
    (fp,) = [f for f in extract_fingerprints(canon, "x", Provenance("q", "x"), "duckdb",
                                             registry) if f.concept_candidate == name]
    return fp


def _executes_differently(a, b):
    con = duckdb.connect()
    con.execute(SEED)
    return sorted(con.execute(a).fetchall()) != sorted(con.execute(b).fetchall())


# -------------------------------------------- 1. ORDER BY names a projection

ORDER_PAIRS = [
    ("SELECT SUM(amount) AS revenue, SUM(refund) AS sort_key FROM raw.orders "
     "GROUP BY country ORDER BY sort_key LIMIT 1",
     "SELECT SUM(amount) AS revenue, -SUM(refund) AS sort_key FROM raw.orders "
     "GROUP BY country ORDER BY sort_key LIMIT 1"),
    ("SELECT SUM(amount) AS revenue, SUM(refund) AS k FROM raw.orders "
     "GROUP BY country ORDER BY 2 LIMIT 1",
     "SELECT SUM(amount) AS revenue, -SUM(refund) AS k FROM raw.orders "
     "GROUP BY country ORDER BY 2 LIMIT 1"),
    ("SELECT DISTINCT ON (country) country, SUM(amount) AS revenue, SUM(refund) AS k "
     "FROM raw.orders GROUP BY country ORDER BY country, k",
     "SELECT DISTINCT ON (country) country, SUM(amount) AS revenue, -SUM(refund) AS k "
     "FROM raw.orders GROUP BY country ORDER BY country, k"),
]


@pytest.mark.parametrize("a, b", ORDER_PAIRS)
def test_ordering_through_another_projection_is_part_of_the_core(a, b):
    fa, fb = _fp(a), _fp(b)
    assert fa.core.hash() != fb.core.hash()
    if "LIMIT" in a:
        assert _executes_differently(a, b)


def test_alias_inside_an_order_by_expression_withholds_proof_grade():
    fp = _fp("SELECT SUM(amount) AS revenue, SUM(refund) AS k FROM raw.orders "
             "GROUP BY country ORDER BY k + 1 LIMIT 1")
    assert fp.canon_level not in PROOF_LEVELS
    assert any("projection alias inside an expression" in g for g in fp.gaps)


def test_order_without_a_bound_is_still_neutral():
    a = ("SELECT SUM(amount) AS revenue, SUM(refund) AS k FROM raw.orders "
         "GROUP BY country ORDER BY k")
    assert _fp(a).core.hash() == _fp(a.replace("SUM(refund)", "-SUM(refund)")).core.hash()


# --------------------------------------------------- 2. plain DISTINCT tuple

DISTINCT_A = "SELECT DISTINCT SUM(amount) AS revenue FROM raw.orders GROUP BY country"
DISTINCT_B = "SELECT DISTINCT country, SUM(amount) AS revenue FROM raw.orders GROUP BY country"


def test_distinct_tuple_is_part_of_the_core():
    assert _fp(DISTINCT_A).core.hash() != _fp(DISTINCT_B).core.hash()
    con = duckdb.connect()
    con.execute("CREATE SCHEMA raw; CREATE TABLE raw.orders(amount DOUBLE, refund DOUBLE, "
                "country VARCHAR); INSERT INTO raw.orders VALUES (10, 0, 'US'), (10, 0, 'CA')")
    assert len(con.execute(DISTINCT_A).fetchall()) == 1
    assert len(con.execute(DISTINCT_B).fetchall()) == 2
    # Names, order and repetition of the tuple are not part of it.
    assert _fp(DISTINCT_B).core.hash() == _fp(
        "SELECT DISTINCT SUM(amount) AS revenue, country, country AS c2 FROM raw.orders "
        "GROUP BY country", "revenue").core.hash()


def _estate(tmp_path, models: dict[str, str]):
    d = tmp_path / "models"
    d.mkdir()
    for name, sql in models.items():
        (d / f"{name}.sql").write_text(f"-- semantix: relation=analytics.{name}\n{sql}\n")
    (tmp_path / "catalog.yml").write_text(CATALOG_YML)
    return d


def test_scan_no_longer_reports_aliasing_for_different_dedup(tmp_path):
    models = _estate(tmp_path, {
        "a": DISTINCT_A,
        "b": DISTINCT_B.replace("AS revenue", "AS rev_by_country")})
    result, store = run_scan(sql_dir=str(models), catalog_path=str(tmp_path / "catalog.yml"),
                             db_path=str(tmp_path / "s.db"))
    store.close()
    assert not [v for v in result.verdicts if v.verdict_type is VerdictType.ALIASING]
    # The policy shows no ALIASING anyway; pin the ENGINE's judgment.
    assert VerdictType.ALIASING not in engine_kinds(result.verdicts)


@pytest.mark.parametrize("authoritative, observed", [
    (ORDER_PAIRS[0][0], ORDER_PAIRS[0][1]),
    (DISTINCT_A, DISTINCT_B),
])
def test_monitor_does_not_call_the_changed_query_conforming(tmp_path, authoritative, observed):
    models = _estate(tmp_path, {"m": authoritative})
    _, store = run_scan(sql_dir=str(models), catalog_path=str(tmp_path / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"))
    registry = build_registry(load_catalog(tmp_path / "catalog.yml"),
                              load_sql_dir(models, "duckdb")[0], "duckdb")
    result = monitor_history(store, observed + ";\n", registry, "duckdb", min_runs=1,
                             authority_systems={"sql"})
    store.close()
    # Ordered LIMIT and plain DISTINCT are outside the proof grammar, so the
    # model is no authority; nothing can conform to it — not even as an
    # experimental match.
    assert result["findings"]
    assert all(engine_kind(f) != "conforming_use" for f in result["findings"])
    assert all(f["kind"] != "conforming_use" for f in result["findings"])
    assert engine_funnel(result["funnel"])["conforming"] == 0


# ------------------------------------------------ 3. receipt current reliance

@pytest.fixture()
def certified(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    model = models / "m.sql"
    model.write_text("-- semantix: relation=db.main.m\nSELECT SUM(amount) AS m FROM db.main.t\n")
    (tmp_path / "catalog.yml").write_text("db:\n  main:\n    t:\n      amount: DOUBLE\n")
    (tmp_path / "metrics.yml").write_text(
        "version: 1\nmetrics:\n  - name: m\n    model: db.main.t\n    expr: \"sum(amount)\"\n")
    run_scan(sql_dir=str(models), catalog_path=str(tmp_path / "catalog.yml"),
             metrics_path=str(tmp_path / "metrics.yml"),
             db_path=str(tmp_path / "s.db"))[1].close()
    estate = {"sql_dir": str(models), "metrics_path": str(tmp_path / "metrics.yml"),
              "catalog_path": str(tmp_path / "catalog.yml")}
    return {"tmp": tmp_path, "db": str(tmp_path / "s.db"), "model": model, "estate": estate}


def _call(server, name, args):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": args}})
    return json.loads(resp["result"]["content"][0]["text"])


def _edit(c):
    c["model"].write_text(
        "-- semantix: relation=db.main.m\nSELECT SUM(amount) * 2 AS m FROM db.main.t\n")


def test_fresh_receipt_is_not_current_after_the_estate_changes_mcp(certified):
    server = McpServer(certified["db"], estate=certified["estate"])
    receipt = _call(server, "attest", {"concept": "m"})
    # The engine certified m; under the conclusions policy the receipt states
    # that only as an experimental comparison.
    assert receipt["status"] == "UNRESOLVED" and receipt["freshness"]["status"] == "fresh"
    (stated,) = receipt["verdicts"]
    assert stated["summary"].startswith(
        "Experimental comparison, not a conclusion — certified under the experimental rules")
    fresh = _call(server, "verify_receipt", {"receipt": receipt})
    # Fresh: authentic over an unchanged estate — and still never usable now
    # (positive verdicts are suspended), so never current.
    assert fresh["signature_authentic"] is True and fresh["estate_unchanged"] is True
    assert fresh["semantic_claim_usable"] is False and fresh["current_use"] is False
    _edit(certified)
    verdict = _call(server, "verify_receipt", {"receipt": receipt})
    assert verdict["current_use"] is False and verdict["trusted"] is False
    # The estate answer is what the edit changes.
    assert verdict["signature_authentic"] is True and verdict["estate_unchanged"] is False
    assert verdict["live_freshness"]["status"] == "stale"
    # Offline (a server without estate sources): signature and history only.
    offline = _call(McpServer(certified["db"]), "verify_receipt", {"receipt": receipt})
    assert offline["historically_valid"] is True and offline["current_use"] is False
    assert offline["estate_unchanged"] is None


def test_fresh_receipt_is_not_current_after_the_estate_changes_cli(certified, capsys):
    server = McpServer(certified["db"], estate=certified["estate"])
    path = certified["tmp"] / "r.json"
    path.write_text(json.dumps(_call(server, "attest", {"concept": "m"})))
    live = ["--db", certified["db"], "--sql-dir", certified["estate"]["sql_dir"],
            "--metrics", certified["estate"]["metrics_path"],
            "--catalog", certified["estate"]["catalog_path"]]
    capsys.readouterr()
    # Positive verdicts are suspended: --require-current fails even over the
    # unchanged estate — but the estate answer is still yes.
    assert main(["verify-receipt", str(path), "--require-current", *live]) == 1
    out = capsys.readouterr().out
    assert "signature authentic: yes · estate unchanged: yes · claim usable: no" in out
    assert main(["verify-receipt", str(path), "--require-current"]) == 1   # offline
    assert "estate unchanged: not checked" in capsys.readouterr().out
    _edit(certified)
    capsys.readouterr()
    assert main(["verify-receipt", str(path), "--require-current", *live]) == 1
    out = capsys.readouterr().out
    assert "live re-check against scan" in out
    assert "signature authentic: yes · estate unchanged: no · claim usable: no" in out


def test_live_check_is_bound_to_the_receipts_own_scan(certified):
    """After the edit, a RESCAN makes the latest snapshot match the estate
    again — but the receipt certified the old snapshot. Checking against the
    latest scan would call it current."""
    server = McpServer(certified["db"], estate=certified["estate"])
    receipt = _call(server, "attest", {"concept": "m"})
    _edit(certified)
    run_scan(sql_dir=certified["estate"]["sql_dir"],
             metrics_path=certified["estate"]["metrics_path"],
             catalog_path=str(certified["tmp"] / "catalog.yml"),
             db_path=certified["db"])[1].close()
    verdict = _call(server, "verify_receipt", {"receipt": receipt})
    assert verdict["live_freshness"]["scan_id"] == receipt["scan_id"]
    assert verdict["current_use"] is False
    # current_use is always False while positive verdicts are suspended; the
    # binding shows in the estate answer, which the latest scan would call yes.
    assert verdict["estate_unchanged"] is False


# --------------------------------------- 4. identical definitions, two names

EQUIV = VIEW.format(extra="") + ("  - name: total_revenue\n"
                                 "    expr: SUM(amount) - SUM(refund_amount)\n"
                                 "  - name: gross_revenue\n    expr: SUM(amount)\n")


@pytest.mark.parametrize("sql, kind", [
    ("SELECT MEASURE(net_revenue) AS total_revenue FROM main.finance.revenue_mv",
     "conforming_use"),
    ("SELECT channel, MEASURE(net_revenue) AS total_revenue FROM main.finance.revenue_mv "
     "WHERE country = 'US' GROUP BY channel", "conforming_use"),
    # A transformation under another approved name is not decided (round
    # fifteen: it can compute that metric exactly).
    ("SELECT MEASURE(net_revenue) * 2 AS total_revenue FROM main.finance.revenue_mv",
     "unverifiable"),
    ("SELECT MEASURE(net_revenue) AS gross_revenue FROM main.finance.revenue_mv",
     "definition_violation"),
    (f"SELECT {GOVERNED} AS total_revenue FROM main.finance.orders "
     f"WHERE status = 'completed'", "conforming_use"),
])
def test_names_alone_do_not_establish_different_definitions(tmp_path, sql, kind):
    result = run(tmp_path, _rows(sql), view_text=EQUIV)
    (f,) = result["findings"]
    assert engine_kind(f) == kind
    if kind == "conforming_use":
        assert f["concept"] == "total_revenue"
        # Shown as an experimental match, not a conclusion.
        assert f["kind"] == "unverifiable" and codes(f) == ["positive_suspended"]
    elif kind == "definition_violation":
        assert f["kind"] == "suspected_difference"
    else:
        assert f["kind"] == kind
    headline_consistent(result)


# ------------------------------------------------------ 5. toolchain gate

def _monitor_with(tmp_path, monkeypatch, scan_qualified: bool, run_qualified: bool, sql):
    import semantix.toolchain as tc
    from semantix.services.history import read_history
    from semantix.services.pilot import load_pilot

    from .test_monitor_conformance import make_pilot

    monkeypatch.delenv("SEMANTIX_QUALIFY_SQLGLOT", raising=False)
    root, models = make_pilot(tmp_path, _rows(sql))
    real = tc.QUALIFIED_SQLGLOT
    monkeypatch.setattr(tc, "QUALIFIED_SQLGLOT", real if scan_qualified else frozenset())
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect="databricks")
    monkeypatch.setattr(tc, "QUALIFIED_SQLGLOT", real if run_qualified else frozenset())
    pilot = load_pilot(root, "databricks")
    registry = build_registry(load_catalog(root / "catalog.yml"),
                              load_sql_dir(models, "databricks")[0], "databricks")
    result = monitor_history(store, read_history(pilot.history), registry, "databricks",
                             min_runs=1, pilot=pilot)
    store.close()
    return result


CONFORMING_QUERIES = [
    f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
    f"WHERE status = 'completed' AND country = 'US'",
    "SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv",
]


@pytest.mark.parametrize("sql", CONFORMING_QUERIES)
@pytest.mark.parametrize("scan_q, run_q", [(True, False), (False, True), (False, False)])
def test_no_positive_monitor_verdict_from_an_unqualified_parser(
        tmp_path, monkeypatch, sql, scan_q, run_q):
    result = _monitor_with(tmp_path, monkeypatch, scan_q, run_q, sql)
    (f,) = result["findings"]
    assert f["kind"] == "unverifiable" and codes(f) == ["toolchain_unqualified"]
    # Withheld by the toolchain gate itself, not merely suspended by policy.
    assert engine_kind(f) == "unverifiable" and engine_codes(f) == ["toolchain_unqualified"]
    assert f["withheld_kind"] == "conforming_use"
    assert f["withheld"].startswith("experimental at most, not a conclusion")
    assert result["toolchain"]["positive_verdicts_withheld"] is True
    assert engine_funnel(result["funnel"])["conforming"] == 0
    headline_consistent(result)


@pytest.mark.parametrize("sql", CONFORMING_QUERIES)
def test_qualified_parsers_still_conform(tmp_path, monkeypatch, sql):
    result = _monitor_with(tmp_path, monkeypatch, True, True, sql)
    f = result["findings"][0]
    assert engine_kind(f) == "conforming_use"
    # Reported as an experimental match while positive verdicts are suspended.
    assert f["kind"] == "unverifiable" and codes(f) == ["positive_suspended"]


# ------------------------------------------------- 6. exact comparator

def test_comparator_is_exact_beyond_28_significant_digits():
    from semantix.oracle import _norm, _run, _same

    D = decimal.Decimal

    def same(a, b):
        return _same(("rows", [(_norm(a),)]), ("rows", [(_norm(b),)]))

    assert not same(D("1.0000000000000000000000000001"), D("1.0000000000000000000000000002"))
    assert not same(D("12345678901234567890123456789012345678"),
                    D("12345678901234567890123456789012345679"))
    assert not same(10 ** 37 + 1, 10 ** 37 + 2)
    assert same(D("1.10"), D("1.1")) and same(5, 5.0) and same(0.0, -0.0)
    assert not same("1", 1)
    con = duckdb.connect()
    a = _run(con, "SELECT CAST('1.0000000000000000000000000001' AS DECIMAL(38,28))")
    b = _run(con, "SELECT CAST('1.0000000000000000000000000002' AS DECIMAL(38,28))")
    assert not _same(a, b)
