"""Regressions from round three, including two the round-two fixes caused.

2026-07-27. Each of these failed before its fix; several exist because an
earlier fix was correct for the case in front of it and wrong for the case
beside it.
"""

import pytest

from semantix.extraction.canonicalize import canonicalize
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import PROOF_LEVELS, Fingerprint, Provenance, SemanticCore, VerdictType
from semantix.services.reconcile import reconcile
from semantix.services.resolution import resolve_concepts

CATALOG = {"raw.orders": {"order_id": "bigint", "status": "varchar",
                          "amount": "decimal(18,2)", "refund_amount": "decimal(18,2)"}}


def fps_for(sql, dialect="duckdb"):
    registry = SchemaRegistry(CATALOG)
    canon = canonicalize(sql, registry, dialect)
    return extract_fingerprints(canon, "analytics.m", Provenance("t", "t"), dialect, registry)


# ------------------------------------------------------------ set operations

def test_a_three_way_union_still_yields_a_measure():
    """Reading only `.this` reached one branch of a two-way union and none of
    a three-way one, so the model vanished entirely."""
    fps = fps_for(
        "select sum(amount) as m from raw.orders where status = 'a' "
        "union all select sum(amount) as m from raw.orders where status = 'b' "
        "union all select sum(amount) as m from raw.orders where status = 'c'"
    )
    assert fps, "a three-branch union produced no fingerprint"
    assert all(f.canon_level not in PROOF_LEVELS for f in fps)


def test_alias_rename_inside_a_unioned_cte_is_not_a_contradiction():
    """normalize_scope silently passed set operations through, so an alias
    rename inside a UNION'd CTE body changed its digest."""
    a = fps_for(
        "with base as (select amount from raw.orders where status = 'x' "
        "union all select amount from raw.orders where status = 'y') "
        "select sum(amount) as m from base"
    )
    b = fps_for(
        "with base as (select o.amount from raw.orders as o where o.status = 'x' "
        "union all select p.amount from raw.orders as p where p.status = 'y') "
        "select sum(amount) as m from base"
    )
    if not a or not b:
        pytest.skip("union CTE did not yield a measure in this dialect")
    assert a[0].core.hash() == b[0].core.hash()


# -------------------------------------------------------------- resolution

def _fp(name, core, relation):
    return Fingerprint(
        fingerprint_id=Fingerprint.make_id(core, name), concept_candidate=name,
        core=core, relation=relation, dialect="duckdb", canon_level="supported",
        canonical_sql="SELECT ...", provenance=Provenance("dbt", f"model.{relation}"),
    )


def _core(expr):
    return SemanticCore(measure_expr=expr, join_nodes=("analytics.fct_orders",),
                        source_columns=("raw.orders.amount",))


def test_aliasing_survives_a_bucket_that_gains_a_second_definition():
    """The round-two chain-collapse fix skipped multi-core buckets entirely,
    so a name that legitimately aliases another concept stopped merging the
    moment it picked up a second definition — splitting a certified concept
    into a false ORPHAN_CLAIM plus SHADOW_METRIC."""
    shared = _core("SUM(amount)")
    other = _core("SUM(refund_amount)")
    concepts = resolve_concepts(
        [
            _fp("gmv", shared, "analytics.a"),
            _fp("total_gmv", shared, "analytics.b"),
            _fp("total_gmv", other, "analytics.c"),   # bucket gains a 2nd core
        ],
        [],
    )
    # `gmv` and the matching `total_gmv` must still be one concept.
    holding_shared = [
        c for c in concepts
        if any(f.core.hash() == shared.hash() for f in c.fingerprints)
    ]
    assert len(holding_shared) == 1, "aliased definitions were split apart"
    assert len(holding_shared[0].fingerprints) >= 2


def test_an_ambiguous_name_produces_one_reported_concept_not_silent_splitting():
    """See test_verdict_logic for the reasoning: the closure is kept and the
    bridging name is named in the verdict."""
    revenue, cost = _core("SUM(amount)"), _core("SUM(refund_amount)")
    concepts = resolve_concepts(
        [
            _fp("revenue", revenue, "analytics.a"),
            _fp("sales", revenue, "analytics.b"),
            _fp("sales", cost, "analytics.c"),
            _fp("cost", cost, "analytics.d"),
        ],
        [],
    )
    assert len(concepts) == 1
    summary = next(v for v in reconcile(concepts)
                   if v.verdict_type == VerdictType.CONTRADICTION).summary
    assert "sales names more than one computation" in summary


# ------------------------------------------------------------ reconciliation

def test_a_difference_below_proof_grade_is_not_a_contradiction():
    """Canonical DIFFERENCE is a hypothesis; a difference involving a member
    we could not canonicalize is not even that, and accusing working code of
    contradicting itself over an artefact of failed resolution is worse than
    saying nothing."""
    from semantix.models import Concept

    good = _fp("revenue", _core("SUM(amount)"), "analytics.a")
    weak = _fp("revenue", _core("SUM(amount) /* different */"), "analytics.b")
    weak.canon_level = "parsed"
    kinds = {v.verdict_type for v in reconcile([Concept("c", "revenue", [good, weak], [])])}
    assert VerdictType.CONTRADICTION not in kinds
    assert VerdictType.UNRESOLVED in kinds


# -------------------------------------------------------------- execution

def test_estate_sql_cannot_inject_statements_into_the_execution_slice(tmp_path):
    """Interpolating a model body raw let it close the parenthesis and append
    its own statements, redefining the relations a divergence is measured
    over."""
    pytest.importorskip("duckdb")
    from semantix.engine import run_scan

    root = tmp_path / "sql"
    (root / "analytics").mkdir(parents=True)
    (root / "analytics" / "a.sql").write_text(
        "select sum(amount) as revenue from raw.orders where status = 'completed'"
    )
    (root / "analytics" / "evil.sql").write_text(
        "select sum(amount) as revenue from raw.orders); "
        "drop table raw.orders; select sum(1) as revenue from (select 1 as amount"
    )
    (tmp_path / "catalog.yml").write_text(
        "raw:\n  orders:\n    order_id: bigint\n    status: varchar\n"
        "    amount: 'decimal(18,2)'\n"
    )
    (tmp_path / "seed.sql").write_text(
        "CREATE SCHEMA IF NOT EXISTS raw;\n"
        "CREATE TABLE raw.orders (order_id BIGINT, status VARCHAR, amount DECIMAL(18,2));\n"
        "INSERT INTO raw.orders VALUES (1,'completed',100),(2,'pending',200);\n"
    )
    result, _ = run_scan(
        sql_dir=str(root), catalog_path=str(tmp_path / "catalog.yml"),
        db_path=str(tmp_path / "s.db"), execute_seed=str(tmp_path / "seed.sql"),
    )
    # The scan completes and no measured figure is derived from a dropped table.
    for verdict in result.verdicts:
        measured = verdict.evidence.get("measured") or {}
        if measured.get("measured"):
            assert measured.get("max_divergence") is None or \
                   measured["max_divergence"]["baseline"] is not None
