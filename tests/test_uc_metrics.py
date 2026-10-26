"""Unity Catalog metric-view adapter + information_schema catalog generator.

Refusal surface first — window measures, join references, parameters, field
indirection, unknown keys — then translation, then the end-to-end scan and
the generated catalog feeding it.
"""

from __future__ import annotations

import pytest
import yaml

from semantix.adapters.infoschema import generate_catalog, write_catalog_yaml
from semantix.adapters.uc_metrics import load_uc_metrics
from semantix.engine import run_scan
from semantix.models import VerdictType
from semantix.services.freshness import SOURCE_SYSTEMS, current_estate_shas

from .policy import engine_verdict


def _view(measures, tmp_path, name="revenue_view", **extra):
    doc = {"version": "1.1", "source": "db.main.orders",
           "measures": measures, **extra}
    p = tmp_path / f"{name}.yml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def _expressible(specs):
    return [s for s in specs if s.expr and not s.untranslatable]


# -------------------------------------------------------------- translation


def test_measure_becomes_claim_with_view_filter(tmp_path):
    p = _view([{"name": "total_revenue", "expr": "SUM(amount)"}], tmp_path,
              filter="status = 'F'")
    specs, _ = load_uc_metrics(p, "duckdb")
    (s,) = specs
    assert s.system == "uc_metrics" and s.name == "total_revenue"
    assert s.model == "db.main.orders"
    assert "amount" in s.expr.lower()
    assert s.filters and "status" in s.filters[0]


def test_source_prefix_rewritten_to_table(tmp_path):
    p = _view([{"name": "total_revenue", "expr": "SUM(source.amount)"}], tmp_path)
    specs, _ = load_uc_metrics(p, "duckdb")
    (s,) = _expressible(specs)
    assert "orders.amount" in s.expr.lower()
    assert "source" not in s.expr.lower()


def test_databricks_expression_rendered_for_scan_dialect(tmp_path):
    p = _view([{"name": "big_orders",
                "expr": "SUM(IF(amount > 100, amount, 0))"}], tmp_path)
    specs, _ = load_uc_metrics(p, "duckdb")
    assert _expressible(specs)


def test_identity_field_reference_allowed(tmp_path):
    p = _view([{"name": "total_revenue", "expr": "SUM(amount)"}], tmp_path,
              fields=[{"name": "amount", "expr": "amount"}])
    specs, _ = load_uc_metrics(p, "duckdb")
    assert _expressible(specs)


# ----------------------------------------------------------------- refusals


def test_window_measure_refused(tmp_path):
    p = _view([{"name": "trailing_revenue", "expr": "SUM(amount)",
                "window": [{"order": "order_date", "range": "trailing 7 day",
                            "semiadditive": "last"}]}], tmp_path)
    specs, _ = load_uc_metrics(p, "duckdb")
    (s,) = specs
    assert not s.expr and "window measure" in s.untranslatable[0]


def test_join_reference_refused_but_joins_disclosed(tmp_path):
    p = _view([{"name": "customer_revenue", "expr": "SUM(customer.spend)"},
               {"name": "order_count", "expr": "COUNT(1)"}], tmp_path,
              joins=[{"name": "customer", "source": "db.main.customer",
                      "on": "source.cust_id = customer.id"}])
    specs, skipped = load_uc_metrics(p, "duckdb")
    refused = [s for s in specs if s.untranslatable]
    ok = _expressible(specs)
    assert len(refused) == 1 and "joined table customer" in refused[0].untranslatable[0]
    assert len(ok) == 1 and ok[0].name == "order_count"
    assert any("join" in s["reason"] for s in skipped)


def test_parameterized_expression_refused(tmp_path):
    p = _view([{"name": "windowed_revenue",
                "expr": "SUM(IF(order_date > :start_date, amount, 0))"}],
              tmp_path,
              parameters=[{"name": "start_date", "data_type": "date"}])
    specs, _ = load_uc_metrics(p, "duckdb")
    (s,) = specs
    assert "parameterized" in s.untranslatable[0]


def test_parameterized_view_filter_taints_every_measure(tmp_path):
    p = _view([{"name": "total_revenue", "expr": "SUM(amount)"}], tmp_path,
              filter="order_date > :start",
              parameters=[{"name": "start", "data_type": "date"}])
    specs, _ = load_uc_metrics(p, "duckdb")
    (s,) = specs
    assert s.untranslatable and "view filter" in s.untranslatable[0]


def test_non_identity_field_reference_refused(tmp_path):
    p = _view([{"name": "net_revenue", "expr": "SUM(net_amount)"}], tmp_path,
              fields=[{"name": "net_amount", "expr": "amount - refunds"}])
    specs, _ = load_uc_metrics(p, "duckdb")
    (s,) = specs
    assert "declared field net_amount" in s.untranslatable[0]


def test_unknown_measure_key_taints(tmp_path):
    p = _view([{"name": "x", "expr": "SUM(amount)", "fill_nulls": 0}], tmp_path)
    specs, _ = load_uc_metrics(p, "duckdb")
    (s,) = specs
    assert "fill_nulls" in s.untranslatable[0]


def test_unknown_top_key_taints_all_measures(tmp_path):
    p = _view([{"name": "x", "expr": "SUM(amount)"}], tmp_path,
              row_filter="tenant_id = 7")
    specs, _ = load_uc_metrics(p, "duckdb")
    (s,) = specs
    assert "row_filter" in s.untranslatable[0]


def test_inline_query_source_refused(tmp_path):
    doc = {"version": "1.1",
           "source": "SELECT * FROM db.main.orders WHERE amount > 0",
           "measures": [{"name": "x", "expr": "SUM(amount)"}]}
    p = tmp_path / "inline.yml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    specs, _ = load_uc_metrics(p, "duckdb")
    (s,) = specs
    assert "inline SQL query" in s.untranslatable[0]


def test_version_gate(tmp_path):
    doc = {"version": "0.1", "source": "db.main.orders",
           "measures": [{"name": "x", "expr": "SUM(amount)"}]}
    p = tmp_path / "old.yml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    with pytest.raises(ValueError, match="0.1"):
        load_uc_metrics(p, "duckdb")


def test_unqualified_unknown_column_passes_through(tmp_path):
    # Not a declared field, no qualifier: a physical column of the source —
    # the registry decides at canonicalization, not the adapter.
    p = _view([{"name": "x", "expr": "SUM(mystery_col)"}], tmp_path)
    specs, _ = load_uc_metrics(p, "duckdb")
    assert _expressible(specs)


# ----------------------------------------------------------- intake shapes


def test_directory_intake(tmp_path):
    d = tmp_path / "views"
    d.mkdir()
    _view([{"name": "a", "expr": "SUM(amount)"}], d, name="view_a")
    _view([{"name": "b", "expr": "COUNT(1)"}], d, name="view_b")
    (d / "notes.yaml").write_text("just: notes\n", encoding="utf-8")
    specs, skipped = load_uc_metrics(d, "duckdb")
    assert {s.name for s in specs} == {"a", "b"}
    assert any("not a metric view" in s["reason"] for s in skipped)


def test_empty_directory_raises(tmp_path):
    d = tmp_path / "views"
    d.mkdir()
    (d / "notes.yaml").write_text("just: notes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no metric view definitions"):
        load_uc_metrics(d, "duckdb")


def test_wildcard_measure_disclosed(tmp_path):
    p = _view([{"expr": "source.*"},
               {"name": "x", "expr": "SUM(amount)"}], tmp_path)
    specs, skipped = load_uc_metrics(p, "duckdb")
    assert len(specs) == 1
    assert any("wildcard" in s["reason"] for s in skipped)


# ------------------------------------------------------------- end to end


def test_scan_certifies_agreeing_metric_view(tmp_path):
    estate = tmp_path / "estate"
    estate.mkdir()
    (estate / "mart.sql").write_text(
        "-- semantix: relation=db.main.rev_mart\n"
        "SELECT SUM(amount) AS total_revenue FROM db.main.orders "
        "WHERE status = 'F'\n", encoding="utf-8")
    catalog = tmp_path / "catalog.yml"
    catalog.write_text(
        "db:\n  main:\n    orders:\n      amount: DOUBLE\n      status: VARCHAR\n",
        encoding="utf-8")
    view = _view([{"name": "total_revenue", "expr": "SUM(amount)"}], tmp_path,
                 filter="status = 'F'")
    result, store = run_scan(sql_dir=str(estate), uc_metrics_path=str(view),
                             catalog_path=str(catalog),
                             db_path=str(tmp_path / "s.db"))
    store.close()
    assert len(result.claims) == 1 and result.claims[0].expressible
    concept = [v for v in result.verdicts if v.concept_label == "total_revenue"]
    # The engine certifies; under the conclusions policy that surfaces as an
    # experimental comparison (UNRESOLVED), never as CERTIFIED.
    assert any(engine_verdict(v) == VerdictType.CERTIFIED
               and v.verdict_type == VerdictType.UNRESOLVED for v in concept)


def test_freshness_covers_uc_metrics(tmp_path):
    assert SOURCE_SYSTEMS["uc_metrics_path"] == frozenset({"uc_metrics"})
    p = _view([{"name": "total_revenue", "expr": "SUM(amount)"}], tmp_path)
    shas = current_estate_shas(uc_metrics_path=str(p))
    assert ("uc_metrics", "total_revenue") in shas


# ------------------------------------------------- information_schema catalog


def test_generate_catalog_roundtrips_through_scan(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from semantix.adapters.catalog import load_catalog

    db = tmp_path / "wh.db"
    conn = duckdb.connect(str(db))
    conn.execute("CREATE SCHEMA main_data")
    conn.execute("CREATE TABLE main_data.orders "
                 "(order_id INTEGER, amount DOUBLE, status VARCHAR)")
    conn.close()

    from semantix.warehouse import connect_warehouse
    wh = connect_warehouse(f"duckdb://{db}")
    try:
        catalog = generate_catalog(wh)
    finally:
        wh.close()
    out = tmp_path / "catalog.yml"
    tables, columns = write_catalog_yaml(catalog, out)
    assert tables == 1 and columns == 3

    loaded = load_catalog(out)
    assert any("orders" in key for key in loaded)


def test_generate_catalog_refuses_empty(tmp_path):
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "empty.db"
    duckdb.connect(str(db)).close()

    from semantix.warehouse import connect_warehouse
    wh = connect_warehouse(f"duckdb://{db}")
    try:
        with pytest.raises(ValueError, match="no user tables"):
            generate_catalog(wh)
    finally:
        wh.close()
