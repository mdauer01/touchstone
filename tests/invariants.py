"""Global invariants, asserted over whole scans rather than single cases.

Three review rounds produced the same pattern: a fix is correct for the case
in front of it, the test mirrors the fix author's assumption, and the
adjacent case stays broken. Example-based tests written by whoever wrote the
fix cannot escape that.

These are properties that must hold for EVERY scan of EVERY estate. They are
applied across all fixtures by test_invariants.py, so a regression anywhere
fails here even when its own example was never imagined.
"""

from __future__ import annotations

from semantix.models import PROOF_LEVELS, CanonLevel, VerdictType

CERTIFYING = {VerdictType.CERTIFIED, VerdictType.ALIASING}


def check_scan(result, store=None) -> list[str]:
    """Return a list of invariant violations for one completed scan."""
    problems: list[str] = []
    problems += _no_certification_over_unread_estate(result)
    problems += _proof_grade_implies_no_gaps(result)
    problems += _every_artifact_is_accounted_for(result)
    problems += _coverage_matches_canon_levels(result)
    if store is not None:
        problems += _receipts_are_substantiated(result, store)
        problems += _history_matches_membership(result, store)
    return problems


def _no_certification_over_unread_estate(result) -> list[str]:
    """I1. A scan that could not read part of the estate cannot certify it.

    An unreadable artifact may define a competing version of any concept —
    that is precisely why it needed reading — so certification while any
    artifact is unread asserts agreement among definitions never seen.
    """
    # Both kinds of unread count: an artifact we could not obtain SQL for, and
    # one we obtained and could not parse. Either may define a competing
    # version of any concept.
    unread = [f"{s['system']}:{s['object_id']}" for s in result.skipped]
    unread += [f"unparseable×{result.canon_levels.get(CanonLevel.FAILED.value, 0)}"] \
        if result.canon_levels.get(CanonLevel.FAILED.value) else []
    if not unread:
        return []
    return [
        f"I1 {v.concept_label}: {v.verdict_type.value} issued while the estate was "
        f"incompletely read ({', '.join(sorted(unread))})"
        for v in result.verdicts
        if v.verdict_type in CERTIFYING
    ]


def _proof_grade_implies_no_gaps(result) -> list[str]:
    """I2. The completeness gate is not bypassable: anything with an unmodeled
    construct must sit outside PROOF_LEVELS."""
    return [
        f"I2 {fp.relation}.{fp.concept_candidate}: proof-grade ({fp.canon_level}) "
        f"despite unmodeled constructs {fp.gaps}"
        for fp in result.fingerprints
        if fp.gaps and fp.canon_level in PROOF_LEVELS
    ]


def _every_artifact_is_accounted_for(result) -> list[str]:
    """I3. Coverage may not be silently overstated: every artifact either
    produced a measure, is listed as unmeasured, or is listed as skipped."""
    produced = {f"{fp.provenance.system}:{fp.provenance.object_id}"
                for fp in result.fingerprints}
    accounted = produced | set(result.unmeasured)
    expected = result.artifacts
    if len(accounted) != expected:
        return [
            f"I3 {len(accounted)} artifacts accounted for but {expected} were scanned "
            f"(unmeasured={len(result.unmeasured)}, skipped={len(result.skipped)})"
        ]
    return []


def _coverage_matches_canon_levels(result) -> list[str]:
    """I4. The headline coverage number is derived from, and agrees with, the
    per-artifact canonicalization levels."""
    total = sum(result.canon_levels.values())
    if total != result.artifacts:
        return [f"I4 canon_levels sum to {total} but {result.artifacts} artifacts scanned"]
    if result.canon_levels.get(CanonLevel.FAILED.value) and result.coverage == 100.0:
        return ["I4 coverage reports 100% while artifacts failed to canonicalize"]
    return []


def _receipts_are_substantiated(result, store) -> list[str]:
    """I5. No receipt claims verification it cannot show evidence for, and no
    receipt is more favourable than the verdicts behind it."""
    problems = []
    for label in {v.concept_label for v in result.verdicts}:
        receipt = store.issue_receipt(label)
        if receipt is None:
            continue
        # While positive verdicts are suspended no receipt may state one at
        # all (semantix.conclusions); a definitive CONTRADICTION is reported
        # as a SUSPECTED_DIVERGENCE.
        if receipt.status in (VerdictType.CERTIFIED.value, VerdictType.ALIASING.value,
                              VerdictType.CONTRADICTION.value):
            problems.append(f"I5 {label}: receipt states {receipt.status}, a suspended "
                            "or definitive claim")
        worst = {v.verdict_type for v in result.verdicts if v.concept_label == label}
        if VerdictType.SUSPECTED_DIVERGENCE in worst and receipt.status != \
                VerdictType.SUSPECTED_DIVERGENCE.value:
            problems.append(
                f"I5 {label}: receipt says {receipt.status} while the scan found a "
                "SUSPECTED_DIVERGENCE"
            )
        if receipt.scope and receipt.scope.get("complete") is False and \
                receipt.status in (VerdictType.CERTIFIED.value, VerdictType.ALIASING.value):
            problems.append(
                f"I5 {label}: {receipt.status} signed over an incomplete scan"
            )
    return problems


def _history_matches_membership(result, store) -> list[str]:
    """I6. Derived valid time is drawn from recorded concept membership, not
    re-derived by matching names."""
    problems = []
    for concept in result.concepts:
        if not concept.fingerprints:
            continue
        intervals = store.history(concept.label)
        if not intervals:
            problems.append(
                f"I6 {concept.label}: has {len(concept.fingerprints)} computed "
                "definition(s) but no derived history"
            )
    return problems
