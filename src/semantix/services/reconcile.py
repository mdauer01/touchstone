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

    members = _member_evidence(fps, expressible)
    base_evidence = {
        "method": "static-canonical-diff",
        "members": members,
        "component_diff": _component_diff(members),
    }

    if fps and claims:
        claim_hashes = {c.core.hash() for c in expressible}
        all_hashes = set(core_hashes) | claim_hashes

        if len(all_hashes) == 1 and expressible and proof_grade:
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
            # single shared hash but a member below proof grade
            verdicts.append(
                _verdict(
                    VerdictType.UNRESOLVED,
                    concept,
                    severity=35,
                    summary=(
                        "Definitions appear identical but at least one could not be "
                        "canonicalized to proof grade; equality is unproven."
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
                    f"none is declared. {_blame_clause(blame, members)}"
                ),
                evidence={**base_evidence, "blame": blame},
            )
        )

    if claims and not fps:
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
                severity=25 + 2 * (len(fps) - 1),
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
                "where": claim.model or claim.provenance.system,
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
                "path": fp.provenance.path,
                "canon_level": fp.canon_level,
                "core_hash": fp.core.hash()[:12],
                "components": fp.core.components(),
            }
        )
    return members


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
