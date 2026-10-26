"""Round sixteen: the second sweep's monitor and end-to-end findings.

Resolved by impact. Session context is reconstructed only from ordered
evidence; when the history cannot establish it, the affected query is
uncertain, never judged against a guessed table. Identity is attributed to
the statement it was written on. Everything here would otherwise be a false
conforming verdict, a false accusation, or a misattributed one.
"""

from __future__ import annotations

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
    assert "unparseable statement" in f["reasons"][0]["detail"]


def test_a_temp_view_in_an_earlier_execution_of_the_session_shadows(tmp_path):
    """#43 / #51: the view is created in one execution, read in the next."""
    rows = [_row(0, "CREATE TEMPORARY VIEW orders AS SELECT * FROM main.finance.orders "
                    "WHERE country = 'US'", start_time="2026-10-02T09:00:00Z",
                 session_id="sess-1", **CTX),
            _row(1, Q_SHORT, session_id="sess-1", **CTX)]
    (f,) = run(tmp_path, rows)["findings"]
    assert f["kind"] == "unverifiable" and codes(f) == ["context_unresolved"]
    assert "another execution of this session" in f["reasons"][0]["detail"]


def test_another_sessions_temp_view_changes_nothing(tmp_path):
    rows = [_row(0, "CREATE TEMPORARY VIEW orders AS SELECT 1 AS amount",
                 start_time="2026-10-02T09:00:00Z", session_id="sess-1", **CTX),
            _row(1, Q_SHORT, session_id="sess-2", **CTX)]
    assert only(run(tmp_path, rows))["kind"] == "conforming_use"


def test_an_unidentified_session_is_uncertain_unless_provably_later(tmp_path):
    earlier = [_row(0, "CREATE TEMPORARY VIEW orders AS SELECT 1 AS amount",
                    start_time="2026-10-02T09:00:00Z", **CTX), _row(1, Q_SHORT, **CTX)]
    (f,) = run(tmp_path / "earlier", earlier)["findings"]
    assert codes(f) == ["context_unresolved"]
    assert "session is not recorded" in f["reasons"][0]["detail"]
    later = [_row(0, "CREATE TEMPORARY VIEW orders AS SELECT 1 AS amount",
                  start_time="2026-10-02T11:00:00Z", **CTX), _row(1, Q_SHORT, **CTX)]
    assert only(run(tmp_path / "later", later))["kind"] == "conforming_use"


def test_fully_qualified_names_do_not_depend_on_use_or_set(tmp_path):
    """#49: one leading USE made every later statement unverifiable."""
    f = only(run(tmp_path, [_row(0, f"USE SCHEMA staging; SET x = 1; {Q_FULL}")]))
    assert f["kind"] == "conforming_use"


def test_statements_that_resolve_nothing_change_nothing(tmp_path):
    """#55: INSERT / SHOW / DESCRIBE before a query are not session changes."""
    text = (f"INSERT INTO main.finance.audit SELECT 1; SHOW TABLES; "
            f"DESCRIBE TABLE main.finance.orders; {Q_SHORT}")
    result = run(tmp_path, [_row(0, text, **CTX)])
    assert only(result)["kind"] == "conforming_use"
    assert result["history"]["statements_after_session_change"] == 0


def test_a_created_fully_qualified_name_is_affected_only_where_read(tmp_path):
    text = ("CREATE OR REPLACE VIEW main.finance.orders AS SELECT * FROM "
            f"main.finance.orders_v2; {Q_FULL}")
    (f,) = run(tmp_path, [_row(0, text)])["findings"]
    assert codes(f) == ["context_unresolved"]
    other = (f"CREATE OR REPLACE VIEW main.finance.something_else AS SELECT 1; {Q_FULL}")
    assert only(run(tmp_path / "other", [_row(0, other)]))["kind"] == "conforming_use"


def test_a_sql_log_orders_statements_but_does_not_identify_sessions(tmp_path):
    """A bare .sql log: order known, sessions not — a USE earlier in it is
    possible context, never reconstructed."""
    from semantix.services.history import from_sql_text

    from .test_monitor_conformance import make_pilot
    from semantix.adapters.catalog import load_catalog
    from semantix.adapters.sql_dir import load_sql_dir
    from semantix.engine import run_scan
    from semantix.services.monitor import build_registry, monitor_history
    from semantix.services.pilot import load_pilot

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
    kinds = sorted(f["kind"] for f in result["findings"])
    assert result["history"]["statements_with_reconstructed_context"] == 0
    assert "conforming_use" in kinds   # the fully qualified one
    assert result["history"]["statements_after_session_change"] == 1


# ------------------------------------------------------- attribution (#37 #56 #48)

def test_a_trailing_tag_names_its_own_statement(tmp_path):
    """#37 / #56: `…; -- semantix: metric=x` was applied to the NEXT one."""
    gross = (f"SELECT SUM(amount) AS gross FROM main.finance.orders "
             f"WHERE status = 'completed'")
    text = f"{gross}; -- semantix: metric=gross_revenue\n{Q_FULL}"
    result = run(tmp_path, [_row(0, text)])
    net = only(result)
    assert net["kind"] == "conforming_use" and net["identity_source"] != "tag"


def test_a_json_query_tag_is_identity(tmp_path):
    """#48: `{"metric": "net_revenue"}` was silently ignored."""
    q = (f"SELECT SUM(amount) - SUM(refund_amount) AS rev FROM main.finance.orders "
         f"WHERE status = 'completed'")
    f = only(run(tmp_path, [_row(0, q, query_tag='{"metric": "net_revenue"}')]))
    assert f["identity_source"] == "query_tag"
