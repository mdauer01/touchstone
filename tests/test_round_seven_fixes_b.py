"""Round seven, part two: adapters, monitor, emitter, proposals, write-back.

Companion to test_round_seven_fixes.py (canonicalization and store identity).
"""

from __future__ import annotations

import json

import pytest
import yaml

from semantix.adapters.ossie import load_ossie
from semantix.adapters.uc_metrics import load_uc_metrics
from semantix.engine import run_scan
from semantix.extraction.registry import SchemaRegistry
from semantix.models import SemanticCore, VerdictType
from semantix.services.emit import emit_declaration
from semantix.services.monitor import build_registry, monitor_history
from semantix.services.propose import build_proposals

CATALOG_YML = ("db:\n  main:\n    orders:\n      amount: DOUBLE\n      refunds: DOUBLE\n"
               "      status: VARCHAR\n      customer_id: INTEGER\n")


def _estate(tmp_path, models: dict[str, str]):
    estate = tmp_path / "estate"
    estate.mkdir(exist_ok=True)
    for name, sql in models.items():
        (estate / f"{name}.sql").write_text(
            f"-- semantix: relation=db.main.{name}\n{sql}\n", encoding="utf-8")
    (tmp_path / "catalog.yml").write_text(CATALOG_YML, encoding="utf-8")
    return dict(sql_dir=str(estate), catalog_path=str(tmp_path / "catalog.yml"),
                db_path=str(tmp_path / "s.db"))


def _verdict(result, label, kind):
    return any(v.concept_label == label and v.verdict_type == kind for v in result.verdicts)


# ------------------------------------------------------------------ adapters


def test_uc_bare_on_join_key_does_not_crash(tmp_path):
    p = tmp_path / "v.yml"
    p.write_text("version: '1.1'\nsource: db.main.orders\n"
                 "joins:\n  - name: customers\n    source: db.main.customers\n"
                 "    on: source.customer_id = customers.id\n"
                 "measures:\n  - name: order_count\n    expr: COUNT(1)\n", encoding="utf-8")
    specs, skipped = load_uc_metrics(p, "duckdb")
    assert specs and not specs[0].untranslatable
    assert any("join" in s["reason"] for s in skipped)


def test_uc_any_placeholder_and_measure_reference_refused(tmp_path):
    p = tmp_path / "v.yml"
    p.write_text("version: '1.1'\nsource: db.main.orders\n"
                 "measures:\n"
                 "  - name: a\n    expr: SUM(IF(status = :UNDECLARED, amount, 0))\n"
                 "  - name: total\n    expr: SUM(amount)\n"
                 "  - name: avg_order\n    expr: MEASURE(total) / COUNT(1)\n"
                 "  - name: composed\n    expr: total / 2\n", encoding="utf-8")
    specs, _ = load_uc_metrics(p, "duckdb")
    by = {s.name: s for s in specs}
    assert "placeholder" in by["a"].untranslatable[0]
    assert "MEASURE" in by["avg_order"].untranslatable[0]
    assert "another measure" in by["composed"].untranslatable[0]
    assert not by["total"].untranslatable


def test_ossie_field_substitution_round_trips_and_refuses_nesting(tmp_path):
    def doc(fields, expr):
        return {"version": "0.1.1", "semantic_model": [{
            "name": "m", "datasets": [{"name": "orders", "source": "db.main.orders",
                                       "fields": fields}],
            "metrics": [{"name": "net_revenue", "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": expr}]}}]}]}
    field = lambda n, e: {"name": n, "expression": {"dialects": [  # noqa: E731
        {"dialect": "ANSI_SQL", "expression": e}]}}
    kwargs = _estate(tmp_path, {"mart": "SELECT SUM(amount - refunds) AS net_revenue "
                                        "FROM db.main.orders"})
    osi = tmp_path / "ok.json"
    osi.write_text(json.dumps(doc([field("net_amount", "amount - refunds")],
                                  "SUM(orders.net_amount)")), encoding="utf-8")
    result, store = run_scan(osi_path=str(osi), **kwargs)
    store.close()
    # The Paren wrapped around the substituted field was a false CONTRADICTION.
    assert _verdict(result, "net_revenue", VerdictType.CERTIFIED)

    nested = tmp_path / "nested.json"
    nested.write_text(json.dumps(doc([field("net", "amount - refunds"),
                                      field("net2", "net * 2")],
                                     "SUM(orders.net2)")), encoding="utf-8")
    specs, _ = load_ossie(nested, "duckdb")
    assert specs[0].untranslatable and "declared field" in specs[0].untranslatable[0]


def test_ossie_source_must_be_a_relation_name(tmp_path):
    p = tmp_path / "d.json"
    p.write_text(json.dumps({"version": "0.1.1", "semantic_model": [{
        "name": "m", "datasets": [{"name": "o", "source": "db.main.orders WHERE status = 'paid'"}],
        "metrics": [{"name": "rev", "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "SUM(o.amount)"}]}}]}]}), encoding="utf-8")
    specs, _ = load_ossie(p, "duckdb")
    assert specs[0].untranslatable and "relation name" in specs[0].untranslatable[0]


def test_ossie_mixed_list_file_raises(tmp_path):
    d = tmp_path / "osi"
    d.mkdir()
    good = {"version": "0.1.1", "semantic_model": [{"name": "m", "datasets": [
        {"name": "o", "source": "db.main.orders"}], "metrics": []}]}
    (d / "mixed.json").write_text(json.dumps([good, {"exported_by": "tool"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="not Ossie documents"):
        load_ossie(d, "duckdb")


def test_sql_dir_nonexistent_path_refused(tmp_path):
    from semantix.adapters.sql_dir import load_sql_dir
    with pytest.raises(ValueError, match="not a directory"):
        load_sql_dir(tmp_path / "modelz", "duckdb")


def test_agents_schema_duckdb_without_tables_refused(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from semantix.adapters.agents_schema import load_agents_schema
    p = tmp_path / "wh.duckdb"
    duckdb.connect(str(p)).close()
    with pytest.raises(ValueError, match="no AGENTS schema tables"):
        load_agents_schema(p)


# ------------------------------------------------------------------- monitor


@pytest.fixture()
def monitored(tmp_path):
    models = {
        "rev_mart": "SELECT SUM(amount - refunds) AS net_revenue FROM db.main.orders "
                    "WHERE status = 'completed'",
        "gross_mart": "SELECT SUM(amount) AS gross_revenue FROM db.main.orders",
    }
    kwargs = _estate(tmp_path, models)
    result, store = run_scan(**kwargs)
    from semantix.adapters.catalog import load_catalog
    from semantix.adapters.sql_dir import load_sql_dir
    registry = build_registry(load_catalog(kwargs["catalog_path"]),
                              load_sql_dir(kwargs["sql_dir"], "duckdb")[0], "duckdb")
    yield store, registry
    store.close()


def _kinds(store, registry, statements, min_runs=2):
    result = monitor_history(store, ";\n".join(statements), registry, "duckdb",
                             min_runs=min_runs)
    return [(f["kind"], f.get("concept")) for f in result["findings"]], result


def test_monitor_reaggregation_over_mart_is_unverifiable(monitored):
    store, registry = monitored
    kinds, _ = _kinds(store, registry,
                      ["SELECT SUM(net_revenue) AS net_revenue FROM db.main.rev_mart"] * 3)
    assert kinds == [("unverifiable", None)]


def test_monitor_other_concepts_name_on_matching_math_is_named_alt(monitored):
    store, registry = monitored
    kinds, _ = _kinds(store, registry,
                      ["SELECT SUM(amount - refunds) AS gross_revenue FROM db.main.orders "
                       "WHERE status = 'completed'"] * 3)
    assert ("named_alt_definition", "gross_revenue") in kinds
    assert ("consistent_use", "net_revenue") in kinds


def test_monitor_mixed_grade_reruns_kept_apart(monitored):
    store, registry = monitored
    clean = ("SELECT SUM(amount - refunds) AS net_revenue FROM db.main.orders "
             "WHERE status = 'completed'")
    sampled = clean.replace("db.main.orders", "db.main.orders TABLESAMPLE 10 PERCENT")
    kinds, result = _kinds(store, registry, [clean] * 5 + [sampled] * 2)
    assert ("consistent_use", "net_revenue") in kinds
    assert ("unverifiable", None) in kinds


def test_monitor_semicolon_in_literal_is_one_statement(monitored):
    store, registry = monitored
    _, result = _kinds(store, registry,
                       ["SELECT SUM(amount) AS gross_revenue FROM db.main.orders "
                        "WHERE status = 'a;b'"] * 2 + ["SELECT SUM((amount FROM broken"])
    assert result["parsed_statements"] == 2 and result["skipped_statements"] == 1
    assert result["statements"] == 3


def test_monitor_nested_measure_counted_as_unread(monitored):
    store, registry = monitored
    _, result = _kinds(store, registry,
                       ["WITH agg AS (SELECT SUM(amount) AS g FROM db.main.orders) "
                        "SELECT g FROM agg"] * 2)
    assert result["statements_with_nested_measures"] == 2
    assert result["statements_without_measures"] == 0


def test_monitor_contested_definition_is_not_consistency(tmp_path):
    kwargs = _estate(tmp_path, {"mart": "SELECT SUM(amount) AS rev FROM db.main.orders"})
    (tmp_path / "metrics.yml").write_text(
        "version: 1\nmetrics:\n  - name: rev\n    model: db.main.orders\n"
        "    expr: \"sum(amount)\"\n    filters: [\"status = 'completed'\"]\n",
        encoding="utf-8")
    result, store = run_scan(metrics_path=str(tmp_path / "metrics.yml"), **kwargs)
    assert _verdict(result, "rev", VerdictType.CONTRADICTION)
    from semantix.adapters.catalog import load_catalog
    from semantix.adapters.sql_dir import load_sql_dir
    registry = build_registry(load_catalog(kwargs["catalog_path"]),
                              load_sql_dir(kwargs["sql_dir"], "duckdb")[0], "duckdb")
    kinds, _ = _kinds(store, registry, ["SELECT SUM(amount) AS rev FROM db.main.orders"] * 2)
    store.close()
    assert kinds == [("contested_use", "rev")]


# ------------------------------------------------------------ emit / propose


def _core(**kw):
    base = dict(measure_expr='SUM(t0."amount")', filters=(), grain=(),
                join_nodes=("db.main.orders",), join_edges=(),
                source_columns=("db.main.orders.amount",))
    base.update(kw)
    return SemanticCore(**base)


def test_emit_refuses_distinct_tag_and_subquery():
    _, reasons = emit_declaration("x", _core(filters=("SELECT DISTINCT",)), "duckdb", "metrics")
    assert reasons and "DISTINCT" in reasons[0]
    _, reasons = emit_declaration(
        "x", _core(measure_expr='SUM(t0."amount") / (SELECT COUNT(*) FROM "db"."main"."orders" AS "o2" '
                                'WHERE "o2"."customer_id" = t0."customer_id")'),
        "duckdb", "metrics")
    assert reasons and "subquery" in reasons[0]


def test_emit_keeps_reserved_words_quoted():
    emitted, reasons = emit_declaration(
        "x", _core(measure_expr='COUNT(t0."from")', source_columns=("db.main.orders.from",)),
        "duckdb", "metrics")
    assert not reasons
    assert '"from"' in yaml.safe_load(emitted.text)["metrics"][0]["expr"]


def test_emit_self_check_marks_verified(tmp_path):
    registry = SchemaRegistry({"db.main.orders": {"amount": "DOUBLE"}})
    emitted, reasons = emit_declaration("total", _core(), "duckdb", "metrics", registry=registry)
    assert not reasons and emitted.verified is True


def test_propose_reports_matching_site_and_rejects_unknown_concept(tmp_path):
    kwargs = _estate(tmp_path, {
        "mart_a": "SELECT SUM(amount) AS rev FROM db.main.orders WHERE status = 'completed'",
        "mart_b": "SELECT SUM(amount) AS rev FROM db.main.orders"})
    (tmp_path / "metrics.yml").write_text(
        "version: 1\nmetrics:\n  - name: rev\n    model: db.main.orders\n"
        "    expr: \"sum(amount)\"\n    filters: [\"status = 'completed'\"]\n",
        encoding="utf-8")
    _, store = run_scan(metrics_path=str(tmp_path / "metrics.yml"), **kwargs)
    proposals = build_proposals(store)
    (p,) = proposals["proposals"]
    matched = [c for c in p["candidates"] if c["matches_declaration"]]
    assert matched and matched[0]["sites"] == ["db.main.mart_a"]
    with pytest.raises(ValueError, match="no concept named"):
        build_proposals(store, concept="nonexistent")
    store.close()


# ------------------------------------------------------------- write-back


def test_publish_refuses_customer_not_null_column_before_deleting(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from semantix.services.writeback import publish_receipts
    kwargs = _estate(tmp_path, {"mart": "SELECT SUM(amount) AS rev FROM db.main.orders"})
    _, store = run_scan(**kwargs)
    wh = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(wh))
    con.execute("CREATE SCHEMA semantix")
    con.execute("CREATE TABLE semantix.verdicts (scan_id VARCHAR, scanned_at TIMESTAMPTZ, "
                "published_at TIMESTAMPTZ, concept VARCHAR, verdict VARCHAR, severity INTEGER, "
                "summary VARCHAR, relation VARCHAR, owner_team VARCHAR NOT NULL)")
    con.execute("INSERT INTO semantix.verdicts VALUES ('manual', now(), now(), 'x', 'CERTIFIED', "
                "5, 's', 'r', 'finance')")
    con.close()
    with pytest.raises(ValueError, match="NOT NULL"):
        publish_receipts(store, str(wh))
    store.close()
    con = duckdb.connect(str(wh))
    assert con.execute("SELECT count(*) FROM semantix.verdicts").fetchone()[0] == 1
    con.close()


def test_diff_markdown_names_lost_certifications():
    from semantix.services.diff import markdown_comment
    md = markdown_comment({"scan_id": "s2", "baseline_scan_id": "s1", "unchanged": 0,
                           "new": [],
                           "resolved": [{"verdict": "CERTIFIED", "concept_label": "gmv",
                                         "severity": 5}]})
    assert "LOST" in md and "resolved by this change" not in md


def test_warehouse_url_grammar(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from semantix.warehouse import connect_warehouse
    db = tmp_path / "wh.db"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE t (x INTEGER)")
    con.close()
    for url in (f"duckdb://{db}", f"duckdb:///{db}", f"duckdb:////{db}"):
        wh = connect_warehouse(url)
        assert wh.query("SELECT count(*) FROM t")[0][0] == 0
        wh.close()
