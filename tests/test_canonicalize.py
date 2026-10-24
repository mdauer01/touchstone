"""Canonical-form equality across cosmetic rewrites — the property the whole
verdict system leans on: equality is proof, difference is hypothesis."""


from semantix.extraction.canonicalize import canonicalize_sql
from semantix.extraction.sqlutil import render
from semantix.models import CanonLevel

DIALECT = "duckdb"


def canon(sql: str, schema: dict) -> str:
    result = canonicalize_sql(sql, schema, DIALECT)
    assert result.level == CanonLevel.OPTIMIZED, result.error
    return render(result.ast, DIALECT)


def strip_output_aliases(text: str) -> str:
    # Output aliases are outside the semantic core; drop them for whole-query
    # comparisons.
    import re

    return re.sub(r' AS "[a-z_]+"', "", text)


def test_author_aliases_do_not_matter(schema):
    a = canon("select sum(x.amount) as m from analytics.fct_orders as x", schema)
    b = canon("select sum(ord.amount) as m from analytics.fct_orders as ord", schema)
    assert a == b
    assert " AS t0" in a


def test_cte_and_inline_converge(schema):
    a = canon(
        """with base as (select * from analytics.fct_orders where status = 'completed')
           select sum(amount) as m from base""",
        schema,
    )
    b = canon(
        "select sum(amount) as m from analytics.fct_orders where status = 'completed'",
        schema,
    )
    assert a == b


def test_not_eq_normalizes_to_neq(schema):
    a = canon("select sum(amount) as m from analytics.fct_orders where not status = 'x'", schema)
    b = canon("select sum(amount) as m from analytics.fct_orders where status <> 'x'", schema)
    assert a == b


def test_conjunct_order_does_not_matter(schema):
    a = canon(
        "select sum(amount) as m from analytics.fct_orders "
        "where channel <> 'internal' and status = 'completed'",
        schema,
    )
    b = canon(
        "select sum(amount) as m from analytics.fct_orders "
        "where status = 'completed' and channel <> 'internal'",
        schema,
    )
    assert a == b


def test_group_by_ordinal_resolves(schema):
    a = canon(
        "select date_trunc('month', order_date) as g, sum(amount) as m "
        "from analytics.fct_orders group by 1",
        schema,
    )
    b = canon(
        "select date_trunc('month', order_date) as g, sum(amount) as m "
        "from analytics.fct_orders group by date_trunc('month', order_date)",
        schema,
    )
    assert a == b
    assert "GROUP BY DATE_TRUNC" in a


def test_order_by_alone_is_neutral(schema):
    """Ordering does not change a measure's value — when nothing bounds it."""
    a = canon("select sum(amount) as m from analytics.fct_orders order by 1", schema)
    b = canon("select sum(amount) as m from analytics.fct_orders", schema)
    assert a == b


def test_limit_is_not_neutral(schema):
    """A bounded measure is a DIFFERENT measure. The previous version of this
    test asserted the opposite and was itself the bug: stripping LIMIT made a
    top-N metric canonically identical to the unbounded total."""
    bounded = canon(
        "select customer_id, sum(amount) as m from analytics.fct_orders "
        "group by 1 order by 2 desc limit 10", schema)
    total = canon(
        "select customer_id, sum(amount) as m from analytics.fct_orders group by 1", schema)
    assert bounded != total
    assert "LIMIT" in bounded
    # ORDER BY survives too — it is load-bearing once the output is bounded.
    assert "ORDER BY" in bounded


def test_symmetric_eq_operands_sorted(schema):
    a = canon(
        "select sum(f.amount) as m from analytics.fct_orders f "
        "join raw.customers c on c.customer_id = f.customer_id",
        schema,
    )
    b = canon(
        "select sum(f.amount) as m from analytics.fct_orders f "
        "join raw.customers c on f.customer_id = c.customer_id",
        schema,
    )
    assert a == b


def test_star_expansion(schema):
    a = canon("select * from analytics.fct_orders", schema)
    assert "*" not in a
    assert '"order_id"' in a and '"channel"' in a


def test_whole_pipeline_aliasing_pair(schema):
    total_gmv = """select date_trunc('month', order_date) as gmv_month, sum(amount) as total_gmv
        from analytics.fct_orders
        where not status = 'cancelled' and channel <> 'internal'
        group by date_trunc('month', order_date)"""
    gmv_value = """with base as (
            select * from analytics.fct_orders
            where channel <> 'internal' and status <> 'cancelled')
        select date_trunc('month', order_date) as reporting_month,
               sum(amount) as gross_merchandise_value
        from base group by 1"""
    assert strip_output_aliases(canon(total_gmv, schema)) == strip_output_aliases(
        canon(gmv_value, schema)
    )


def test_degradation_ladder():
    # unknown single table still optimizes (nothing to disambiguate) —
    # graceful, and equality remains deterministic
    unknown = canonicalize_sql("select sum(amount) from nowhere.unknown_t", {}, DIALECT)
    # sqlglot 30.12 optimizes a lone unknown table; 30.24 refuses to qualify
    # against an empty schema. Both are honest (the stricter one is below
    # proof grade); what must hold is that it never raises.
    assert unknown.level in (CanonLevel.OPTIMIZED, CanonLevel.PARSED)

    # a genuinely ambiguous column degrades to PARSED, never raises
    ambiguous = canonicalize_sql(
        "select sum(amount) from a.t1 cross join a.t2", {}, DIALECT
    )
    assert ambiguous.level == CanonLevel.PARSED
    assert ambiguous.ast is not None and ambiguous.error

    unparseable = canonicalize_sql("this is not sql at all (", {}, DIALECT)
    assert unparseable.level == CanonLevel.FAILED
    assert unparseable.ast is None
