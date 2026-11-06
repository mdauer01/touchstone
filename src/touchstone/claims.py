"""A declared definition, canonicalized like computed SQL: `build_claim`
synthesizes the query a declaration claims to be and fingerprints it.

Its own module so the monitoring path (`touchstone check`, `monitor --pilot`)
reads approved definitions without loading the estate scan (touchstone.engine
re-exports it)."""

from __future__ import annotations

from touchstone.adapters.base import DeclaredSpec, spec_content_text
from touchstone.extraction.canonicalize import canonicalize
from touchstone.extraction.fingerprint import extract_fingerprints
from touchstone.extraction.registry import SchemaRegistry
from touchstone.models import CanonLevel, Claim, Provenance


def build_claim(spec: DeclaredSpec, registry: SchemaRegistry, dialect: str) -> Claim:
    """Give a declared definition the same canonical treatment as computed SQL
    (§4) by synthesizing the query it claims to be."""
    provenance = Provenance(system=spec.system, object_id=f"{spec.system}.{spec.name}", path=spec.path)
    claim = Claim(
        # Identity covers EVERYTHING the declaration says (model, filters,
        # group_by, untranslatable reasons) plus where it was said: keyed on
        # system|name|expr, two same-named declarations over different models
        # — or two refusals with expr=None — collapsed to one store row, and
        # every downstream reader (propose, publish, monitor) presented the
        # wrong declaration.
        claim_id=Claim.make_id(spec.name, provenance,
                               spec_content_text(spec) + "|" + (spec.path or "")),
        name=spec.name,
        provenance=provenance,
        description=spec.description,
        owner=spec.owner,
        model=spec.model,
        expr_raw=spec.expr,
        filters_raw=tuple(spec.filters),
        group_by_raw=tuple(spec.group_by),
    )
    # A claim the adapter could only PARTLY translate is not a weaker claim,
    # it is a different one. Reconciling it would certify or contradict a
    # definition nobody wrote, so it stays inexpressible and says why.
    # Checked BEFORE the expr/model check: most adapter refusals ship
    # expr=None, and the generic "no expr/model declared" was masking the
    # actual reason the adapter took care to write down.
    if spec.untranslatable:
        claim.error = (
            "could not translate part of the declaration, so it cannot be "
            "verified without inventing it: " + "; ".join(spec.untranslatable)
        )
        return claim

    if not spec.expr or not spec.model:
        claim.error = "no expr/model declared — not machine-expressible"
        return claim

    # A declaration that RE-AGGREGATES a model's aggregate output —
    # `sum(revenue)` over a mart that already sums — is a cross-LEVEL claim
    # the static comparison cannot make honestly: the mart's fingerprint is
    # over its sources, the claim is over the mart, and comparing them
    # produced a sev-96 CONTRADICTION blaming the mart's own internals for
    # faithfully restating itself. This is the standard metric-over-mart
    # pattern, so on a pilot's first scan the flagship report was mostly
    # top-severity noise. Refuse with the real reason; through-model
    # resolution is the Phase 1 fix.
    if spec.model and spec.expr:
        agg_cols = registry.aggregate_outputs(spec.model)
        if agg_cols:
            referenced = _columns_in_expr(spec.expr) & {c.lower() for c in agg_cols}
            if referenced and _is_aggregate_expr(spec.expr):
                claim.error = (
                    f"declares an aggregate over {', '.join(sorted(referenced))}, "
                    f"which {spec.model} already aggregates — a re-aggregation "
                    f"the static core cannot compare against the model's own "
                    f"definition; will not blame the model's internals "
                    f"(through-model resolution is Phase 1)"
                )
                return claim

    sql = _synthesize_claim_sql(spec, dialect)
    canon = canonicalize(sql, registry, dialect)
    claim.canon_level = canon.level.value
    if not canon.usable or canon.level == CanonLevel.PARSED:
        claim.error = canon.error or "canonicalization below proof grade"
        return claim

    fps = extract_fingerprints(
        canon, spec.model, provenance, dialect, registry
    )
    if len(fps) != 1:
        claim.error = f"expected exactly one measure in declared expr, found {len(fps)}"
        return claim

    claim.expressible = True
    claim.core = fps[0].core
    claim.canonical_sql = fps[0].canonical_sql
    # The COMPLETENESS GATE applies to declarations too. `claim.canon_level`
    # was set from canonicalize_sql, which only ever returns optimized /
    # qualified / parsed / failed — the INCOMPLETE downgrade happens inside
    # extract_fingerprints and was never copied back. So a declared definition
    # using a construct the core cannot model was labelled proof-grade, and
    # reconcile's `proof_grade` check — which reads exactly this field — fed it
    # straight into the CERTIFIED gate. Two declarations over different arrays
    # were provably indistinguishable at proof grade.
    claim.canon_level = fps[0].canon_level
    claim.gaps = fps[0].gaps
    claim.rules = fps[0].rules
    claim.parse_check = fps[0].parse_check
    if fps[0].gaps:
        claim.error = ("declaration uses constructs the semantic core cannot "
                       "represent: " + "; ".join(fps[0].gaps))
    return claim


def _columns_in_expr(expr: str) -> set[str]:
    """Lowercased column names a declared expression references."""
    import sqlglot
    from sqlglot import exp as _exp

    try:
        node = sqlglot.parse_one(expr)
    except Exception:  # noqa: BLE001 — arbitrary declared text
        return set()
    return {c.name.lower() for c in node.find_all(_exp.Column)}


def _is_aggregate_expr(expr: str) -> bool:
    import sqlglot
    from sqlglot import exp as _exp

    try:
        node = sqlglot.parse_one(expr)
    except Exception:  # noqa: BLE001
        return False
    return node.find(_exp.AggFunc) is not None


def _synthesize_claim_sql(spec: DeclaredSpec, dialect: str | None = None) -> str:
    parts = []
    for gb in spec.group_by:
        parts.append(gb)
    # Quoted in the dialect: metric-view measures carry display names
    # (`Net Revenue`), which unquoted made every declaration unreadable.
    from sqlglot import exp as _exp

    alias = _exp.to_identifier(spec.name, quoted=True).sql(dialect=dialect)
    parts.append(f"{spec.expr} AS {alias}")
    sql = f"SELECT {', '.join(parts)} FROM {spec.model}"
    if spec.filters:
        sql += " WHERE " + " AND ".join(f"({f})" for f in spec.filters)
    if spec.group_by:
        sql += " GROUP BY " + ", ".join(spec.group_by)
    return sql
