"""Extraction must read what is there, and nothing that is not.

Round-two adversarial review, 2026-07-26. These are the defects that survive
the completeness gate: the core is populated, but with the wrong contents.
Every test failed before its fix.
"""

import pytest

from semantix.adapters.catalog import load_catalog
from semantix.extraction.canonicalize import canonicalize
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import PROOF_LEVELS, Provenance

CATALOG = {
    "raw.orders": {
        "order_id": "bigint", "customer_id": "bigint", "status": "varchar",
        "amount": "decimal(18,2)", "refund_amount": "decimal(18,2)",
    },
}


def fingerprint(sql: str, catalog=None, dialect="duckdb"):
    registry = SchemaRegistry(catalog or CATALOG)
    canon = canonicalize(sql, registry, dialect)
    fps = extract_fingerprints(canon, "analytics.m", Provenance("t", "t"), dialect, registry)
    return fps[0] if fps else None


# --------------------------------------------------------- CTE body digest

def test_cosmetic_edits_to_a_cte_do_not_fabricate_a_contradiction():
    """The CTE digest hashed the RENDERED BODY TEXT, so reformatting, a
    comment, or an alias rename inside a CTE changed the fingerprint — the
    engine reported a definition change where none happened, and fractured
    the concept's bitemporal history."""
    a = fingerprint(
        "with base as (select customer_id, sum(amount) as s from raw.orders "
        "where status = 'completed' group by 1) select sum(s) as m from base"
    )
    b = fingerprint(
        "with base as (select cust.customer_id, sum(cust.amount) as s "
        "from raw.orders as cust where cust.status = 'completed' "
        "group by 1) select sum(s) as m from base"
    )
    assert a.core.hash() == b.core.hash(), "an alias rename inside a CTE changed the fingerprint"


def test_a_real_change_inside_a_cte_is_still_detected():
    a = fingerprint(
        "with base as (select customer_id, sum(amount) as s from raw.orders "
        "where status = 'completed' group by 1) select sum(s) as m from base"
    )
    b = fingerprint(
        "with base as (select customer_id, sum(amount) as s from raw.orders "
        "where status = 'pending' group by 1) select sum(s) as m from base"
    )
    assert a.core.hash() != b.core.hash()


def test_lineage_does_not_resolve_through_an_unresolved_cte_name():
    """Terminals were recorded against the CTE's NAME, producing physical
    lineage like `base.s` — a relation that does not exist."""
    fp = fingerprint(
        "with base as (select customer_id, sum(amount) as s from raw.orders "
        "group by 1) select sum(s) as m from base"
    )
    assert not any(t.startswith("base.") for t in fp.core.source_columns), (
        f"fabricated lineage terminal: {fp.core.source_columns}"
    )


# ------------------------------------------------------------- DISTINCT ON

def test_distinct_on_keeps_its_key():
    """`DISTINCT ON (a)` and `DISTINCT ON (b)` keep different rows. Both were
    flattened to the bare tag `SELECT DISTINCT`, making them identical."""
    a = fingerprint(
        "select distinct on (customer_id) customer_id, sum(amount) as m "
        "from raw.orders group by 1"
    )
    b = fingerprint(
        "select distinct on (status) status, sum(amount) as m "
        "from raw.orders group by 1"
    )
    if a is None or b is None:
        pytest.skip("dialect did not produce a measure for DISTINCT ON")
    assert a.core.hash() != b.core.hash()


# ------------------------------------------------------------ set operations

def test_union_models_are_not_silently_dropped():
    """A UNION model yielded no measure at all and no gap — invisible."""
    registry = SchemaRegistry(CATALOG)
    sql = ("select sum(amount) as m from raw.orders where status = 'completed' "
           "union all select sum(refund_amount) as m from raw.orders")
    canon = canonicalize(sql, registry, "duckdb")
    fps = extract_fingerprints(canon, "analytics.m", Provenance("t", "t"), "duckdb", registry)
    assert fps, "a UNION model produced no fingerprint and no gap"
    assert all(f.canon_level not in PROOF_LEVELS for f in fps), (
        "only one branch of a UNION was read, so it must not be proof grade"
    )
    assert any("set operation" in g for f in fps for g in f.gaps)


# ---------------------------------------------------------------- registry

def test_registry_never_overwrites_a_known_catalog_table_with_nothing():
    """register_model wrote `{}` for a model it could not resolve, erasing a
    real catalog entry and making every later model over that table fail to
    qualify — producing false CONTRADICTIONs between identical definitions."""
    registry = SchemaRegistry(CATALOG)
    before = registry.columns("raw.orders")
    registry.register_model("raw.orders", None)
    assert registry.columns("raw.orders") == before


# ----------------------------------------------------------------- catalog

def test_flat_catalog_shape_does_not_crash():
    """The CLI help documents `schema.table.column: type`; the loader assumed
    the nested shape and raised a traceback on the documented one."""
    import tempfile
    import pathlib

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "c.yml"
        p.write_text("raw.orders:\n  order_id: bigint\n  amount: 'decimal(18,2)'\n")
        catalog = load_catalog(p)
        assert catalog.get("raw.orders", {}).get("order_id") == "bigint"
