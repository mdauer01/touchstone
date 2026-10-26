"""Apache Ossie adapter: documents in, honest claims out.

The properties under test, in the adapter's own order of importance:
refuse what cannot be read faithfully (versions, non-SQL dialects, unknown
keys), translate what can (per-dialect parsing, dataset resolution, field
substitution), and let the existing reconciliation decide whether a metric's
dialect variants actually agree.
"""

from __future__ import annotations

import json

import pytest
import sqlglot

from semantix.adapters.ossie import load_ossie
from semantix.engine import run_scan
from semantix.models import VerdictType
from semantix.services.freshness import SOURCE_SYSTEMS, current_estate_shas

from .policy import engine_verdict


def _doc(datasets, metrics, version="0.1.1", **model_extra):
    return {
        "version": version,
        "semantic_model": [
            {"name": "m", "datasets": datasets, "metrics": metrics, **model_extra}
        ],
    }


def _dataset(name="orders", source="db.main.orders", **extra):
    return {"name": name, "source": source, **extra}


def _metric(name, *variants, **extra):
    return {
        "name": name,
        "expression": {
            "dialects": [{"dialect": d, "expression": e} for d, e in variants]
        },
        **extra,
    }


def _write(tmp_path, doc, name="doc.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _load(tmp_path, doc, dialect="duckdb"):
    return load_ossie(_write(tmp_path, doc), dialect)


def _expressible(specs):
    return [s for s in specs if s.expr and not s.untranslatable]


def _refused(specs):
    return [s for s in specs if s.untranslatable]


# ------------------------------------------------------------- translation


def test_single_dataset_ansi_metric(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()], [_metric("total_revenue", ("ANSI_SQL", "SUM(orders.amount)"))]))
    assert len(specs) == 1
    s = specs[0]
    assert s.system == "ossie" and s.name == "total_revenue"
    assert s.model == "db.main.orders" and not s.untranslatable
    assert "amount" in s.expr.lower()


def test_dataset_name_rewritten_to_source_tail(tmp_path):
    # The synthesized claim reads FROM db.main.fct_orders, so the qualifier
    # must become the table's own name or qualification cannot bind.
    specs, _ = _load(tmp_path, _doc(
        [_dataset(name="sales", source="db.main.fct_orders")],
        [_metric("total_revenue", ("ANSI_SQL", "SUM(sales.amount)"))]))
    assert "fct_orders.amount" in specs[0].expr.lower()


def test_source_tail_accepted_as_qualifier(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset(name="sales", source="db.main.fct_orders")],
        [_metric("total_revenue", ("ANSI_SQL", "SUM(fct_orders.amount)"))]))
    assert _expressible(specs) and specs[0].model == "db.main.fct_orders"


def test_multi_dialect_variants_become_sibling_specs(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()],
        [_metric("total_revenue",
                 ("ANSI_SQL", "SUM(orders.amount)"),
                 ("SNOWFLAKE", "SUM(orders.net)"))]))
    assert len(specs) == 2
    assert {s.name for s in specs} == {"total_revenue"}
    assert specs[0].expr != specs[1].expr
    assert specs[0].path.endswith("@ANSI_SQL")
    assert specs[1].path.endswith("@SNOWFLAKE")


def test_identical_variants_merge_into_one_claim(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()],
        [_metric("total_revenue",
                 ("ANSI_SQL", "SUM(orders.amount)"),
                 ("SNOWFLAKE", "SUM(orders.amount)"))]))
    assert len(specs) == 1
    assert "@ANSI_SQL+SNOWFLAKE" in specs[0].path


def test_snowflake_variant_parsed_under_snowflake_grammar(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()],
        [_metric("paid_revenue",
                 ("SNOWFLAKE", "SUM(IFF(orders.status = 'paid', orders.amount, 0))"))]))
    (s,) = _expressible(specs)
    # Whatever the rendering, it must be readable in the scan dialect.
    sqlglot.parse_one(s.expr, read="duckdb")


def test_field_substitution(tmp_path):
    ds = _dataset(fields=[{
        "name": "net_amount",
        "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "amount - refunds"}]},
    }])
    specs, _ = _load(tmp_path, _doc(
        [ds], [_metric("net_revenue", ("ANSI_SQL", "SUM(orders.net_amount)"))]))
    (s,) = _expressible(specs)
    assert "amount - refunds" in s.expr.lower()


def test_identity_field_left_alone(tmp_path):
    ds = _dataset(fields=[{
        "name": "amount",
        "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "amount"}]},
    }])
    specs, _ = _load(tmp_path, _doc(
        [ds], [_metric("total_revenue", ("ANSI_SQL", "SUM(orders.amount)"))]))
    (s,) = _expressible(specs)
    assert "amount" in s.expr.lower()


def test_field_with_no_readable_dialect_refuses(tmp_path):
    ds = _dataset(fields=[{
        "name": "net_amount",
        "expression": {"dialects": [
            {"dialect": "TABLEAU", "expression": "[Amount] - [Refunds]"}]},
    }])
    specs, _ = _load(tmp_path, _doc(
        [ds], [_metric("net_revenue", ("ANSI_SQL", "SUM(orders.net_amount)"))]))
    (s,) = specs
    assert s.untranslatable and "net_amount" in s.untranslatable[0]


# ---------------------------------------------------------------- refusals


def test_non_sql_variant_blocks_but_sql_sibling_survives(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()],
        [_metric("total_revenue",
                 ("ANSI_SQL", "SUM(orders.amount)"),
                 ("TABLEAU", "SUM([Amount])"))]))
    assert len(specs) == 2
    ok = _expressible(specs)
    bad = _refused(specs)
    assert len(ok) == 1 and len(bad) == 1
    assert "TABLEAU" in bad[0].untranslatable[0]


def test_unknown_dialect_refused_by_name(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()], [_metric("x", ("KUSTO", "summarize sum(amount)"))]))
    (s,) = specs
    assert "KUSTO" in s.untranslatable[0]


def test_metric_spanning_datasets_refused(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset(), _dataset(name="stores", source="db.main.stores")],
        [_metric("productivity",
                 ("ANSI_SQL", "SUM(orders.amount) / SUM(stores.employees)"))]))
    (s,) = specs
    assert "spans datasets" in s.untranslatable[0]


def test_unqualified_column_with_two_datasets_refused(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset(), _dataset(name="stores", source="db.main.stores")],
        [_metric("total_revenue", ("ANSI_SQL", "SUM(amount)"))]))
    (s,) = specs
    assert "cannot attribute" in s.untranslatable[0]


def test_unqualified_column_with_one_dataset_resolves(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()], [_metric("total_revenue", ("ANSI_SQL", "SUM(amount)"))]))
    (s,) = _expressible(specs)
    assert s.model == "db.main.orders"


def test_unknown_qualifier_refused(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()], [_metric("x", ("ANSI_SQL", "SUM(payments.amount)"))]))
    (s,) = specs
    assert "payments" in s.untranslatable[0]


def test_full_query_expression_refused(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()], [_metric("x", ("ANSI_SQL", "SELECT SUM(amount) FROM orders"))]))
    (s,) = specs
    assert "full query" in s.untranslatable[0]


def test_unparseable_variant_refused(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()], [_metric("x", ("ANSI_SQL", "SUM((amount"))]))
    (s,) = specs
    assert "could not parse" in s.untranslatable[0]


# --------------------------------------------------------- deny by default


def test_unknown_metric_key_taints(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()],
        [_metric("x", ("ANSI_SQL", "SUM(orders.amount)"), window="7d")]))
    (s,) = specs
    assert not s.expr and "window" in s.untranslatable[0]


def test_unknown_document_key_taints_every_metric(tmp_path):
    doc = _doc([_dataset()], [_metric("x", ("ANSI_SQL", "SUM(orders.amount)"))])
    doc["default_filter"] = "region = 'EU'"
    specs, _ = _load(tmp_path, doc)
    (s,) = specs
    assert "default_filter" in s.untranslatable[0]


def test_unknown_model_key_taints_every_metric(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()], [_metric("x", ("ANSI_SQL", "SUM(orders.amount)"))],
        row_policy="tenant"))
    (s,) = specs
    assert "row_policy" in s.untranslatable[0]


def test_metric_custom_extensions_block(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset()],
        [_metric("x", ("ANSI_SQL", "SUM(orders.amount)"),
                 custom_extensions=[{"vendor_name": "dbt", "data": "{}"}])]))
    (s,) = specs
    assert not s.expr and "dbt" in s.untranslatable[0]


def test_dataset_custom_extensions_block_its_metrics(tmp_path):
    specs, _ = _load(tmp_path, _doc(
        [_dataset(custom_extensions=[{"vendor_name": "acme", "data": "{}"}])],
        [_metric("x", ("ANSI_SQL", "SUM(orders.amount)"))]))
    (s,) = specs
    assert s.untranslatable and "acme" in s.untranslatable[0]


def test_model_custom_extensions_disclosed_not_blocking(tmp_path):
    specs, skipped = _load(tmp_path, _doc(
        [_dataset()], [_metric("x", ("ANSI_SQL", "SUM(orders.amount)"))],
        custom_extensions=[{"vendor_name": "dbt", "data": "{}"}]))
    assert _expressible(specs)
    assert any("dbt" in s["reason"] for s in skipped)


def test_duplicate_dialect_with_different_text_taints(tmp_path):
    doc = _doc([_dataset()], [{
        "name": "x",
        "expression": {"dialects": [
            {"dialect": "ANSI_SQL", "expression": "SUM(a)"},
            {"dialect": "ANSI_SQL", "expression": "SUM(b)"},
        ]},
    }])
    specs, _ = _load(tmp_path, doc)
    (s,) = specs
    assert "twice" in " ".join(s.untranslatable)


# ------------------------------------------------------- intake and shapes


def test_version_gate(tmp_path):
    with pytest.raises(ValueError, match="0.2.0.dev0"):
        _load(tmp_path, _doc([_dataset()], [], version="0.2.0.dev0"))
    doc = _doc([_dataset()], [])
    del doc["version"]
    with pytest.raises(ValueError, match="version"):
        _load(tmp_path, doc)


def test_dbt_wrapper_documents_read(tmp_path):
    wrapper = {
        "metadata": {"dbt_version": "1.12.0"},
        "documents": [_doc([_dataset()],
                           [_metric("x", ("ANSI_SQL", "SUM(orders.amount)"))])],
    }
    specs, skipped = load_ossie(_write(tmp_path, wrapper, "osi_document.json"),
                                "duckdb")
    assert _expressible(specs)
    assert any("wrapper keys not read" in s["reason"] for s in skipped)


def test_yaml_document(tmp_path):
    import yaml
    doc = _doc([_dataset()], [_metric("x", ("ANSI_SQL", "SUM(orders.amount)"))])
    p = tmp_path / "model.yaml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    specs, _ = load_ossie(p, "duckdb")
    assert _expressible(specs)


def test_directory_intake_discloses_stray_files(tmp_path):
    d = tmp_path / "osi"
    d.mkdir()
    (d / "doc.json").write_text(json.dumps(_doc(
        [_dataset()], [_metric("x", ("ANSI_SQL", "SUM(orders.amount)"))])),
        encoding="utf-8")
    (d / "notes.yaml").write_text("just: notes\n", encoding="utf-8")
    specs, skipped = load_ossie(d, "duckdb")
    assert _expressible(specs)
    assert any("not an Ossie document" in s["reason"] for s in skipped)


def test_directory_without_documents_raises(tmp_path):
    d = tmp_path / "osi"
    d.mkdir()
    (d / "notes.yaml").write_text("just: notes\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no Ossie documents"):
        load_ossie(d, "duckdb")


def test_relationships_and_keys_disclosed(tmp_path):
    specs, skipped = _load(tmp_path, _doc(
        [_dataset(primary_key=["id"])],
        [_metric("x", ("ANSI_SQL", "SUM(orders.amount)"))],
        relationships=[{"name": "o2c", "from": "orders", "to": "customers",
                        "from_columns": ["customer_id"], "to_columns": ["id"]}]))
    reasons = " ".join(s["reason"] for s in skipped)
    assert "relationship" in reasons and "key declarations" in reasons
    assert all(s.get("informational") for s in skipped)


def test_datasets_only_model_disclosed(tmp_path):
    specs, skipped = _load(tmp_path, _doc([_dataset()], []))
    assert not specs
    assert any("no metrics" in s["reason"] for s in skipped)


def test_nameless_metric_raises(tmp_path):
    doc = _doc([_dataset()], [{"expression": {"dialects": []}}])
    with pytest.raises(ValueError, match="no name"):
        _load(tmp_path, doc)


# ------------------------------------------------------------- end to end


def _estate(tmp_path, sql):
    estate = tmp_path / "estate"
    estate.mkdir()
    (estate / "mart.sql").write_text(
        "-- semantix: relation=db.main.revenue_mart\n" + sql, encoding="utf-8")
    catalog = tmp_path / "catalog.yml"
    catalog.write_text(
        "db:\n  main:\n    orders:\n      amount: DOUBLE\n      net: DOUBLE\n"
        "      status: VARCHAR\n", encoding="utf-8")
    return estate, catalog


def test_scan_certifies_agreeing_ossie_claim(tmp_path):
    estate, catalog = _estate(
        tmp_path, "SELECT SUM(amount) AS total_revenue FROM db.main.orders\n")
    osi = _write(tmp_path, _doc(
        [_dataset()],
        [_metric("total_revenue", ("ANSI_SQL", "SUM(orders.amount)"))]))
    result, store = run_scan(sql_dir=str(estate), osi_path=str(osi),
                             catalog_path=str(catalog),
                             db_path=str(tmp_path / "s.db"))
    store.close()
    assert len(result.claims) == 1 and result.claims[0].expressible
    concept = [v for v in result.verdicts if v.concept_label == "total_revenue"]
    # The engine certifies; under the conclusions policy that surfaces as an
    # experimental comparison (UNRESOLVED), never as CERTIFIED.
    assert any(engine_verdict(v) == VerdictType.CERTIFIED
               and v.verdict_type == VerdictType.UNRESOLVED for v in concept)


def test_scan_contradicts_disagreeing_dialect_variants(tmp_path):
    # The ANSI text and the Snowflake text of ONE metric quietly diverge —
    # the cross-dialect drift only a canonical comparison can call out.
    estate, catalog = _estate(
        tmp_path, "SELECT SUM(amount) AS total_revenue FROM db.main.orders\n")
    osi = _write(tmp_path, _doc(
        [_dataset()],
        [_metric("total_revenue",
                 ("ANSI_SQL", "SUM(orders.amount)"),
                 ("SNOWFLAKE", "SUM(orders.net)"))]))
    result, store = run_scan(sql_dir=str(estate), osi_path=str(osi),
                             catalog_path=str(catalog),
                             db_path=str(tmp_path / "s.db"))
    store.close()
    assert len(result.claims) == 2
    concept = [v for v in result.verdicts if v.concept_label == "total_revenue"]
    # The engine contradicts; the policy reports it as a suspected divergence.
    assert any(engine_verdict(v) == VerdictType.CONTRADICTION
               and v.verdict_type == VerdictType.SUSPECTED_DIVERGENCE for v in concept)


def test_scan_result_ossie_skips_stay_informational(tmp_path):
    estate, catalog = _estate(
        tmp_path, "SELECT SUM(amount) AS total_revenue FROM db.main.orders\n")
    osi = _write(tmp_path, _doc(
        [_dataset()],
        [_metric("total_revenue", ("ANSI_SQL", "SUM(orders.amount)"))],
        relationships=[{"name": "r", "from": "a", "to": "b",
                        "from_columns": ["x"], "to_columns": ["y"]}]))
    result, store = run_scan(sql_dir=str(estate), osi_path=str(osi),
                             catalog_path=str(catalog),
                             db_path=str(tmp_path / "s.db"))
    store.close()
    ossie_skips = [s for s in result.skipped if s["system"] == "ossie"]
    assert ossie_skips and all(s.get("informational") for s in ossie_skips)


def test_freshness_covers_ossie(tmp_path):
    assert SOURCE_SYSTEMS["osi_path"] == frozenset({"ossie"})
    osi = _write(tmp_path, _doc(
        [_dataset()],
        [_metric("total_revenue", ("ANSI_SQL", "SUM(orders.amount)"))]))
    shas = current_estate_shas(osi_path=str(osi))
    assert ("ossie", "total_revenue") in shas
