"""Round four: adapters must not invent, drop, or wrongly refuse a claim.

2026-07-28. An adapter may TRANSLATE a declaration; it may never INVENT one.
Every test here failed before its fix, and each failure produced a claim that
reached proof grade and reconciled against real code — i.e. a verdict about a
definition nobody wrote.
"""

import pytest

from touchstone.adapters.agents_schema import claims_from_tables
from touchstone.adapters.catalog import load_catalog


def lookml(**row):
    tables = {
        "lookml_view": [{"name": "orders", "sql_table_name": "analytics.fct_orders"}],
        "lookml_measure": [{"measure_name": "revenue", "view_name": "orders",
                            "sql": "${TABLE}.amount", "type": "sum", **row}],
    }
    return claims_from_tables(tables)[0]


# --------------------------------------------------------------- filters

def test_legacy_filters_block_names_the_declared_field():
    """`filters: { field: status, value: "complete" }` is the pre-21.12 form and
    still ubiquitous. The modern regex matched the sub-token `value: "complete"`
    inside it, producing a predicate on a column literally named `value` — which
    exists in plenty of fact tables — while dropping the real filter."""
    spec = lookml(filters='{ field: status, value: "complete" }')
    assert spec.filters == ["status = 'complete'"]
    assert not spec.untranslatable


def test_legacy_block_in_an_unknown_shape_is_refused_not_guessed():
    spec = lookml(filters='{ field: status, operator: contains }')
    assert spec.filters == []
    assert spec.untranslatable


def test_filter_literals_are_escaped():
    """`Lowe's` is an ordinary retail value. Interpolated raw it produced
    unparseable SQL; and a quote-BALANCED value produced a valid injected
    predicate that the optimizer then folded away, so a filtered declaration
    canonicalized to filters=() at proof grade."""
    import sqlglot
    from sqlglot import exp

    assert lookml(filters='[merchant: "Lowe\'s"]').filters == ["merchant = 'Lowe''s'"]

    spec = lookml(filters="[status: \"x' OR TRUE OR '\"]")
    # The injected text survives as DATA inside one string literal, not as SQL:
    # parse the predicate and check it is a single equality whose right side is
    # a literal. Previously it parsed as `status = 'x' OR TRUE OR ''`, which the
    # optimizer folded to nothing — a filtered declaration reaching proof grade
    # with filters=().
    node = sqlglot.parse_one(spec.filters[0])
    assert isinstance(node, exp.EQ), f"filter parsed as {type(node).__name__}, not an equality"
    assert isinstance(node.expression, exp.Literal)
    assert node.expression.this == "x' OR TRUE OR '"


def test_a_field_of_another_view_is_refused():
    """`users.country` is a column of a JOINED view. `\\w+` could not match the
    qualifier so the regex resynced on `country` and applied the filter to this
    measure's base table — a predicate on a column that is not there."""
    spec = lookml(filters='[users.country: "US"]')
    assert spec.filters == []
    assert "another view" in spec.untranslatable[0]


@pytest.mark.parametrize("value", ["before yesterday", "after today", "before tomorrow",
                                   "after yesterday", "past today"])
def test_relative_date_operands_are_recognized(value):
    """`before`/`after`/`past` take a date operand that need not contain a
    digit. Requiring one let these through as string equalities against their
    own text, on a date column."""
    spec = lookml(filters=f'[d: "{value}"]')
    assert spec.filters == [], f"fabricated a predicate from {value!r}"
    assert spec.untranslatable


# ---------------------------------------------------------- measure types

def test_type_number_is_refused_because_it_may_reference_other_measures():
    """`${refund_total}` is itself an aggregate; passing the sql through turned
    it into a bare column reference — a definition nobody wrote, and not even
    valid grouped SQL. dbt's adapter refuses ratio/derived metrics for exactly
    this reason."""
    spec = lookml(type="number", sql="SUM(${TABLE}.amount) - ${refund_total}")
    assert spec.expr is None
    assert spec.untranslatable


def test_sql_distinct_key_is_refused():
    """Looker emits a de-duplicating symmetric aggregate here. Rendering a plain
    SUM asserts the very thing sql_distinct_key exists to prevent, and would
    certify against a naive fan-out-prone model."""
    spec = lookml(sql_distinct_key="${order_id}")
    assert spec.expr is None
    assert "symmetric aggregate" in spec.untranslatable[0]


def test_type_count_counts_rows():
    """Looker's `type: count` takes no sql and counts rows. It yielded no
    expression at all — the single most common measure type, unverifiable."""
    assert lookml(type="count", sql="").expr == "count(*)"
    assert lookml(type="count", sql="${TABLE}.order_id").expr == "count(*)"


@pytest.mark.parametrize("mtype", ["median", "percentile", "running_total",
                                   "sum_distinct", "yesno", "string", ""])
def test_unmodelled_measure_types_are_refused_with_a_reason(mtype):
    spec = lookml(type=mtype)
    assert spec.expr is None
    assert spec.untranslatable, f"{mtype!r} passed through with no disclosure"


def test_ordinary_aggregates_still_translate():
    """The refusals must not swallow the types the adapter genuinely models."""
    assert lookml(type="sum").expr == "sum(amount)"
    assert lookml(type="average").expr == "avg(amount)"
    assert lookml(type="count_distinct").expr == "count(distinct amount)"


# ------------------------------------------------------------------- OSI

def test_osi_declared_filters_are_not_silently_dropped():
    """Reading only `expression` recorded an UNFILTERED metric for a
    declaration that filters — a claim narrower than the one written, which
    certifies against unfiltered code."""
    tables = {
        "osi_dataset": [{"name": "orders", "source_table": "analytics.fct_orders"}],
        "osi_metric": [{"name": "net_revenue", "dataset_name": "orders",
                        "expression": "sum(amount - refund_amount)",
                        "filters": "status = 'completed'",
                        "dimensions": "order_date, channel"}],
    }
    spec = claims_from_tables(tables)[0]
    assert spec.untranslatable, "a declared filter vanished without a trace"
    assert any("filters" in u for u in spec.untranslatable)


# --------------------------------------------------------------- catalog

def test_a_struct_column_does_not_delete_its_table(tmp_path):
    """A node holding both scalars and dicts is a table with a STRUCT/VARIANT/
    RECORD column — routine on BigQuery, Snowflake and Databricks. Treating it
    as a namespace deleted the table outright and invented a phantom relation
    named after the struct, dropping every model over it to `parsed`."""
    (tmp_path / "c.yml").write_text(
        "raw:\n"
        "  orders:\n"
        "    order_id: bigint\n"
        "    amount: 'decimal(18,2)'\n"
        "    shipping_address:\n"
        "      street: varchar\n"
        "      city: varchar\n"
        "  customers:\n"
        "    customer_id: bigint\n"
    )
    catalog = load_catalog(tmp_path / "c.yml")
    assert "raw.orders" in catalog, "the table was deleted by its own struct column"
    assert catalog["raw.orders"]["order_id"] == "bigint"
    assert catalog["raw.orders"]["amount"] == "decimal(18,2)"
    assert "shipping_address" in catalog["raw.orders"]
    assert "raw.orders.shipping_address" not in catalog  # no phantom relation
    assert catalog["raw.customers"] == {"customer_id": "bigint"}


def test_three_part_relations_still_load(tmp_path):
    (tmp_path / "c.yml").write_text(
        "prod:\n  raw:\n    orders:\n      order_id: bigint\n")
    assert load_catalog(tmp_path / "c.yml") == {"prod.raw.orders": {"order_id": "bigint"}}
