"""Round sixteen: the second sweep's monitor and end-to-end findings.

Resolved by impact. Session context is reconstructed only from ordered
evidence; when the history cannot establish it, the affected query is
uncertain, never judged against a guessed table. Identity is attributed to
the statement it was written on. Everything here would otherwise be a false
conforming verdict, a false accusation, or a misattributed one.
"""

from __future__ import annotations

from .policy import engine_funnel, engine_kind
from .test_monitor_conformance import GOVERNED, T_IN, codes, only, run

Q_SHORT = f"SELECT {GOVERNED} AS net_revenue FROM orders WHERE status = 'completed'"
Q_FULL = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
          f"WHERE status = 'completed'")
CTX = {"catalog": "main", "schema": "finance"}


def _row(i, text, start_time=T_IN, **extra):
    return {"statement_id": f"s{i}", "statement_text": text, "start_time": start_time,
            "executed_by": "ana@example.com", **extra}


# ------------------------------------------------ session state (#36 #43 #49 #51 #55)

def test_an_unparseable_statement_before_a_short_name_is_uncertain(tmp_path):
    """#36: it may have been the CREATE TEMP VIEW that shadows `orders`."""
    (f,) = run(tmp_path, [_row(0, f"CREATE TEMP VIEW orders AS SELECT ??? ; {Q_SHORT}",
                                **CTX)])["findings"]
    assert f["kind"] == "unverifiable" and codes(f) == ["context_unresolved"]
    assert engine_kind(f) == "unverifiable"
    assert "unparseable statement" in f["reasons"][0]["detail"]


def test_a_temp_view_in_an_earlier_execution_of_the_session_shadows(tmp_path):
    """#43 / #51: the view is created in one execution, read in the next."""
    rows = [_row(0, "CREATE TEMPORARY VIEW orders AS SELECT * FROM main.finance.orders "
                    "WHERE country = 'US'", start_time="2026-10-02T09:00:00Z",
                 session_id="sess-1", **CTX),
            _row(1, Q_SHORT, session_id="sess-1", **CTX)]
    (f,) = run(tmp_path, rows)["findings"]
    assert f["kind"] == "unverifiable" and codes(f) == ["context_unresolved"]
    assert engine_kind(f) == "unverifiable"
    assert "another execution of this session" in f["reasons"][0]["detail"]


def test_another_sessions_temp_view_changes_nothing(tmp_path):
    rows = [_row(0, "CREATE TEMPORARY VIEW orders AS SELECT 1 AS amount",
                 start_time="2026-10-02T09:00:00Z", session_id="sess-1", **CTX),
            _row(1, Q_SHORT, session_id="sess-2", **CTX)]
    f = only(run(tmp_path, rows))
    assert engine_kind(f) == "conforming_use"
    assert f["kind"] == "unverifiable" and codes(f) == ["positive_suspended"]


def test_an_unidentified_session_is_uncertain_unless_provably_later(tmp_path):
    earlier = [_row(0, "CREATE TEMPORARY VIEW orders AS SELECT 1 AS amount",
                    start_time="2026-10-02T09:00:00Z", **CTX), _row(1, Q_SHORT, **CTX)]
    (f,) = run(tmp_path / "earlier", earlier)["findings"]
    assert codes(f) == ["context_unresolved"]
    assert "session is not recorded" in f["reasons"][0]["detail"]
    later = [_row(0, "CREATE TEMPORARY VIEW orders AS SELECT 1 AS amount",
                  start_time="2026-10-02T11:00:00Z", **CTX), _row(1, Q_SHORT, **CTX)]
    assert engine_kind(only(run(tmp_path / "later", later))) == "conforming_use"


def test_fully_qualified_names_do_not_depend_on_use_or_set(tmp_path):
    """#49: one leading USE made every later statement unverifiable."""
    f = only(run(tmp_path, [_row(0, f"USE SCHEMA staging; SET x = 1; {Q_FULL}")]))
    assert engine_kind(f) == "conforming_use"
    assert f["kind"] == "unverifiable" and codes(f) == ["positive_suspended"]


def test_statements_that_resolve_nothing_change_nothing(tmp_path):
    """#55: INSERT / SHOW / DESCRIBE before a query are not session changes."""
    text = (f"INSERT INTO main.finance.audit SELECT 1; SHOW TABLES; "
            f"DESCRIBE TABLE main.finance.orders; {Q_SHORT}")
    result = run(tmp_path, [_row(0, text, **CTX)])
    assert engine_kind(only(result)) == "conforming_use"
    assert result["history"]["statements_after_session_change"] == 0


def test_a_created_fully_qualified_name_is_affected_only_where_read(tmp_path):
    text = ("CREATE OR REPLACE VIEW main.finance.orders AS SELECT * FROM "
            f"main.finance.orders_v2; {Q_FULL}")
    (f,) = run(tmp_path, [_row(0, text)])["findings"]
    assert codes(f) == ["context_unresolved"]
    other = (f"CREATE OR REPLACE VIEW main.finance.something_else AS SELECT 1; {Q_FULL}")
    assert engine_kind(only(run(tmp_path / "other", [_row(0, other)]))) == "conforming_use"


def test_a_sql_log_orders_statements_but_does_not_identify_sessions(tmp_path):
    """A bare .sql log: order known, sessions not — a USE earlier in it is
    possible context, never reconstructed."""
    from touchstone.adapters.catalog import load_catalog
    from touchstone.adapters.sql_dir import load_sql_dir
    from touchstone.engine import run_scan
    from touchstone.services.history import from_sql_text
    from touchstone.services.monitor import build_registry, monitor_history
    from touchstone.services.pilot import load_pilot

    from .test_monitor_conformance import make_pilot

    # A bare log carries no start times: compared against current definitions.
    root, models = make_pilot(tmp_path, [_row(0, "SELECT 1")], compare="current",
                              applies=None)
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect="databricks")
    try:
        pilot = load_pilot(root, "databricks")
        registry = build_registry(load_catalog(root / "catalog.yml"),
                                  load_sql_dir(models, "databricks")[0], "databricks")
        result = monitor_history(store, from_sql_text(f"USE main.finance;\n{Q_SHORT};\n"
                                                      f"{Q_FULL};\n"),
                                 registry, "databricks", min_runs=1, pilot=pilot)
    finally:
        store.close()
    kinds = sorted(engine_kind(f) for f in result["findings"])
    assert result["history"]["statements_with_reconstructed_context"] == 0
    assert "conforming_use" in kinds   # the fully qualified one
    assert result["history"]["statements_after_session_change"] == 1


# ------------------------------------------------------- attribution (#37 #56 #48)

def test_a_trailing_tag_names_its_own_statement(tmp_path):
    """#37 / #56: `…; -- touchstone: metric=x` was applied to the NEXT one."""
    gross = ("SELECT SUM(amount) AS gross FROM main.finance.orders "
             "WHERE status = 'completed'")
    text = f"{gross}; -- touchstone: metric=gross_revenue\n{Q_FULL}"
    result = run(tmp_path, [_row(0, text)])
    net = only(result)
    assert engine_kind(net) == "conforming_use" and net["identity_source"] != "tag"


def test_a_json_query_tag_is_identity(tmp_path):
    """#48: `{"metric": "net_revenue"}` was silently ignored."""
    q = ("SELECT SUM(amount) - SUM(refund_amount) AS rev FROM main.finance.orders "
         "WHERE status = 'completed'")
    f = only(run(tmp_path, [_row(0, q, query_tag='{"metric": "net_revenue"}')]))
    assert f["identity_source"] == "query_tag"


# ------------------------------------------- receipts and freshness (#50 #52 #53 #54)

import json  # noqa: E402

import pytest  # noqa: E402

from touchstone.cli import main  # noqa: E402
from touchstone.engine import run_scan  # noqa: E402
from touchstone.mcp import McpServer  # noqa: E402
from touchstone.models import Receipt  # noqa: E402
from touchstone.services.freshness import check_freshness  # noqa: E402
from touchstone.signing import sign_receipt  # noqa: E402
from touchstone.store import Store  # noqa: E402


@pytest.fixture()
def certified(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    (models / "m.sql").write_text("-- touchstone: relation=db.main.m\n"
                                  "SELECT SUM(amount) AS m FROM db.main.t\n")
    (tmp_path / "catalog.yml").write_text("db:\n  main:\n    t:\n      amount: DOUBLE\n")
    (tmp_path / "metrics.yml").write_text(
        "version: 1\nmetrics:\n  - name: m\n    model: db.main.t\n    expr: \"sum(amount)\"\n")
    run_scan(sql_dir=str(models), catalog_path=str(tmp_path / "catalog.yml"),
             metrics_path=str(tmp_path / "metrics.yml"), db_path=str(tmp_path / "s.db"))[1].close()
    estate = {"sql_dir": str(models), "metrics_path": str(tmp_path / "metrics.yml"),
              "catalog_path": str(tmp_path / "catalog.yml")}
    return {"tmp": tmp_path, "db": str(tmp_path / "s.db"), "models": models, "estate": estate}


def _attest(c):
    server = McpServer(c["db"], estate=c["estate"])
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "attest", "arguments": {"concept": "m"}}})
    return server, json.loads(resp["result"]["content"][0]["text"])


def _verify(server, receipt):
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "verify_receipt",
                                     "arguments": {"receipt": receipt}}})
    return json.loads(resp["result"]["content"][0]["text"])


def test_a_file_that_turns_unreadable_is_drift(certified):
    """#50: the live check ignored files the scan's loaders refuse."""
    server, receipt = _attest(certified)
    # The engine certified m; the receipt states it as an experimental
    # comparison (positive verdicts are suspended), so it is never current —
    # what the unreadable file must change is the estate answer.
    assert receipt["status"] == "UNRESOLVED"
    (stated,) = receipt["verdicts"]
    assert stated["summary"].startswith(
        "Experimental comparison, not a conclusion — certified under the experimental rules")
    fresh = _verify(server, receipt)
    assert fresh["signature_authentic"] is True and fresh["estate_unchanged"] is True
    assert fresh["semantic_claim_usable"] is False and fresh["current_use"] is False
    (certified["models"] / "broken.sql").write_bytes(b"SELECT \xff\xfe FROM x")
    verdict = _verify(server, receipt)
    assert verdict["current_use"] is False
    assert verdict["estate_unchanged"] is False
    assert verdict["live_freshness"]["status"] == "stale"


def test_a_changed_catalog_is_drift_and_an_unchecked_one_is_partial(certified):
    """#52: verdicts depend on the catalog's types."""
    store = Store(certified["db"])
    try:
        no_catalog = {k: v for k, v in certified["estate"].items() if k != "catalog_path"}
        partial = check_freshness(store, **no_catalog)
        assert partial["status"] == "partial" and "catalog" in partial["unchecked_systems"]
        (certified["tmp"] / "catalog.yml").write_text(
            "db:\n  main:\n    t:\n      amount: DECIMAL(18,2)\n")
        stale = check_freshness(store, **certified["estate"])
    finally:
        store.close()
    assert stale["status"] == "stale" and stale["estate_drift"]["changed"] == ["catalog:catalog"]


def test_the_scans_own_dialect_rehashes_the_estate(tmp_path):
    """#53: attest/mcp/publish re-hashed with --dialect's default (duckdb)."""
    views = tmp_path / "views"
    views.mkdir()
    (views / "rev.yaml").write_text(
        "version: 1.1\nsource: main.finance.orders\nmeasures:\n"
        "  - name: revenue\n    expr: SUM(`amount`)\n")
    (tmp_path / "catalog.yml").write_text(
        "main:\n  finance:\n    orders:\n      amount: DOUBLE\n")
    run_scan(uc_metrics_path=str(views), sql_dir=str(tmp_path / "views"),
             catalog_path=str(tmp_path / "catalog.yml"),
             db_path=str(tmp_path / "s.db"), dialect="databricks")[1].close()
    store = Store(str(tmp_path / "s.db"))
    try:
        estate = {"uc_metrics_path": str(views), "catalog_path": str(tmp_path / "catalog.yml")}
        assert check_freshness(store, **estate)["status"] in ("fresh", "partial")
        assert not check_freshness(store, **estate)["estate_drift"]["changed"]
    finally:
        store.close()
    import argparse

    from touchstone.cli import _estate_kwargs
    args = argparse.Namespace(dbt=None, sql_dir=None, views_path=None, metrics=None,
                              agents_schema_path=None, osi_path=None,
                              uc_metrics_path=str(views), catalog_path=None, dialect=None)
    assert "dialect" not in _estate_kwargs(args)


def test_a_receipt_this_store_did_not_issue_is_never_current(certified, tmp_path, capsys):
    """#54: rewritten and re-signed with another key, it verified for CURRENT
    use against this store's estate."""
    server, receipt = _attest(certified)
    forged = {k: v for k, v in receipt.items() if k != "signature"}
    forged["status"] = "CERTIFIED"
    forged["known_divergences"] = []
    other = Store(str(tmp_path / "other.db"))
    try:
        r = Receipt(**{k: forged.get(k) for k in Receipt.__dataclass_fields__
                       if k != "signature"})
        sign_receipt(other, r)
        forged = r.as_dict()
    finally:
        other.close()
    verdict = _verify(server, forged)
    assert verdict["current_use"] is False
    # current_use is always False while positive verdicts are suspended; the
    # forgery shows in the signature answer.
    assert verdict["signature_authentic"] is False
    assert "did not issue" in verdict["live_freshness"]["note"]
    path = tmp_path / "forged.json"
    path.write_text(json.dumps(forged))
    capsys.readouterr()
    assert main(["verify-receipt", str(path), "--require-current", "--db", certified["db"],
                 "--sql-dir", certified["estate"]["sql_dir"],
                 "--metrics", certified["estate"]["metrics_path"],
                 "--catalog", certified["estate"]["catalog_path"]]) == 1
    assert "signature authentic: no" in capsys.readouterr().out


# ------------------------------------------------ judgment (#38 #39 #40 #41 #42 #57)

import hashlib  # noqa: E402

import yaml  # noqa: E402

from touchstone.adapters.catalog import load_catalog  # noqa: E402
from touchstone.adapters.sql_dir import load_sql_dir  # noqa: E402
from touchstone.services.history import read_history  # noqa: E402
from touchstone.services.monitor import build_registry, monitor_history  # noqa: E402
from touchstone.services.pilot import load_pilot  # noqa: E402

from .test_monitor_conformance import CATALOG, MODEL, VIEW, _rows  # noqa: E402

NET_VIEW = VIEW.format(extra="")


@pytest.mark.parametrize("required, query", [
    ("order_date = DATE'2026-01-01'", "order_date = DATE'2026-1-1'"),
    ("order_date = DATE'2026-01-01'", "order_date = TIMESTAMP'2026-01-01 00:00:00'"),
    ("order_date = DATE'2026-01-01'", "order_date IN (DATE'2026-01-01', DATE'2026-1-1')"),
    ("discount = 0.1", "discount = 0.10000000000000001"),
    ("discount = 0", "discount = CAST(-0.4 AS INT)"),
    ("order_date = '2026-01-01'", "order_date = '2026-1-1'"),
])
def test_equal_values_in_other_spellings_are_not_a_replaced_filter(tmp_path, required, query):
    """#38: decided from literal text, equal values were called different."""
    view = NET_VIEW.replace("filter: status = 'completed'", f"filter: {required}")
    q = f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders WHERE {query}"
    f = only(run(tmp_path, _rows(q), view_text=view))
    assert engine_kind(f) != "definition_violation", f
    assert f["kind"] != "suspected_difference", f


def test_distinct_values_are_still_a_replaced_filter(tmp_path):
    q = f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders WHERE status = 'pending'"
    f = only(run(tmp_path, _rows(q)))
    assert engine_kind(f) == "definition_violation"
    assert f["kind"] == "suspected_difference"
    assert codes(f) == ["required_filter_not_preserved"]


@pytest.mark.parametrize("restore", ["status BETWEEN 'completed' AND 'completed'",
                                     "status IN ('completed', NULL)"])
def test_an_or_restored_by_another_condition_is_not_weakened(tmp_path, restore):
    """#39"""
    q = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
         f"WHERE (status = 'completed' OR country = 'US') AND {restore}")
    f = only(run(tmp_path, _rows(q)))
    assert engine_kind(f) != "definition_violation", f
    assert f["kind"] != "suspected_difference", f


def _two_view_run(tmp_path, sql, gross_text):
    root = tmp_path / "pilot"
    v = root / "views"
    v.mkdir(parents=True)
    (root / "catalog.yml").write_text(CATALOG)
    (v / "net.yaml").write_text(NET_VIEW)
    (v / "gross.yaml").write_text(gross_text)
    (root / "history.jsonl").write_text(json.dumps(
        {"statement_id": "x", "statement_text": sql, "start_time": "2026-10-02T10:00:00Z"}))

    def entry(name, f, a, b):
        return {"name": name, "snapshot": f"views/{f}",
                "sha256": hashlib.sha256((v / f).read_bytes()).hexdigest(),
                "applies": {"from": a, "to": b}}
    (root / "pilot.yml").write_text(yaml.safe_dump({
        "history": "history.jsonl", "compare_against": "as_of_execution",
        "catalog": "catalog.yml", "authority": {"system": "uc_metrics", "metric_views": [
            entry("main.finance.revenue_mv", "net.yaml", "2026-09-01T00:00:00Z", None),
            entry("main.finance.gross_mv", "gross.yaml", "2026-09-01T00:00:00Z",
                  "2026-10-01T00:00:00Z")]}}))
    m = tmp_path / "models"
    m.mkdir()
    (m / "m.sql").write_text(MODEL)
    _, store = run_scan(sql_dir=str(m), uc_metrics_path=str(v),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect="databricks")
    try:
        p = load_pilot(root, "databricks")
        reg = build_registry(load_catalog(root / "catalog.yml"),
                             load_sql_dir(m, "databricks")[0], "databricks")
        return monitor_history(store, read_history(p.history), reg, "databricks",
                               min_runs=1, pilot=p)
    finally:
        store.close()


@pytest.mark.parametrize("sql", [
    f"SELECT {GOVERNED} AS gross_revenue FROM main.finance.orders WHERE status = 'completed'",
    "SELECT MEASURE(net_revenue) AS gross_revenue FROM main.finance.revenue_mv",
])
def test_a_metric_named_after_an_unreadable_expired_definition_stays_named(tmp_path, sql):
    """#40: it fell through to 'unnamed' and conformed to net_revenue."""
    unreadable = NET_VIEW.replace(
        "  - name: net_revenue\n    expr: SUM(amount) - SUM(refund_amount)\n",
        "  - name: gross_revenue\n    expr: SUM(amount) / MEASURE(net_revenue)\n")
    result = _two_view_run(tmp_path, sql, unreadable)
    assert engine_funnel(result["funnel"])["conforming"] == 0
    named = [f for f in result["findings"] if f["concept"] == "gross_revenue"]
    assert named and all(engine_kind(f) == "unverifiable" for f in named)
    assert all(f["kind"] == "unverifiable" for f in named)


@pytest.mark.parametrize("projection", ["CAST(MEASURE(net_revenue) AS DOUBLE)",
                                        "-(-MEASURE(net_revenue))", "MEASURE(net_revenue) * 1",
                                        "MEASURE(net_revenue) / 1"])
def test_value_preserving_wrappers_over_measure_are_not_accusations(tmp_path, projection):
    """#41 / #57"""
    q = f"SELECT {projection} AS net_revenue FROM main.finance.revenue_mv"
    f = only(run(tmp_path, _rows(q)))
    assert f["kind"] == "unverifiable" and codes(f) == ["transformation_undecided"]
    assert engine_kind(f) == "unverifiable"
    assert f["suspected"]


def test_a_definite_change_over_measure_is_still_a_violation(tmp_path):
    q = "SELECT MEASURE(net_revenue) + 100 AS net_revenue FROM main.finance.revenue_mv"
    f = only(run(tmp_path, _rows(q)))
    assert engine_kind(f) == "definition_violation"
    assert f["kind"] == "suspected_difference"
    assert codes(f) == ["transformed_governed_measure"]


def test_a_column_alias_list_on_the_metric_view_is_refused(tmp_path):
    """#42: `AS v(…, gross_revenue, net_revenue)` can swap two measures."""
    two = NET_VIEW.replace("measures:\n", "measures:\n  - name: gross_revenue\n"
                                          "    expr: SUM(amount)\n")
    q = ("SELECT MEASURE(net_revenue) AS net_revenue FROM main.finance.revenue_mv "
         "AS v(country, channel, order_date, month, gross_revenue, net_revenue) "
         "WHERE country = 'US'")
    f = only(run(tmp_path, _rows(q), view_text=two))
    assert f["kind"] == "unverifiable"
    assert engine_kind(f) == "unverifiable"
    assert "column alias list" in f["reasons"][0]["detail"]


# ------------------------------------------------------------- P3s (#44 - #63)

from .test_monitor_conformance import D, make_pilot  # noqa: E402


def test_a_measure_with_a_display_name_is_readable(tmp_path):
    """#44: `Net Revenue` made the synthesized declaration unparseable."""
    view = NET_VIEW.replace("  - name: net_revenue\n", "  - name: Net Revenue\n")
    q = (f"SELECT {GOVERNED} AS `Net Revenue` FROM main.finance.orders "
         f"WHERE status = 'completed'")
    result = run(tmp_path, _rows(q), view_text=view)
    assert result["reference"]["excluded_below_proof"] == 0
    assert engine_funnel(result["funnel"])["conforming"] == 1


def test_current_mode_keeps_a_definition_whose_window_ends_later(tmp_path):
    """#45 / #58: a future applies.to read as 'every recorded one has ended'."""
    q = f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders WHERE status = 'completed'"
    result = run(tmp_path, _rows(q), compare="current",
                 applies=("2026-09-01T00:00:00Z", "2999-01-01T00:00:00Z"))
    assert engine_kind(only(result)) == "conforming_use"


def test_a_multi_measure_projection_is_one_use_on_every_path(tmp_path):
    """#46"""
    view = NET_VIEW.replace("measures:\n", "measures:\n  - name: gross_revenue\n"
                                          "    expr: SUM(amount)\n")
    q = ("SELECT MEASURE(gross_revenue) - MEASURE(net_revenue) AS net_revenue "
         "FROM main.finance.revenue_mv LIMIT 10")
    result = run(tmp_path, _rows(q), view_text=view)
    assert result["funnel"]["measure_uses"] == 1


def test_pilot_instants_keep_their_written_precision(tmp_path):
    """#47: unquoted YAML instants were truncated to microseconds; comma
    fractions were dropped."""
    from touchstone.services.pilot import load_pilot, precise_ts

    root, _ = make_pilot(tmp_path, _rows("SELECT 1"))
    manifest = (root / "pilot.yml").read_text()
    (root / "pilot.yml").write_text(manifest.replace(
        "from: '2026-09-01T00:00:00Z'", "from: 2026-09-01T00:00:00.123456789Z"))
    pilot = load_pilot(root, D)
    assert pilot.views[0].applies_from == "2026-09-01T00:00:00.123456789Z"
    assert precise_ts("2026-09-01T00:00:00,5Z") == "2026-09-01T00:00:00.500000000Z"


def test_an_out_of_range_instant_is_unreadable_not_a_crash(tmp_path):
    """#61"""
    rows = _rows(f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
                 f"WHERE status = 'completed'", start_time="9999-12-31T23:59:59-01:00")
    result = run(tmp_path, rows)
    assert result["history"]["executions_with_unreadable_time"] == 1


def test_measure_over_a_cte_names_the_cte_not_the_session(tmp_path):
    """#60"""
    q = ("WITH v AS (SELECT * FROM main.finance.revenue_mv) "
         "SELECT MEASURE(net_revenue) AS net_revenue FROM v")
    (f,) = run(tmp_path, _rows(q, **CTX))["findings"]
    assert codes(f) == ["shape_unsupported"] and "a CTE" in f["reasons"][0]["detail"]


def test_unrecognized_aggregates_are_counted_not_dropped(tmp_path):
    """Measures computed by functions the parser does not recognize used to
    vanish from the denominator; now each is an unverifiable use."""
    q = ("SELECT product(amount) AS net_revenue FROM main.finance.orders "
         "WHERE status = 'completed'")
    result = run(tmp_path, _rows(q))
    assert result["funnel"]["measure_uses"] == 1
    assert result["funnel"]["unverifiable_by_reason"] == {"unrecognized function": 1}


def test_unrecognized_aggregates_count_against_scan_coverage(tmp_path):
    models = tmp_path / "models"
    models.mkdir()
    (models / "a.sql").write_text("-- touchstone: relation=db.main.a\n"
                                  "SELECT SUM(amount) AS s, product(amount) AS p "
                                  "FROM db.main.t\n")
    (tmp_path / "catalog.yml").write_text("db:\n  main:\n    t:\n      amount: DOUBLE\n")
    result, store = run_scan(sql_dir=str(models), catalog_path=str(tmp_path / "catalog.yml"),
                             db_path=str(tmp_path / "s.db"))
    store.close()
    assert len(result.unrecognized) == 1
    assert result.verifiable_measures[1] == len(result.fingerprints) + 1


def test_monitor_json_creates_its_directory_and_prints_session_counts(tmp_path, capsys):
    """#62 / #59"""
    queries = [f"USE SCHEMA staging; SELECT {GOVERNED} AS net_revenue FROM orders "
               f"WHERE status = 'completed'"]
    root, models = make_pilot(tmp_path, _rows(*queries, **CTX))
    db = str(tmp_path / "cli.db")
    assert main(["scan", "--sql-dir", str(models), "--uc-metrics", str(root / "views"),
                 "--catalog", str(root / "catalog.yml"), "--dialect", D, "--db", db]) == 0
    capsys.readouterr()
    out_json = tmp_path / "new" / "dir" / "m.json"
    assert main(["monitor", "--pilot", str(root), "--sql-dir", str(models), "--db", db,
                 "--min-runs", "1", "--json", str(out_json)]) == 0
    assert out_json.is_file()
    assert "resolved from an earlier USE" in capsys.readouterr().out


def test_a_timestamptz_history_column_needs_no_pytz(tmp_path):
    """#63"""
    import duckdb

    from touchstone.warehouse import DuckDBWarehouse

    con = duckdb.connect()
    con.execute("CREATE TABLE h AS SELECT 'SELECT 1' AS statement_text, "
                "TIMESTAMPTZ '2026-10-02 10:00:00+02' AS start_time")
    rows = DuckDBWarehouse(con, "mem").query_records("SELECT * FROM h")
    assert isinstance(rows[0]["start_time"], str) and rows[0]["start_time"].startswith("2026")
