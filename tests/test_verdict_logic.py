"""Verdicts must follow from the members, and only from them.

Round-two adversarial review, 2026-07-26. These are reconciliation and
clustering defects: correct fingerprints, wrong conclusion. Every test failed
before its fix.
"""

from semantix.models import (
    Claim,
    Concept,
    Fingerprint,
    Provenance,
    SemanticCore,
    VerdictType,
)
from semantix.services.reconcile import reconcile
from semantix.services.resolution import resolve_concepts

PROV = Provenance(system="dbt", object_id="model.x")
CLAIM_PROV = Provenance(system="metrics_yaml", object_id="metrics.x")


def core(expr: str, filters=()) -> SemanticCore:
    return SemanticCore(measure_expr=expr, filters=tuple(filters),
                        join_nodes=("analytics.fct_orders",),
                        source_columns=("raw.orders.amount",))


def fp(name: str, c: SemanticCore, relation="analytics.m") -> Fingerprint:
    return Fingerprint(
        fingerprint_id=Fingerprint.make_id(c, name), concept_candidate=name, core=c,
        relation=relation, dialect="duckdb", canon_level="supported",
        canonical_sql="SELECT ...", provenance=Provenance("dbt", f"model.{relation}"),
    )


def claim(name: str, c: SemanticCore | None, expressible=True) -> Claim:
    return Claim(
        claim_id=Claim.make_id(name, CLAIM_PROV, name), name=name, provenance=CLAIM_PROV,
        model="analytics.fct_orders", expr_raw="sum(...)", expressible=expressible,
        core=c, canon_level="supported" if expressible else "failed",
        error=None if expressible else "not machine-expressible",
    )


def kinds(verdicts):
    return {v.verdict_type for v in verdicts}


# ------------------------------------------------------------ clustering

def test_an_ambiguous_name_links_what_it_spans_and_says_so():
    """Two models both call their measure `sales` while computing different
    things: one aliases `revenue`, the other aliases `cost`.

    Concept identity is the transitive closure of "same name" and "same core",
    and both relations are required — so `sales` genuinely does bridge the two.
    Two attempts to break that transitivity failed in review (unioning whole
    buckets chained silently; excluding bridges split legitimate aliases into a
    false ORPHAN_CLAIM plus SHADOW_METRIC). The closure is kept and the
    ambiguity is REPORTED rather than resolved by a heuristic, so the reader
    can see why the concept is large."""
    revenue, cost = core("SUM(amount)"), core("SUM(fee)")
    concepts = resolve_concepts(
        [
            fp("revenue", revenue, "analytics.a"),
            fp("sales", revenue, "analytics.b"),
            fp("sales", cost, "analytics.c"),
            fp("cost", cost, "analytics.d"),
        ],
        [],
    )
    assert len(concepts) == 1
    contradiction = next(v for v in reconcile(concepts)
                         if v.verdict_type == VerdictType.CONTRADICTION)
    assert "sales names more than one computation" in contradiction.summary


def test_concept_labels_do_not_depend_on_input_order():
    """An order-dependent label fractures bitemporal history: the same estate
    rescanned yields a different concept id and the history splits in two."""
    c = core("SUM(amount)")
    fps = [fp("alpha", c, "analytics.a"), fp("beta", c, "analytics.b")]
    claims = [claim("zulu", c), claim("alpha", c)]
    forward = resolve_concepts(fps, claims)
    backward = resolve_concepts(list(reversed(fps)), list(reversed(claims)))
    assert [x.label for x in forward] == [x.label for x in backward]
    assert [x.concept_id for x in forward] == [x.concept_id for x in backward]


# ----------------------------------------------------------- reconciliation

def test_an_inexpressible_claim_blocks_certification():
    """Two declared definitions, one of which we could not translate. The
    expressible one matches the code — but certifying means asserting the
    OTHER human-written claim also agrees, which was never checked."""
    c = core("SUM(amount)")
    concept = Concept("c1", "revenue", [fp("revenue", c)],
                      [claim("revenue", c), claim("revenue_alt", None, expressible=False)])
    assert VerdictType.CERTIFIED not in kinds(reconcile([concept]))


def test_an_inexpressible_claim_does_not_mask_a_real_code_disagreement():
    """Two models genuinely disagree. A declared claim we cannot read must not
    downgrade that to 'unresolved' — the contradiction stands on its own."""
    concept = Concept(
        "c1", "revenue",
        [fp("revenue", core("SUM(amount)"), "analytics.a"),
         fp("revenue", core("SUM(amount - fee)"), "analytics.b")],
        [claim("revenue", None, expressible=False)],
    )
    assert VerdictType.CONTRADICTION in kinds(reconcile([concept]))


def test_two_conflicting_declarations_contradict_even_with_no_code():
    """Finance and the catalog both declare `revenue`, differently, and
    nothing computes it. That is a real, reportable disagreement."""
    concept = Concept("c1", "revenue", [],
                      [claim("revenue", core("SUM(amount)")),
                       claim("revenue", core("SUM(amount - fee)"))])
    assert VerdictType.CONTRADICTION in kinds(reconcile([concept]))


def test_shadow_severity_never_overruns_the_contradiction_band():
    """Severity drives triage order. An unbounded shadow score sorted ahead of
    real contradictions once enough sites computed it."""
    c = core("SUM(amount)")
    many = [fp("refund_rate", c, f"analytics.m{i}") for i in range(40)]
    shadow = next(v for v in reconcile([Concept("c1", "refund_rate", many, [])])
                  if v.verdict_type == VerdictType.SHADOW_METRIC)
    assert shadow.severity < 80


def test_blame_points_at_the_declaration_not_the_model_it_names():
    """A declared claim's `model` is the table it READS, not where the faulty
    text lives. When the declaration is the divergent member, naming that
    model as the site sends someone to correct working code instead of the
    YAML. (The declaration is baseline when there is one, so reaching this
    needs a second, disagreeing declaration.)"""
    matching = core("SUM(amount)")
    concept = Concept(
        "c1", "revenue",
        [fp("revenue", matching, "analytics.finance")],
        [claim("revenue", matching), claim("revenue", core("SUM(amount - fee)"))],
    )
    contradiction = next(v for v in reconcile([concept])
                         if v.verdict_type == VerdictType.CONTRADICTION)
    assert "declaration of revenue" in contradiction.summary
    assert "analytics.fct_orders diverges" not in contradiction.summary
