"""Agents Schema integration: claims in from AGENTS.*, receipts back out."""

import json

import pytest

from touchstone.adapters.agents_schema import (
    claims_from_tables,
    load_agents_schema,
)
from touchstone.cli import main
from touchstone.engine import run_scan
from touchstone.models import VerdictType
from touchstone.services.writeback import publish_receipts
from touchstone.signing import verify_receipt_dict
from touchstone.store import Store

from .policy import engine_kinds, engine_verdict


def test_osi_metric_resolves_model_through_dataset():
    specs = claims_from_tables({
        "osi_dataset": [{"name": "orders", "source_table": "analytics.fct_orders"}],
        "osi_metric": [
            {"name": "net_revenue", "dataset_name": "orders",
             "expression": "sum(amount - refund_amount)", "description": "net"},
            {"name": "floating_metric", "expression": "sum(x)"},  # no dataset
        ],
    })
    by_name = {s.name: s for s in specs}
    assert by_name["net_revenue"].model == "analytics.fct_orders"
    assert by_name["net_revenue"].system == "agents_osi"
    assert by_name["floating_metric"].model is None  # honestly inexpressible


def test_lookml_measure_translation():
    specs = claims_from_tables({
        "lookml_view": [{"name": "orders_view", "sql_table_name": "analytics.fct_orders"}],
        "lookml_measure": [
            {"view_name": "orders_view", "measure_name": "gmv", "type": "sum",
             "sql": "${TABLE}.amount",
             "filters": '[channel: "-internal", status: "-cancelled"]'},
            {"view_name": "orders_view", "measure_name": "buyers",
             "type": "count_distinct", "sql": "${TABLE}.customer_id"},
            {"view_name": "orders_view", "measure_name": "weird", "type": "sum",
             "sql": "${TABLE}.amount", "filters": "advanced lookml soup"},
        ],
    })
    by_name = {s.name: s for s in specs}
    gmv = by_name["gmv"]
    assert gmv.expr == "sum(amount)"
    # LookML negation includes NULLs in Looker. The earlier expectation here
    # (a bare `<>`) encoded the bug: it made the claim stricter than the
    # declaration, so a model that also dropped NULLs would falsely certify.
    assert gmv.filters == [
        "(channel <> 'internal' OR channel IS NULL)",
        "(status <> 'cancelled' OR status IS NULL)",
    ]
    assert gmv.model == "analytics.fct_orders"
    assert by_name["buyers"].expr == "count(distinct customer_id)"
    # Unparseable filter text is REPORTED, never passed through as if it were
    # a SQL predicate — emitting it verbatim invented a claim.
    weird = by_name["weird"]
    assert weird.filters == []
    assert weird.untranslatable == ["advanced lookml soup"]


def test_json_and_duckdb_intake_produce_identical_claims(acme, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    from_json = load_agents_schema(acme / "agents_schema.json")

    data = json.loads((acme / "agents_schema.json").read_text())
    db = tmp_path / "agents.duckdb"  # the natural filename — catalog also "agents"
    con = duckdb.connect(str(db))
    catalog = con.execute("SELECT current_database()").fetchone()[0]
    con.execute(f'CREATE SCHEMA "{catalog}".agents')
    for table, rows in data.items():
        if not isinstance(rows, list) or not rows:
            continue
        cols = sorted({k for r in rows for k in r})
        fq = f'"{catalog}".agents.{table}'
        con.execute(f"CREATE TABLE {fq} ({', '.join(c + ' VARCHAR' for c in cols)})")
        for r in rows:
            con.execute(f"INSERT INTO {fq} VALUES ({', '.join('?' for _ in cols)})",
                        [r.get(c) for c in cols])
    con.close()

    from_duckdb = load_agents_schema(db)
    key = lambda s: (s.system, s.name)  # noqa: E731
    assert sorted((s.system, s.name, s.expr, s.model, tuple(s.filters))
                  for s in from_json) == \
           sorted((s.system, s.name, s.expr, s.model, tuple(s.filters))
                  for s in from_duckdb)
    assert {key(s) for s in from_json} == {("agents_osi", "net_revenue"),
                                           ("agents_lookml", "gmv")}


def test_cross_provider_scan_surfaces_underspecified_declarations(acme, tmp_path):
    result, _ = run_scan(
        dbt_path=str(acme / "dbt_project"),
        metrics_path=str(acme / "metrics.yml"),
        agents_schema_path=str(acme / "agents_schema.json"),
        catalog_path=str(acme / "catalog.yml"),
        db_path=str(tmp_path / "x.db"),
    )
    assert len(result.claims) == 5  # 3 from YAML + 2 from Agents Schema
    by_label = {}
    for v in result.verdicts:
        by_label.setdefault(v.concept_label, []).append(v)

    # gmv: the grain-less LookML declaration breaks cross-provider consensus,
    # and blame names the missing grain. (The engine's CONTRADICTION surfaces
    # as a suspected divergence under the conclusions policy.)
    gmv = [v for v in by_label["gmv"] if engine_verdict(v) == VerdictType.CONTRADICTION]
    assert gmv and "different grain" in gmv[0].summary
    assert all(v.verdict_type == VerdictType.SUSPECTED_DIVERGENCE for v in gmv)

    # net_revenue: still contradicted; the filterless OSI claim is now one of
    # the divergent members in evidence.
    nr = [v for v in by_label["net_revenue"]
          if engine_verdict(v) == VerdictType.CONTRADICTION]
    assert nr
    assert all(v.verdict_type == VerdictType.SUSPECTED_DIVERGENCE for v in nr)
    wheres = {d["where"] for d in nr[0].evidence["blame"]["divergent"]}
    assert "analytics.growth_net_revenue" in wheres
    # The OSI declaration itself — named as the declaration, not as the table
    # it reads. Blaming `analytics.fct_orders` sent a reader to correct
    # working code for a fault in someone's ontology entry.
    assert "the agents_osi declaration of net_revenue" in wheres


def test_publish_receipts_into_agents_root(acme, tmp_path):
    duckdb = pytest.importorskip("duckdb")
    _, store = run_scan(
        dbt_path=str(acme / "dbt_project"),
        metrics_path=str(acme / "metrics.yml"),
        catalog_path=str(acme / "catalog.yml"),
        db_path=str(tmp_path / "s.db"),
    )
    warehouse = tmp_path / "wh.duckdb"
    summary = publish_receipts(store, str(warehouse))
    assert summary["receipts"] == 4  # net_revenue, gmv, customer_ltv, refund_rate

    con = duckdb.connect(str(warehouse))
    rows = {key: content for _, key, content in
            con.execute("SELECT * FROM agents.root WHERE provider = 'touchstone'").fetchall()}
    receipt = json.loads(rows["receipt/net_revenue"])
    # The engine contradicts; the published receipt states the effective
    # status under the conclusions policy, a suspected divergence.
    assert receipt["status"] == "SUSPECTED_DIVERGENCE"
    assert any(v["summary"].startswith("Suspected divergence (")
               for v in receipt["verdicts"])
    scanned = [r for r in store.verdicts_for_scan(receipt["scan_id"])
               if r["concept_label"] == "net_revenue"]
    assert VerdictType.CONTRADICTION in engine_kinds(scanned)
    assert json.loads(rows["assessment/net_revenue"])["semantic_claim_usable"] is False
    ok, _ = verify_receipt_dict(receipt)  # what agents read IS the certificate
    assert ok
    assert "uses:" in rows["skill/verify-metric"]
    assert "semantic receipts" in rows["receipt"]

    # Republish replaces the provider's rows wholesale (the schema's own model).
    publish_receipts(store, str(warehouse))
    count = con.execute(
        "SELECT count(*) FROM agents.root WHERE provider = 'touchstone'").fetchone()[0]
    con.close()
    assert count == len(rows)


def test_publish_without_scans_is_a_clean_error(tmp_path, capsys):
    empty = tmp_path / "empty.db"
    Store(str(empty)).close()
    rc = main(["publish", "--db", str(empty),
               "--warehouse", str(tmp_path / "w.duckdb")])
    assert rc == 2
    assert "run `touchstone scan` first" in capsys.readouterr().err
