"""Resolution proposals + the shared emitter.

The invariant that makes proposals trustworthy: a declaration emitted from
a fingerprint, scanned back in, CERTIFIES against that fingerprint. Then
the refusals — join graphs, post-aggregate filters, Ossie's filterless
metrics — and the memo's two-direction framing.
"""

from __future__ import annotations

import json

import pytest
import yaml

from semantix.engine import run_scan
from semantix.models import SemanticCore, VerdictType
from semantix.services.emit import emit_declaration
from semantix.services.propose import build_proposals, render_memo, write_proposals

CATALOG = (
    "db:\n  main:\n    orders:\n      amount: DOUBLE\n      refund_amount: DOUBLE\n"
    "      status: VARCHAR\n      order_date: DATE\n      customer_id: INTEGER\n"
    "    customers:\n      customer_id: INTEGER\n      region: VARCHAR\n"
)
MART_SQL = (
    "-- semantix: relation=db.main.rev_mart\n"
    "SELECT DATE_TRUNC('month', order_date) AS month, "
    "SUM(amount - refund_amount) AS net_revenue "
    "FROM db.main.orders WHERE status = 'completed' GROUP BY 1\n"
)


def _estate(tmp_path, metrics_yml: str | None, extra_models: dict | None = None):
    estate = tmp_path / "estate"
    estate.mkdir(exist_ok=True)
    (estate / "rev_mart.sql").write_text(MART_SQL, encoding="utf-8")
    for name, sql in (extra_models or {}).items():
        (estate / f"{name}.sql").write_text(sql, encoding="utf-8")
    catalog = tmp_path / "catalog.yml"
    catalog.write_text(CATALOG, encoding="utf-8")
    kwargs = dict(sql_dir=str(estate), catalog_path=str(catalog),
                  db_path=str(tmp_path / "s.db"))
    if metrics_yml is not None:
        (tmp_path / "metrics.yml").write_text(metrics_yml, encoding="utf-8")
        kwargs["metrics_path"] = str(tmp_path / "metrics.yml")
    return kwargs


def _verdict(result, label, kind):
    return any(v.concept_label == label and v.verdict_type == kind
               for v in result.verdicts)


# ------------------------------------------------------------ round trip


def test_contradiction_proposal_round_trips_to_certified(tmp_path):
    # The ontology forgot the filter and the grain.
    drifted = ("version: 1\nmetrics:\n  - name: net_revenue\n"
               "    model: db.main.orders\n    expr: \"sum(amount - refund_amount)\"\n")
    kwargs = _estate(tmp_path, drifted)
    result, store = run_scan(**kwargs)
    assert _verdict(result, "net_revenue", VerdictType.CONTRADICTION)

    proposals = build_proposals(store, fmt="metrics")
    store.close()
    (p,) = proposals["proposals"]
    assert p["kind"] == "resolution" and p["concept"] == "net_revenue"
    (cand,) = p["candidates"]
    assert cand["declaration"] is not None
    assert cand["sites"] == ["db.main.rev_mart"]
    # The code-side direction names exactly what drifted.
    (change,) = cand["code_changes"]
    assert set(change["diff"]) == {"filters", "grain"}

    # Scan again with the proposed declaration in place of the drifted one.
    (tmp_path / "metrics.yml").write_text(cand["declaration"].text, encoding="utf-8")
    result2, store2 = run_scan(**{**kwargs, "db_path": str(tmp_path / "s2.db")})
    store2.close()
    assert _verdict(result2, "net_revenue", VerdictType.CERTIFIED)
    assert not _verdict(result2, "net_revenue", VerdictType.CONTRADICTION)


def test_shadow_promotion_round_trips_to_certified(tmp_path):
    kwargs = _estate(tmp_path, None)  # no ontology at all
    result, store = run_scan(**kwargs)
    assert _verdict(result, "net_revenue", VerdictType.SHADOW_METRIC)
    proposals = build_proposals(store, fmt="metrics")
    store.close()
    (p,) = proposals["proposals"]
    assert p["kind"] == "promotion" and p["declared"] == []
    (cand,) = p["candidates"]
    (tmp_path / "metrics.yml").write_text(cand["declaration"].text, encoding="utf-8")
    result2, store2 = run_scan(**{**kwargs, "metrics_path": str(tmp_path / "metrics.yml"),
                                  "db_path": str(tmp_path / "s2.db")})
    store2.close()
    assert _verdict(result2, "net_revenue", VerdictType.CERTIFIED)


# -------------------------------------------------------------- emitter


def _core(**kw):
    # Cores render relation-qualified (round seven): the fixture must match
    # what canonicalization produces, or the self-check rightly refuses it.
    base = dict(measure_expr='SUM("amount")', filters=(), grain=(),
                join_nodes=("db.main.orders",), join_edges=(),
                source_columns=("db.main.orders.amount",))
    base.update(kw)
    return SemanticCore(**base)


def test_emit_metrics_unqualifies_and_unquotes():
    emitted, reasons = emit_declaration(
        "net_revenue",
        _core(measure_expr='SUM(t0."amount" - t0."refund_amount")',
              filters=('t0."status" = \'completed\'',),
              grain=("DATE_TRUNC('MONTH', t0.\"order_date\")",)),
        "duckdb", "metrics")
    assert not reasons
    doc = yaml.safe_load(emitted.text)
    (entry,) = doc["metrics"]
    assert entry["model"] == "db.main.orders"
    assert entry["expr"] == "SUM(amount - refund_amount)"
    assert entry["filters"] == ["status = 'completed'"]
    assert entry["group_by"] == ["DATE_TRUNC('MONTH', order_date)"]


def _registry():
    from semantix.extraction.registry import SchemaRegistry
    return SchemaRegistry({"db.main.orders": {"amount": "DOUBLE", "status": "VARCHAR",
                                              "order_date": "DATE"}})


def test_emit_uc_metric_view_shape():
    emitted, reasons = emit_declaration(
        "net_revenue", _core(filters=('"status" = \'completed\'',)),
        "duckdb", "uc-metrics", registry=_registry())
    assert not reasons and emitted.verified is True
    doc = yaml.safe_load(emitted.text)
    assert doc["version"] == "1.1" and doc["source"] == "db.main.orders"
    assert "status = 'completed'" in doc["filter"]
    assert doc["measures"] == [{"name": "net_revenue", "expr": "SUM(amount)"}]
    # A metric-view measure has no fixed grain: writing grain as `fields`
    # rescanned as a CONTRADICTION, so a grained core is refused.
    _, reasons = emit_declaration(
        "net_revenue", _core(grain=("DATE_TRUNC('MONTH', t0.\"order_date\")",)),
        "duckdb", "uc-metrics", registry=_registry())
    assert reasons and "no fixed grain" in reasons[0]


def test_emit_ossie_document_and_its_refusal():
    emitted, reasons = emit_declaration("total_revenue", _core(), "duckdb", "ossie",
                                        registry=_registry())
    assert not reasons and emitted.verified is True
    doc = json.loads(emitted.text)
    assert doc["version"] == "0.1.1"
    (model,) = doc["semantic_model"]
    assert model["datasets"] == [{"name": "orders", "source": "db.main.orders"}]
    (metric,) = model["metrics"]
    assert metric["expression"]["dialects"][0]["expression"] == "SUM(orders.amount)"

    _, reasons = emit_declaration(
        "net_revenue", _core(filters=('"status" = \'completed\'',)),
        "duckdb", "ossie", registry=_registry())
    assert reasons and "no filter or grain" in reasons[0]
    # Without a registry there is no self-check, and Ossie/UC transpile.
    _, reasons = emit_declaration("total_revenue", _core(), "duckdb", "ossie")
    assert reasons and "self-check" in reasons[0]


def test_emit_refuses_join_graph_and_tagged_filters():
    _, reasons = emit_declaration(
        "x", _core(join_nodes=("db.main.orders", "db.main.customers"),
                   join_edges=("db.main.orders.customer_id=db.main.customers.customer_id",)),
        "duckdb", "metrics")
    assert reasons and "join graph" in reasons[0]
    _, reasons = emit_declaration(
        "x", _core(filters=('HAVING SUM(t0."amount") > 0',)), "duckdb", "metrics")
    assert reasons and "HAVING" in reasons[0]


def test_emit_unknown_format_raises():
    with pytest.raises(ValueError, match="unknown declaration format"):
        emit_declaration("x", _core(), "duckdb", "lookml")


# ----------------------------------------------------------------- memo


def test_memo_presents_both_directions_and_writes_files(tmp_path):
    drifted = ("version: 1\nmetrics:\n  - name: net_revenue\n"
               "    model: db.main.orders\n    expr: \"sum(amount - refund_amount)\"\n")
    kwargs = _estate(tmp_path, drifted)
    _, store = run_scan(**kwargs)
    proposals = build_proposals(store)
    store.close()
    written = write_proposals(proposals, tmp_path / "out")
    memo = (tmp_path / "out" / "RESOLUTIONS.md").read_text(encoding="utf-8")
    assert "Option A" in memo and "Option B" in memo
    assert "only a human can pick" in memo
    assert "status = 'completed'" in memo
    assert len(written["declarations"]) == 1
    # Full relation + content hash: two sites sharing a table name in
    # different schemas no longer overwrite each other.
    assert "net_revenue__db_main_rev_mart__" in written["declarations"][0]
    assert written["declarations"][0].endswith(".yml")


def test_memo_when_nothing_to_propose(tmp_path):
    kwargs = _estate(tmp_path, (
        "version: 1\nmetrics:\n  - name: net_revenue\n    model: db.main.orders\n"
        "    expr: \"sum(amount - refund_amount)\"\n"
        "    filters: [\"status = 'completed'\"]\n"
        "    group_by: [\"date_trunc('month', order_date)\"]\n"))
    result, store = run_scan(**kwargs)
    assert _verdict(result, "net_revenue", VerdictType.CERTIFIED)
    proposals = build_proposals(store)
    store.close()
    assert proposals["proposals"] == []
    assert "No contradictions or shadow metrics" in render_memo(proposals)


def test_join_backed_shadow_is_refused_with_reason(tmp_path):
    joined = (
        "-- semantix: relation=db.main.regional_mart\n"
        "SELECT c.region, SUM(o.amount) AS regional_revenue "
        "FROM db.main.orders o JOIN db.main.customers c "
        "ON o.customer_id = c.customer_id GROUP BY 1\n")
    kwargs = _estate(tmp_path, None, extra_models={"regional_mart": joined})
    result, store = run_scan(**kwargs)
    assert _verdict(result, "regional_revenue", VerdictType.SHADOW_METRIC)
    proposals = build_proposals(store, concept="regional_revenue")
    store.close()
    (p,) = proposals["proposals"]
    (cand,) = p["candidates"]
    assert cand["declaration"] is None
    assert any("join graph" in r for r in cand["refused"])
