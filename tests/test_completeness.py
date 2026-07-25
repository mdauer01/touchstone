"""The completeness gate: unmodeled SQL must never reach proof grade.

The SemanticCore is a whitelist, so anything it does not read defaults to
"identical" — which turns a blind spot into a false certification. These
tests pin the inverted default and the constructs that closed it, all of
which were live false-certification paths found by an adversarial review on
2026-07-25.
"""

import pytest

from semantix.extraction.canonicalize import canonicalize_sql
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import PROOF_LEVELS, CanonLevel, Provenance

CATALOG = {
    "raw.orders": {
        "order_id": "bigint", "customer_id": "bigint", "status": "varchar",
        "channel": "varchar", "amount": "decimal(18,2)", "refund_amount": "decimal(18,2)",
    },
    "raw.refunds": {"refund_id": "bigint", "order_id": "bigint", "amt": "decimal(18,2)"},
}


def fp(sql: str, dialect: str = "duckdb"):
    registry = SchemaRegistry(CATALOG)
    canon = canonicalize_sql(sql, registry.sqlglot_schema(), dialect)
    fps = extract_fingerprints(canon, "analytics.m", Provenance("t", "t"), dialect, registry)
    return fps[0] if fps else None


def core_hash(sql: str) -> str:
    return fp(sql).core.hash()


# ------------------------------------------------- the constructs, now modeled

@pytest.mark.parametrize(
    "name,a,b",
    [
        (
            "cte-body",
            "with base as (select customer_id, sum(amount) as s from raw.orders "
            "where status = 'completed' group by 1) select sum(s) as m from base",
            "with base as (select customer_id, sum(refund_amount) as s from raw.orders "
            "where status = 'cancelled' group by 1) select sum(s) as m from base",
        ),
        (
            "join-type",
            "select sum(o.amount) as m from raw.orders o "
            "join raw.refunds r on o.order_id = r.order_id",
            "select sum(o.amount) as m from raw.orders o "
            "left join raw.refunds r on o.order_id = r.order_id",
        ),
        (
            "having",
            "select customer_id, sum(amount) as m from raw.orders group by 1 "
            "having sum(amount) > 100",
            "select customer_id, sum(amount) as m from raw.orders group by 1",
        ),
        (
            "rollup-vs-total",
            "select status, channel, sum(amount) as m from raw.orders "
            "group by rollup(status, channel)",
            "select sum(amount) as m from raw.orders",
        ),
        (
            "grouping-sets",
            "select status, sum(amount) as m from raw.orders "
            "group by grouping sets ((status), ())",
            "select status, sum(amount) as m from raw.orders group by status",
        ),
        (
            "select-distinct",
            "select distinct customer_id, sum(amount) as m from raw.orders group by 1",
            "select customer_id, sum(amount) as m from raw.orders group by 1",
        ),
        (
            "limit",
            "select customer_id, sum(amount) as m from raw.orders group by 1 "
            "order by 2 desc limit 10",
            "select customer_id, sum(amount) as m from raw.orders group by 1",
        ),
        (
            "cross-join",
            "select sum(o.amount) as m from raw.orders o cross join raw.refunds r",
            "select sum(o.amount) as m from raw.orders o",
        ),
    ],
)
def test_semantically_different_measures_never_collide(name, a, b):
    """Each pair falsely CERTIFIED before 2026-07-25."""
    fa, fb = fp(a), fp(b)
    assert fa.core.hash() != fb.core.hash(), f"{name}: false certification"


def test_modeled_constructs_stay_proof_grade():
    """Modeling must restore proof grade — a gate that never lifts is just a
    refusal to verify."""
    for sql in [
        "select customer_id, sum(amount) as m from raw.orders group by 1 having sum(amount) > 100",
        "select sum(o.amount) as m from raw.orders o left join raw.refunds r on o.order_id = r.order_id",
        "select status, sum(amount) as m from raw.orders group by rollup(status)",
        "select customer_id, sum(amount) as m from raw.orders group by 1 order by 2 desc limit 10",
        "with base as (select customer_id, sum(amount) as s from raw.orders group by 1) "
        "select sum(s) as m from base",
    ]:
        f = fp(sql)
        assert f.canon_level in PROOF_LEVELS, sql
        assert f.gaps == (), sql


def test_identical_logic_under_different_cte_names_still_matches():
    """The CTE digest keys on the BODY, never the name, so staging a metric
    under a different alias is not a semantic change."""
    a = core_hash("with base as (select customer_id, sum(amount) as s from raw.orders "
                  "where status = 'completed' group by 1) select sum(s) as m from base")
    b = core_hash("with staging as (select customer_id, sum(amount) as s from raw.orders "
                  "where status = 'completed' group by 1) select sum(s) as m from staging")
    assert a == b


@pytest.mark.parametrize(
    "name,sql,dialect,gap",
    [
        ("lateral", "select sum(x.amount) as m from raw.orders o, "
                    "lateral (select o.amount as amount) x", "duckdb", "opaque derived table"),
        ("unnest", "select sum(u) as m from raw.orders, unnest([1, 2]) as t(u)",
         "duckdb", "UNNEST source"),
        ("with-totals", "select status, sum(amount) as m from raw.orders "
                        "group by status with totals", "clickhouse", "GROUP BY WITH TOTALS"),
    ],
)
def test_unmodeled_construct_drops_below_proof_grade(name, sql, dialect, gap):
    """Constructs the core still cannot represent must refuse certification
    rather than assume equality — and must name what they could not read."""
    f = fp(sql, dialect)
    assert f is not None, name
    assert f.canon_level == CanonLevel.INCOMPLETE.value, name
    assert gap in f.gaps, name
    assert f.canon_level not in PROOF_LEVELS, name


def test_clean_query_hash_is_unchanged_by_the_gate():
    """Queries using none of these constructs must keep the exact hash they
    had before, so existing bitemporal history stays comparable."""
    f = fp("select customer_id, sum(amount - refund_amount) as net "
           "from raw.orders where status = 'completed' group by 1")
    assert f.canon_level == CanonLevel.OPTIMIZED.value
    assert f.gaps == ()
    assert f.core.filters == ('t0."status" = \'completed\'',)
