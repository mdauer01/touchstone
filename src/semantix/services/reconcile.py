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
                        f"{_declared_phrase(claims).capitalize()} and {len(fps)} "
                        f"computed definition{'s' if len(fps) != 1 else ''} are "
                        f"canonically identical."
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
                        "Definitions differ, but at least one is below proof grade "
                        f"({_proof_blockers(fps, expressible)}), so the difference "
                        "may be an artefact of what could not be resolved rather "
                        "than a real disagreement."
                    ),
                    evidence=base_evidence,
                )
            )
        elif len(all_hashes) > 1 and expressible:
            matching, diverging = _split_by_claim(fps, expressible)
            blame = blame_members(members)
            base_evidence = {**base_evidence, "blame": blame}
            # When the ONTOLOGIES disagree with each other, the code is not the
            # fault. `_split_by_claim` tests each fingerprint against the UNION
            # of claim hashes while blame measures divergence against ONE
            # baseline claim, so the same site was reported as both matching
            # and diverging in a single forwardable sentence — and the actual
            # finding (two ontologies declare this differently while the code
            # faithfully implements both) was never stated.
            if len(claim_hashes) > 1:
                summary = (
                    f"{len(claim_hashes)} distinct declared definitions of this "
                    f"concept disagree with each other (across "
                    f"{len(expressible)} declarations)"
                    + (f", and the estate computes {len(core_hashes)} distinct "
                       f"definition(s) across {len(fps)} site(s)"
                       if len(core_hashes) > 1 else
                       f"; the estate computes one definition at {len(fps)} site(s)")
                    + f". {_blame_clause(blame, members)}"
                )
            elif matching:
                summary = (
                    f"Declared definition matches {_names(matching)}; "
                    f"{_blame_clause(blame, members)}"
                )
            else:
                summary = (
                    f"No computed definition matches the declared one; "
                    f"{_blame_clause(blame, members)}"
                )
            # A bridging name is WHY an unrelated model was pulled into this
            # concept, and the clause was emitted only in the no-claims branch
            # — so as soon as a declaration existed the reader lost the
            # explanation and was handed a bare accusation instead, naming a
            # correct model that never claimed to compute this concept.
            summary += _ambiguity_clause(fps)
            severity = 80 + 10 + min(10, 2 * len(fps))
            verdicts.append(
                _verdict(VerdictType.CONTRADICTION, concept, severity, summary, base_evidence)
            )
        elif not expressible and len(core_hashes) > 1 and not proof_grade:
            # Same gate as the sibling branch above. Without it, an UNREADABLE
            # declaration — which contributes zero canonical information —
            # UPGRADED an explicitly-unproven difference into a severity-84
            # CONTRADICTION: identical fingerprints at 0% coverage produced
            # UNRESOLVED with no declaration and a top-band accusation with a
            # description-only one.
            verdicts.append(
                _verdict(
                    VerdictType.UNRESOLVED,
                    concept,
                    severity=40,
                    summary=(
                        f"{len(fps)} computed definitions differ and the declared "
                        "definition could not be read, but at least one definition "
                        "could not be canonicalized to proof grade — the difference "
                        "is unproven."
                    ),
                    evidence=base_evidence,
                )
            )
        elif not expressible and len(core_hashes) > 1:
            # The code disagrees with itself. An unreadable declaration does
            # not make that disagreement go away, and reporting it as merely
            # "unresolved" buries a real contradiction.
            blame = blame_members(members)
            plural = "neither" if len(fps) == 2 else "none of them"
            verdicts.append(
                _verdict(
                    VerdictType.CONTRADICTION,
                    concept,
                    severity=80 + min(10, 2 * len(fps)),
                    summary=(
                        f"{len(fps)} computed definitions disagree; "
                        f"{_declared_phrase(claims)} could not be read, so "
                        f"{plural} can be confirmed correct. "
                        f"{_blame_clause(blame, members)}"
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
                        + ((" Reason: " + _clip(inexpressible[0].error or "", 160))
                           if inexpressible and inexpressible[0].error else "")
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
                    "at least one definition is below proof grade "
                    f"({_proof_blockers(fps, expressible)})"
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
                    f"{len(fps)} computed definitions differ, but at least one is "
                    f"below proof grade ({_proof_blockers(fps, [])}) — the "
                    "difference is unproven."
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
        if len(declared_hashes) > 1 and proof_grade:
            blame = blame_members(members)
            verdicts.append(
                _verdict(
                    VerdictType.CONTRADICTION,
                    concept,
                    severity=80,
                    summary=(
                        f"{len(declared_hashes)} distinct declared definitions of "
                        f"this concept disagree (across {len(expressible)} "
                        f"declarations), and nothing in the estate computes it. "
                        f"{_blame_clause(blame, members)}"
                    ),
                    evidence={**base_evidence, "blame": blame},
                )
            )
        elif len(declared_hashes) > 1:
            # The same proof-grade gate as every sibling branch — but with an
            # UNRESOLVED voice, not silence. Gating alone meant a disagreement
            # between two PROOF-GRADE ontology declarations was swallowed
            # whenever an unrelated third declaration was merely incomplete:
            # the concept's only verdict became ORPHAN_CLAIM, with no mention
            # anywhere that the human-written declarations differ.
            verdicts.append(
                _verdict(
                    VerdictType.UNRESOLVED,
                    concept,
                    severity=40,
                    summary=(
                        f"{len(declared_hashes)} distinct declared definitions of "
                        "this concept appear to disagree, but at least one "
                        "declaration is below proof grade "
                        f"({_proof_blockers(fps, expressible)}) — the disagreement "
                        "is unproven. Nothing in the estate computes it."
                    ),
                    evidence=base_evidence,
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

    for alias_names in _alias_groups(fps):
        # Count NORMALIZED names, print raw spellings: `GMV`/`gmv`/
        # `gross_merchandise_value` is two names in three spellings, and
        # "3 different names" overcounted the aliasing.
        distinct = len({normalize_name(n) for n in alias_names})
        verdicts.append(
            _verdict(
                VerdictType.ALIASING,
                concept,
                severity=30,
                summary=(
                    "Identical canonical definition computed under "
                    f"{distinct} different names: {', '.join(sorted(alias_names))}."
                ),
                evidence=base_evidence,
            )
        )

    return verdicts


def _copy_evidence(evidence: dict) -> dict:
    """One evidence dict PER VERDICT.

    `base_evidence` was shared by every verdict a concept produced, so
    execution writing `verdict.evidence["measured"] = ...` on one of them
    attached a measured result to verdicts it had deliberately not measured —
    an UNRESOLVED whose summary says the difference is unproven, carrying
    evidence asserting a measured total. Scan-level counts and per-verdict
    evidence then disagreed about what was measured.
    """
    return dict(evidence)


def _verdict(vt: VerdictType, concept: Concept, severity: int, summary: str, evidence: dict) -> Verdict:
    evidence = _copy_evidence(evidence)
    if vt in (VerdictType.CERTIFIED, VerdictType.ALIASING):
        evidence["proof"] = _proof_basis(concept)
    return Verdict(
        verdict_type=vt,
        concept_id=concept.concept_id,
        concept_label=concept.label,
        severity=severity,
        summary=summary,
        evidence=evidence,
    )


def _proof_basis(concept: Concept) -> dict:
    """What a POSITIVE verdict rests on: the policy, the rules that produced
    the proof-grade forms it compared, and the policy's assumptions."""
    from semantix.extraction.proof import ASSUMPTIONS, POLICY_ID

    members = [*concept.fingerprints, *concept.claims]
    rules = sorted({r for m in members if m.canon_level in PROOF_LEVELS
                    for r in (m.rules or ())})
    return {"policy": POLICY_ID, "rules": rules, "assumptions": list(ASSUMPTIONS)}


def _proof_blockers(fps: list[Fingerprint], expressible: list[Claim]) -> str:
    """Name each below-proof-grade member with its ACTUAL cause.

    "Could not be canonicalized to proof grade" was asserted for every
    blocker, but CanonLevel.INCOMPLETE means canonicalization WORKED and the
    SemanticCore does not model a construct — a TABLESAMPLE in the declared
    model, say. The old text sent the reader to fix canonicalization while
    the gap sat named in `claim.error`, unreachable from the verdict.
    """
    sites = _site_names(fps)
    parts: list[str] = []
    for fp, site in zip(fps, sites):
        if fp.canon_level in PROOF_LEVELS:
            continue
        if fp.gaps:
            parts.append(f"{site} cannot be compared at proof grade: "
                         + ", ".join(fp.gaps[:3]))
        else:
            parts.append(f"{site} canonicalized only to {fp.canon_level}")
    for claim in expressible:
        if claim.canon_level in PROOF_LEVELS:
            continue
        label = f"the {claim.provenance.system} declaration of {claim.name}"
        if claim.gaps:
            parts.append(f"{label} cannot be compared at proof grade: " + ", ".join(claim.gaps))
        elif claim.error:
            parts.append(f"{label}: {claim.error}")
        else:
            parts.append(f"{label} canonicalized only to {claim.canon_level}")
    # Bounded like every sibling clause: 500 below-grade sites once built an
    # 84 KiB single sentence, stored in the DB and dropped into a PR comment.
    if len(parts) > 3:
        parts = parts[:3] + [f"(+{len(parts) - 3} more)"]
    return "; ".join(parts) or "cause unrecorded"


def _site_names(fps: list[Fingerprint]) -> list[str]:
    """Per-member human site names, aligned with `fps` by POSITION.

    Sites were named by RELATION, which is exactly what is non-unique in an
    estate that defines a relation twice — the round-four
    "matches-and-diverges" self-contradictory sentence recurred verbatim
    there. And the first fix keyed the map on short_id, which is CONTENT-
    addressed: two models at different relations computing the identical
    measure share a short_id, so the dict collapsed them and stamped one
    team's relation on the other team's member — misattribution in the
    verdict evidence itself. Positional alignment has neither problem.
    """
    counts: dict[str, int] = {}
    for fp in fps:
        counts[fp.relation] = counts.get(fp.relation, 0) + 1
    return [
        (f"{fp.relation} [{fp.provenance.object_id}]"
         if counts[fp.relation] > 1 else fp.relation)
        for fp in fps
    ]


def _member_evidence(fps: list[Fingerprint], expressible_claims: list[Claim]) -> list[dict]:
    sites = _site_names(fps)
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
    for fp, site in zip(fps, sites):
        members.append(
            {
                "member_id": fp.short_id,
                "kind": "computed",
                "name": fp.concept_candidate,
                "where": site,
                "system": fp.provenance.system,
                "path": fp.provenance.path,
                "canon_level": fp.canon_level,
                "core_hash": fp.core.hash()[:12],
                "components": fp.core.components(),
            }
        )
    return members


def _clip(text: str, limit: int) -> str:
    """Cut at a word boundary with an ellipsis — `…cannot rule out sema` in a
    verdict summary read as a typo, not a truncation."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + " …"


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


def _declared_phrase(claims: list[Claim]) -> str:
    """"the declared definition" is wrong when two ontologies declare it."""
    if len(claims) > 1:
        return f"{len(claims)} declared definitions"
    return "the declared definition"


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
    sites = _site_names(fps)
    matching, diverging = [], []
    for fp, site in zip(fps, sites):
        target = matching if fp.core.hash() in claim_hashes else diverging
        target.append(f"{site} ({fp.provenance.system})")
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
    # Report the name the ESTATE uses, not its normalized form. `orders.count`
    # and `orders_count` are two different names that normalize to one string,
    # so the clause named a token no artifact contains and the stated reason
    # for the grouping was false — a reader grepping for it finds one model.
    raw: dict[str, set[str]] = {}
    for fp in fps:
        key = normalize_name(fp.concept_candidate)
        by_name.setdefault(key, set()).add(fp.core.hash())
        raw.setdefault(key, set()).add(fp.concept_candidate)
    ambiguous = sorted(n for n, hashes in by_name.items() if len(hashes) > 1)
    if not ambiguous:
        return ""
    # TWO different stated reasons, because two different things happen. A
    # single spelling used for two computations genuinely "names more than
    # one computation". Two DIFFERENT spellings that normalize together
    # (`orders.count` / `orders_count`) each name exactly one — asserting
    # otherwise sent a reader grepping for a multi-use name that has one use.
    # The true reason there is the normalization collision itself.
    clauses = []
    for key in ambiguous:
        spellings = sorted(raw[key])
        per_spelling = {sp: {fp.core.hash() for fp in fps
                             if fp.concept_candidate == sp} for sp in spellings}
        multi_use = sorted(sp for sp, hashes in per_spelling.items() if len(hashes) > 1)
        if multi_use:
            clauses.append(f"{', '.join(multi_use)} "
                           f"{'names' if len(multi_use) == 1 else 'name'} more than "
                           f"one computation")
        # NOT elif: a key can have BOTH properties (one spelling used twice
        # AND a second spelling normalizing in) — first-wins told the reader
        # only half of why the concept grew.
        if len(spellings) > 1:
            clauses.append(
                f"{', '.join(spellings)} are treated as the same name"
                + ("" if multi_use else " and denote different computations"))
    return (f" These are grouped because {'; and because '.join(clauses)}, "
            f"which links every definition the name spans.")


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


def _alias_groups(fps: list[Fingerprint]) -> list[set[str]]:
    """Every set of names sharing one identical canonical core.

    Three defects in the previous single-set version, all in the direction of
    over-claiming:

    * it returned on the FIRST group found, so a second, unrelated alias set in
      the same concept was reported nowhere;
    * a name that denotes MORE than one computation was still listed, so
      ALIASING asserted "alpha and beta are identical" when only one of the two
      things called `alpha` was — a reader consolidating on that merges a SUM
      and an AVG. Ambiguous names are now excluded from the claim;
    * names were compared AFTER normalization, so `revenue.total` and
      `revenue_total` collapsed to one name and the aliasing went unreported,
      while the converse produced a summary naming a string no artifact uses.
      Grouping is still normalized (that is what makes them the same concept)
      but the names REPORTED are the ones the estate actually contains.
    """
    by_hash: dict[str, set[str]] = {}
    name_hashes: dict[str, set[str]] = {}
    raw_names: dict[str, set[str]] = {}
    for fp in fps:
        if fp.canon_level not in PROOF_LEVELS:
            continue
        key = normalize_name(fp.concept_candidate)
        digest = fp.core.hash()
        by_hash.setdefault(digest, set()).add(key)
        name_hashes.setdefault(key, set()).add(digest)
        raw_names.setdefault(key, set()).add(fp.concept_candidate)

    groups = []
    for names in by_hash.values():
        # A name meaning more than one thing cannot be said to BE another name.
        unambiguous = {n for n in names if len(name_hashes[n]) == 1}
        if len(unambiguous) > 1:
            groups.append({raw for n in unambiguous for raw in raw_names[n]})
    return groups
