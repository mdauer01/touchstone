"""Fingerprint field extraction: grain, filters, join graph, lineage terminals."""

from touchstone.extraction.canonicalize import canonicalize_sql
from touchstone.extraction.fingerprint import extract_fingerprints
from touchstone.models import Provenance

DIALECT = "duckdb"
PROV = Provenance(system="test", object_id="test.model", path=None)


def fingerprints(sql: str, schema: dict, registry):
    canon = canonicalize_sql(sql, schema, DIALECT)
    return extract_fingerprints(canon, "analytics.test_model", PROV, DIALECT, registry)


def test_join_query_fields(schema, registry):
    sql = """select date_trunc('week', f.order_date) as w,
                    sum(f.refund_amount) / nullif(sum(f.amount), 0) as refund_rate
             from analytics.fct_orders as f
             join raw.customers as c on c.customer_id = f.customer_id
             where c.segment <> 'test'
             group by date_trunc('week', f.order_date)"""
    fps = fingerprints(sql, schema, registry)
    assert len(fps) == 1
    fp = fps[0]
    assert fp.concept_candidate == "refund_rate"
    assert fp.core.join_nodes == ("analytics.fct_orders", "raw.customers")
    assert len(fp.core.grain) == 1 and "DATE_TRUNC('WEEK'" in fp.core.grain[0]
    # lineage terminals resolve through the mart to the raw layer
    assert "raw.orders.amount" in fp.core.source_columns
    assert "raw.orders.refund_amount" in fp.core.source_columns
    assert fp.core.measure_expr.startswith("SUM(")


def test_case_when_inside_aggregate_is_one_measure(schema, registry):
    sql = """select sum(case when status = 'completed' then amount else 0 end) as completed_rev
             from analytics.fct_orders"""
    fps = fingerprints(sql, schema, registry)
    assert len(fps) == 1
    assert fps[0].concept_candidate == "completed_rev"
    assert "CASE" in fps[0].core.measure_expr


def test_windowed_aggregate_is_not_a_measure(schema, registry):
    sql = """select order_id, sum(amount) over (partition by customer_id) as running
             from analytics.fct_orders"""
    assert fingerprints(sql, schema, registry) == []


def test_non_aggregate_model_yields_nothing(schema, registry):
    assert fingerprints("select * from analytics.fct_orders", schema, registry) == []


def test_identical_content_same_id_different_name_different_id(schema, registry):
    a = fingerprints("select sum(amount) as gmv from analytics.fct_orders", schema, registry)[0]
    b = fingerprints("select sum(x.amount) as gmv from analytics.fct_orders x", schema, registry)[0]
    c = fingerprints("select sum(amount) as gmv_total from analytics.fct_orders", schema, registry)[0]
    assert a.fingerprint_id == b.fingerprint_id      # content-addressed
    assert a.core.hash() == c.core.hash()            # same math ...
    assert a.fingerprint_id != c.fingerprint_id      # ... different observed name
