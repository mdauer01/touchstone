"""Verdict matrix over synthetic concepts."""

from touchstone.models import (
    Claim,
    Concept,
    Fingerprint,
    Provenance,
    SemanticCore,
    VerdictType,
)
from touchstone.services.reconcile import reconcile
from touchstone.services.resolution import resolve_concepts

PROV = Provenance(system="dbt", object_id="model.x")
CLAIM_PROV = Provenance(system="metrics_yaml", object_id="metrics.x")

CORE_A = SemanticCore(
    measure_expr='SUM(t0."amount" - t0."refund_amount")',
    filters=("t0.\"status\" = 'completed'",),
    grain=("DATE_TRUNC('MONTH', t0.\"order_date\")",),
    join_nodes=("analytics.fct_orders",),
    source_columns=("raw.orders.amount", "raw.orders.refund_amount"),
)
CORE_B = SemanticCore(
    measure_expr='SUM(t0."amount")',
    filters=("t0.\"status\" IN ('completed', 'pending')",),
    grain=("DATE_TRUNC('MONTH', t0.\"order_date\")",),
    join_nodes=("analytics.stg_orders",),
    source_columns=("raw.orders.amount",),
)


def fp(name: str, core: SemanticCore, relation: str = "analytics.m1", level: str = "supported") -> Fingerprint:
    return Fingerprint(
        fingerprint_id=Fingerprint.make_id(core, name),
        concept_candidate=name,
        core=core,
        relation=relation,
        dialect="duckdb",
        canon_level=level,
        canonical_sql="SELECT ...",
        provenance=Provenance(system="dbt", object_id=f"model.{relation}"),
    )


def claim(name: str, core: SemanticCore | None, expressible: bool = True) -> Claim:
    return Claim(
        claim_id=Claim.make_id(name, CLAIM_PROV, name),
        name=name,
        provenance=CLAIM_PROV,
        model="analytics.fct_orders",
        expr_raw="sum(...)",
        expressible=expressible,
        core=core,
        canon_level="supported" if expressible else "failed",
    )


def verdict_types(verdicts):
    return {v.verdict_type for v in verdicts}


def test_certified():
    concept = Concept("c1", "net_revenue", [fp("net_revenue", CORE_A)], [claim("net_revenue", CORE_A)])
    verdicts = reconcile([concept])
    assert verdict_types(verdicts) == {VerdictType.CERTIFIED}


def test_contradiction_names_the_diverging_site():
    concept = Concept(
        "c1",
        "net_revenue",
        [fp("net_revenue", CORE_A, "analytics.finance"), fp("net_revenue", CORE_B, "analytics.growth")],
        [claim("net_revenue", CORE_A)],
    )
    verdicts = reconcile([concept])
    assert verdict_types(verdicts) == {VerdictType.CONTRADICTION}
    v = verdicts[0]
    assert "analytics.growth" in v.summary and "diverges" in v.summary
    assert v.evidence["component_diff"]["expression"] is False
    assert v.evidence["component_diff"]["grain"] is True
    assert v.severity >= 90


def test_contradiction_between_shadows_only():
    concept = Concept(
        "c1", "net_revenue",
        [fp("net_revenue", CORE_A), fp("net_revenue", CORE_B, "analytics.m2")],
        [],
    )
    types = verdict_types(reconcile([concept]))
    assert VerdictType.CONTRADICTION in types
    assert VerdictType.SHADOW_METRIC in types


def test_orphan_claim():
    concept = Concept("c1", "customer_ltv", [], [claim("customer_ltv", None, expressible=False)])
    assert verdict_types(reconcile([concept])) == {VerdictType.ORPHAN_CLAIM}


def test_shadow_metric():
    concept = Concept("c1", "refund_rate", [fp("refund_rate", CORE_B)], [])
    assert verdict_types(reconcile([concept])) == {VerdictType.SHADOW_METRIC}


def test_aliasing_with_certification():
    concept = Concept(
        "c1", "gmv",
        [fp("total_gmv", CORE_A, "analytics.m1"), fp("gross_merchandise_value", CORE_A, "analytics.m2")],
        [claim("gmv", CORE_A)],
    )
    types = verdict_types(reconcile([concept]))
    assert types == {VerdictType.CERTIFIED, VerdictType.ALIASING}


def test_inexpressible_claim_with_computed_members_is_unresolved():
    concept = Concept(
        "c1", "net_revenue",
        [fp("net_revenue", CORE_A)],
        [claim("net_revenue", None, expressible=False)],
    )
    types = verdict_types(reconcile([concept]))
    assert VerdictType.UNRESOLVED in types


def test_parsed_level_equality_is_not_certified():
    concept = Concept(
        "c1", "net_revenue",
        [fp("net_revenue", CORE_A, level="parsed")],
        [claim("net_revenue", CORE_A)],
    )
    types = verdict_types(reconcile([concept]))
    assert VerdictType.CERTIFIED not in types
    assert VerdictType.UNRESOLVED in types


def test_resolution_merges_aliases_by_core_hash():
    fps = [
        fp("total_gmv", CORE_A, "analytics.m1"),
        fp("gross_merchandise_value", CORE_A, "analytics.m2"),
        fp("net_revenue", CORE_B, "analytics.m3"),
    ]
    claims = [claim("gmv", CORE_A)]
    concepts = resolve_concepts(fps, claims)
    labels = {c.label: c for c in concepts}
    assert set(labels) == {"gmv", "net_revenue"}
    assert len(labels["gmv"].fingerprints) == 2
    assert len(labels["gmv"].claims) == 1
