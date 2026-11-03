"""Reading the ENGINE's judgment under the conclusions policy.

While positive verdicts are suspended (touchstone.conclusions), users see
experimental comparisons and suspected differences; the engine's own result
travels beside them. Tests that pin the engine's judgment — "never
conforming", "a violation with code X", "certifies these two" — read it
through these helpers, so the policy and the judgment are tested separately
and neither assertion is weakened.
"""

from __future__ import annotations

from touchstone.models import VerdictType


def _evidence(v) -> dict:
    return (v.evidence if hasattr(v, "evidence") else v.get("evidence")) or {}


def engine_verdict(v) -> VerdictType:
    """What the engine concluded for a scan Verdict (or stored row)."""
    ev = _evidence(v)
    for key in ("experimental", "suspected"):
        if isinstance(ev.get(key), dict) and ev[key].get("verdict"):
            return VerdictType(ev[key]["verdict"])
    return v.verdict_type if hasattr(v, "verdict_type") else VerdictType(v["verdict"])


def engine_kinds(verdicts, label: str | None = None) -> set[VerdictType]:
    return {engine_verdict(v) for v in verdicts
            if label is None or (v.concept_label if hasattr(v, "concept_label")
                                 else v["concept_label"]) == label}


def engine_kind(f: dict) -> str:
    """What the monitor's rules concluded for a finding."""
    if isinstance(f.get("experimental"), dict) and f["experimental"].get("kind"):
        return f["experimental"]["kind"]
    if f.get("kind") == "suspected_difference":
        return "definition_violation"
    return f["kind"]


def engine_conformance(f: dict) -> str:
    if isinstance(f.get("experimental"), dict) and f["experimental"].get("conformance"):
        return f["experimental"]["conformance"]
    if f.get("kind") == "suspected_difference":
        return "violation"
    return f.get("conformance")


def engine_codes(f: dict) -> list[str]:
    exp = f.get("experimental")
    reasons = exp.get("reasons") if isinstance(exp, dict) else f.get("reasons")
    return [r["code"] for r in reasons or []]


def engine_funnel(fn: dict) -> dict:
    """The funnel's buckets as the rules decided them (conforming = the
    experimental matches, violations = the suspected differences)."""
    return {**fn, "conforming": fn["experimental_matches"],
            "conforming_by_shape": fn["experimental_matches_by_shape"],
            "contested": fn["experimental_contested"],
            "violations": fn["suspected_differences"],
            "checked": fn["experimental_matches"] + fn["suspected_differences"]}
