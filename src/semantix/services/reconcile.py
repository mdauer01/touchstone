"""Reconciliation engine (§5), static stage.

Doctrine (asymmetric equivalence): canonical-core equality at a proof-grade
canon level is *proof of sameness*; canonical difference is a *hypothesis of
difference*. Phase 0 has no execution harness, so difference-based verdicts
carry `method: static-canonical-diff` in their evidence and name exactly
which components diverge — the harness (Phase 1) upgrades them to numeric
divergence.

A concept can accrue several verdicts (e.g. CERTIFIED + ALIASING); the
report picks the primary by VERDICT_PRECEDENCE.
"""

from __future__ import annotations


from semantix.models import (
    Claim,
    Concept,
    Fingerprint,
    PROOF_LEVELS,
    Verdict,
    VerdictType,
)
from semantix.services.blame import blame_members
from semantix.services.resolution import normalize_name

_COMPONENTS = ["expression", "filters", "grain", "join graph", "physical lineage"]


def reconcile(concepts: list[Concept]) -> list[Verdict]:
    verdicts: list[Verdict] = []
    for concept in concepts:
        verdicts.extend(_reconcile_concept(concept))
    return sorted(verdicts, key=lambda v: (-v.severity, v.concept_label))


def _reconcile_concept(concept: Concept) -> list[Verdict]:
    fps = concept.fingerprints
    claims = concept.claims
    expressible = [c for c in claims if c.core is not None]
    verdicts: list[Verdict] = []

    core_hashes = sorted({fp.core.hash() for fp in fps})
    proof_grade = all(fp.canon_level in PROOF_LEVELS for fp in fps) and all(
        c.canon_level in PROOF_LEVELS for c in expressible
    )

    inexpressible = [c for c in claims if c.core is None]
    members = _member_evidence(fps, expressible)
    base_evidence = {
        "method": "static-canonical-diff",
        "members": members,
        "component_diff": _component_diff(members),
    }
    # A claim we could not translate is the reason several verdicts refuse to
    # certify, yet it appeared nowhere in the evidence: `members` holds only
    # the definitions that HAVE a canonical core. A reader was told the
    # concept could not be certified and given no way to see what blocked it.
    # Kept out of `members` deliberately — these rows have no components, so
    # they must not enter the component diff or blame arithmetic.
    if inexpressible:
        base_evidence["inexpressible_claims"] = _inexpressible_evidence(inexpressible)

    if fps and claims:
        claim_hashes = {c.core.hash() for c in expressible}
        all_hashes = set(core_hashes) | claim_hashes

        # Certification asserts that EVERY declared definition agrees with the
        # code. A claim we could not translate was silently dropped from the
        # comparison, so a concept certified while a human-written declaration
        # sat unexamined beside it.
        fully_expressible = len(expressible) == len(claims)

        if len(all_hashes) == 1 and expressible and proof_grade and fully_expressible:
            verdicts.append(
                _verdict(
                    VerdictType.CERTIFIED,
                    concept,
                    severity=5,
                    summary=(
                        f"Declared definition and {len(fps)} computed "
                        f"definition{'s' if len(fps) != 1 else ''} are canonically identical."
                    ),
                    evidence=base_evidence,
                )
            )
        elif len(all_hashes) > 1 and expressible and not proof_grade:
            # Canonical DIFFERENCE is only a hypothesis, and a difference
            # involving a member we could not canonicalize to proof grade is
            # not even that — it may be an artefact of what we failed to
            # resolve rather than a real disagreement. Reporting it as a
            # CONTRADICTION accuses working code.
            verdicts.append(
                _verdict(
                    VerdictType.UNRESOLVED,
                    concept,
                    severity=40,
                    summary=(
                        "Definitions differ, but at least one could not be canonicalized "
                        "to proof grade, so the difference may be an artefact of what "
                        "could not be resolved rather than a real disagreement."
                    ),
                    evidence=base_evidence,
                )
            )
        elif len(all_hashes) > 1 and expressible:
            matching, diverging = _split_by_claim(fps, expressible)
            blame = blame_members(members)
            base_evidence = {**base_evidence, "blame": blame}
            if matching:
                summary = (
                    f"Declared definition matches {_names(matching)}; "
                    f"{_blame_clause(blame, members)}"
                )
            else:
                summary = (
                    f"No computed definition matches the declared one; "
                    f"{_blame_clause(blame, members)}"
                )
            severity = 80 + 10 + min(10, 2 * len(fps))
            verdicts.append(
                _verdict(VerdictType.CONTRADICTION, concept, severity, summary, base_evidence)
            )
        elif not expressible and len(core_hashes) > 1:
            # The code disagrees with itself. An unreadable declaration does
            # not make that disagreement go away, and reporting it as merely
            # "unresolved" buries a real contradiction.
            blame = blame_members(members)
            verdicts.append(
                _verdict(
                    VerdictType.CONTRADICTION,
                    concept,
                    severity=80 + min(10, 2 * len(fps)),
                    summary=(
                        f"{len(fps)} computed definitions disagree; the declared "
                        f"definition could not be read, so neither can be confirmed "
                        f"correct. {_blame_clause(blame, members)}"
                    ),
                    evidence={**base_evidence, "blame": blame},
                )
            )
        elif not expressible:
            verdicts.append(
                _verdict(
                    VerdictType.UNRESOLVED,
                    concept,
                    severity=35,
                    summary=(
                        "Declared definition is not machine-expressible; static "
                        "verification impossible — requires the dynamic execution "
                        "harness (Phase 1)."
                    ),
                    evidence=base_evidence,
                )
            )
        else:
            # A single shared hash that still cannot certify. TWO distinct
            # things reach here — a member below proof grade, and a declared
            # definition we could not translate at all — and this branch
            # asserted the first unconditionally. When the real cause was an
            # untranslatable YAML claim, the reader was sent to fix
            # canonicalization while a human-written declaration sat
            # unexamined. Report the reason that actually holds.
            reasons = []
            if not proof_grade:
                reasons.append(
                    "at least one definition could not be canonicalized to proof grade"
                )
            if not fully_expressible:
                reasons.append(
                    f"{len(inexpressible)} declared definition(s) could not be "
                    f"translated and so were never compared: "
                    + _claim_names(inexpressible)
                )
            verdicts.append(
                _verdict(
                    VerdictType.UNRESOLVED,
                    concept,
                    severity=35,
                    summary=(
                        "Definitions appear identical, but " + "; ".join(reasons)
                        + " — equality is unproven."
                    ),
                    evidence=base_evidence,
                )
            )
    elif fps and len(core_hashes) > 1 and not proof_grade:
        verdicts.append(
            _verdict(
                VerdictType.UNRESOLVED,
                concept,
                severity=40,
                summary=(
                    f"{len(fps)} computed definitions differ, but at least one could "
                    "not be canonicalized to proof grade — the difference is unproven."
                ),
                evidence=base_evidence,
            )
        )
    elif fps and len(core_hashes) > 1:
        blame = blame_members(members)
        verdicts.append(
            _verdict(
                VerdictType.CONTRADICTION,
                concept,
                severity=80 + min(10, 2 * len(fps)),
                summary=(
                    f"{len(fps)} computed definitions of the same concept disagree; "
                    f"none is declared.{_ambiguity_clause(fps)} "
                    f"{_blame_clause(blame, members)}"
                ),
                evidence={**base_evidence, "blame": blame},
            )
        )

    if claims and not fps:
        # Two ontologies declaring the same concept differently is a real
        # disagreement even when nothing computes it — arguably the purest
        # one, since no code exists to arbitrate.
        declared_hashes = {c.core.hash() for c in expressible}
        if len(declared_hashes) > 1:
            blame = blame_members(members)
            verdicts.append(
                _verdict(
                    VerdictType.CONTRADICTION,
                    concept,
                    severity=80,
                    summary=(
                        f"{len(expressible)} declared definitions of this concept "
                        f"disagree, and nothing in the estate computes it. "
                        f"{_blame_clause(blame, members)}"
                    ),
                    evidence={**base_evidence, "blame": blame},
                )
            )
        verdicts.append(
            _verdict(
                VerdictType.ORPHAN_CLAIM,
                concept,
                severity=40,
                summary="Declared in the ontology but never computed anywhere in the scanned estate.",
                evidence=base_evidence,
            )
        )
    if fps and not claims:
        verdicts.append(
            _verdict(
                VerdictType.SHADOW_METRIC,
                concept,
                # Capped below the orphan/contradiction bands: severity drives
                # triage order, and an unbounded shadow score sorted ahead of
                # real contradictions once enough sites computed it.
                severity=min(39, 25 + 2 * (len(fps) - 1)),
                summary=(
                    f"Computed at {len(fps)} site{'s' if len(fps) != 1 else ''} in the "
                    "estate but absent from the declared ontology."
                ),
                evidence=base_evidence,
            )
        )

    alias_names = _alias_names(fps)
    if alias_names:
        verdicts.append(
            _verdict(
                VerdictType.ALIASING,
                concept,
                severity=30,
                summary=(
                    "Identical canonical definition computed under "
                    f"{len(alias_names)} different names: {', '.join(sorted(alias_names))}."
                ),
                evidence=base_evidence,
            )
        )

    return verdicts


def _verdict(vt: VerdictType, concept: Concept, severity: int, summary: str, evidence: dict) -> Verdict:
    return Verdict(
        verdict_type=vt,
        concept_id=concept.concept_id,
        concept_label=concept.label,
        severity=severity,
        summary=summary,
        evidence=evidence,
    )


def _member_evidence(fps: list[Fingerprint], expressible_claims: list[Claim]) -> list[dict]:
    members = []
    for claim in expressible_claims:
        members.append(
            {
                "member_id": claim.short_id,
                "kind": "declared",
                "name": claim.name,
                # The model a claim names is the table it READS, not where the
                # faulty text lives. Blame renders declared sites from
                # `system`, so a YAML fault is not reported against code.
                "where": claim.model or claim.provenance.system,
                "system": claim.provenance.system,
                "path": claim.provenance.path,
                "canon_level": claim.canon_level,
                "core_hash": claim.core.hash()[:12],
                "components": claim.core.components(),
            }
        )
    for fp in fps:
        members.append(
            {
                "member_id": fp.short_id,
                "kind": "computed",
                "name": fp.concept_candidate,
                "where": fp.relation,
                "system": fp.provenance.system,
                "path": fp.provenance.path,
                "canon_level": fp.canon_level,
                "core_hash": fp.core.hash()[:12],
                "components": fp.core.components(),
            }
        )
    return members


def _inexpressible_evidence(claims: list[Claim]) -> list[dict]:
    """The declarations that blocked comparison, and why — so "could not be
    translated" is an inspectable fact rather than an assertion."""
    return [
        {
            "member_id": claim.short_id,
            "kind": "declared",
            "name": claim.name,
            "system": claim.provenance.system,
            "path": claim.provenance.path,
            "canon_level": claim.canon_level,
            "expr_raw": claim.expr_raw,
            "reason": claim.error or "no machine-readable expression was declared",
        }
        for claim in claims
    ]


def _claim_names(claims: list[Claim]) -> str:
    return ", ".join(f"{c.name} ({c.provenance.system})" for c in claims) or "none"


def _component_diff(members: list[dict]) -> dict[str, bool]:
    """component -> True when all members agree on it."""
    diff = {}
    for comp in _COMPONENTS:
        values = {repr(m["components"][comp]) for m in members}
        diff[comp] = len(values) <= 1
    return diff


def _differing_components(members: list[dict]) -> list[str]:
    diff = _component_diff(members)
    out = [comp for comp, equal in diff.items() if not equal]
    return out or ["expression"]


def _split_by_claim(
    fps: list[Fingerprint], expressible_claims: list[Claim]
) -> tuple[list[str], list[str]]:
    claim_hashes = {c.core.hash() for c in expressible_claims}
    matching, diverging = [], []
    for fp in fps:
        target = matching if fp.core.hash() in claim_hashes else diverging
        target.append(f"{fp.relation} ({fp.provenance.system})")
    return matching, diverging


def _names(sites: list[str]) -> str:
    return ", ".join(sites) if sites else "nothing"


def _ambiguity_clause(fps: list[Fingerprint]) -> str:
    """Name the measure names that mean more than one thing.

    Concept identity is the transitive closure of "same name" and "same
    canonical core", so a name used for two different computations pulls the
    concepts it spans together. That is honest, but only useful if the report
    says WHICH name is doing it — otherwise the reader sees an inexplicably
    large concept.
    """
    by_name: dict[str, set[str]] = {}
    for fp in fps:
        by_name.setdefault(normalize_name(fp.concept_candidate), set()).add(fp.core.hash())
    ambiguous = sorted(n for n, hashes in by_name.items() if len(hashes) > 1)
    if not ambiguous:
        return ""
    return (f" These are grouped because {', '.join(ambiguous)} names more than one "
            f"computation, which links every definition it spans.")


def _blame_clause(blame: dict, members: list[dict]) -> str:
    """Human tail of a CONTRADICTION summary: per-site clause-level blame,
    capped at two sites so summaries stay one forwardable sentence."""
    divergent = blame.get("divergent", [])
    if not divergent:
        differing = _differing_components(members)
        return f"definitions diverge on {', '.join(differing)}."
    shown = [f"{d['where']} diverges: {d['sentence']}" for d in divergent[:2]]
    more = f" (+{len(divergent) - 2} more site(s))" if len(divergent) > 2 else ""
    return "; ".join(shown) + more + "."


def _alias_names(fps: list[Fingerprint]) -> set[str]:
    """Names that share one identical canonical core — proof-grade aliasing."""
    by_hash: dict[str, set[str]] = {}
    for fp in fps:
        if fp.canon_level in PROOF_LEVELS:
            by_hash.setdefault(fp.core.hash(), set()).add(normalize_name(fp.concept_candidate))
    for names in by_hash.values():
        if len(names) > 1:
            return names
    return set()
