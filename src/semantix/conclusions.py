"""What anyone may RELY on: one policy for effective verdicts and reliance.

Nine successive review rounds found new false certifications, the last ones
in how SQL is PARSED (sqlglot binds `%` and `IS` differently from the
engines). A guard operating on an incorrectly parsed tree may never recognize
the dangerous construct, so refusing the known classes does not establish
the soundness of what remains. Until an explicitly defined grammar,
justified transformations and evidence that parsing matches the target
engines exist (docs/proof-policy.md), semantix reports no positive verdict as
a conclusion:

* CERTIFIED and ALIASING (scan) and conforming / contested uses (monitor) are
  EXPERIMENTAL comparisons — kept for development, never a conclusion;
* a definitive negative that rests on the same interpretation (CONTRADICTION,
  a definition violation) is a SUSPECTED difference, never an accusation.

Relabeling is not the point; trust decisions are. Every consumer that
decides whether something may be relied on asks `reliance()`, which answers
three questions separately — signature authentic, estate unchanged, semantic
claim usable — and the first two can never establish the third. Original
signed receipts and stored rows are never rewritten: today's assessment is
reported beside them.

Mapping is idempotent: applied when results are produced AND again wherever
stored rows are read, so a store written before this policy surfaces under
it too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

POSITIVE_VERDICTS = "suspended"
SUSPENSION_REASON = (
    "positive verdicts are suspended: repeated reviews found false certifications, "
    "including SQL the parser reads differently from the engines, so an equality "
    "is an experimental comparison, not a conclusion")
SUSPECTED_REASON = (
    "the comparison rests on SQL interpretation that is not verified — a suspected "
    "difference, not a conclusion")

# Scan verdict values (models.VerdictType) by role.
POSITIVE_SCAN = frozenset({"CERTIFIED", "ALIASING"})
DEFINITIVE_NEGATIVE_SCAN = frozenset({"CONTRADICTION"})
SUSPECTED_DIVERGENCE = "SUSPECTED_DIVERGENCE"

# Monitor finding kinds by role.
POSITIVE_MONITOR = frozenset({"conforming_use", "contested_use"})
DEFINITIVE_NEGATIVE_MONITOR = frozenset({"definition_violation"})
SUSPECTED_DIFFERENCE = "suspected_difference"


def suspended() -> bool:
    return POSITIVE_VERDICTS == "suspended"


# --------------------------------------------------------------- scan verdicts

def effective_scan_fields(verdict: str, severity: int, summary: str,
                          evidence: dict | None) -> tuple[str, int, str, dict]:
    """(verdict, severity, summary, evidence) as reported under the policy in
    force. Idempotent; the engine's own result travels in
    evidence["experimental"] (positives) or evidence["suspected"] (negatives)."""
    evidence = dict(evidence or {})
    if not suspended():
        return verdict, severity, summary, evidence
    if verdict in POSITIVE_SCAN:
        experimental = {"verdict": verdict, "summary": summary}
        if "proof" in evidence:
            experimental["proof"] = evidence.pop("proof")
        evidence["experimental"] = experimental
        if summary.startswith(_EXPERIMENTAL_PREFIX):   # already mapped text
            return "UNRESOLVED", severity, summary, evidence
        return ("UNRESOLVED", severity,
                f"{_EXPERIMENTAL_PREFIX}{verdict.lower()} under "
                f"the experimental rules ({SUSPENSION_REASON}). {summary}", evidence)
    if verdict in DEFINITIVE_NEGATIVE_SCAN:
        evidence["suspected"] = {"verdict": verdict, "summary": summary,
                                 "observations_unverified": True}
        if summary.startswith(_SUSPECTED_PREFIX):   # already mapped text
            return SUSPECTED_DIVERGENCE, severity, summary, evidence
        return (SUSPECTED_DIVERGENCE, severity,
                f"{_SUSPECTED_PREFIX}{SUSPECTED_REASON}): {requalify_text(summary)}",
                evidence)
    return verdict, severity, summary, evidence


_EXPERIMENTAL_PREFIX = "Experimental comparison, not a conclusion — "
_SUSPECTED_PREFIX = "Suspected divergence ("


# Engine sentences that state an equality or a disagreement as fact, and how
# they read under the policy. A suspected divergence's summary carried
# "Declared definition matches X" verbatim into JSON, HTML, receipts and the
# warehouse — an unlabelled equality inside every report of the divergence.
_SCAN_PHRASES = (
    ("Declared definition matches ",
     "Declared definition appears to match (experimental comparison) "),
    ("No computed definition matches the declared one",
     "No computed definition appears to match the declared one (experimental "
     "comparison)"),
    ("disagree with each other", "appear to disagree with each other"),
)


def requalify_text(text: str) -> str:
    """An engine sentence reworded as observation, not conclusion. Idempotent."""
    if not suspended() or not isinstance(text, str):
        return text
    for strong, weak in _SCAN_PHRASES:
        if weak not in text:
            text = text.replace(strong, weak)
    return text


def effective_verdict(verdict) -> None:
    """Apply the policy to an engine Verdict in place."""
    from semantix.models import VerdictType

    value, severity, summary, evidence = effective_scan_fields(
        verdict.verdict_type.value, verdict.severity, verdict.summary, verdict.evidence)
    verdict.verdict_type = VerdictType(value)
    verdict.severity = severity
    verdict.summary = summary
    verdict.evidence = evidence


def effective_row(row: dict) -> dict:
    """A stored verdict row (store.verdicts_for_scan) under the policy."""
    value, severity, summary, evidence = effective_scan_fields(
        row["verdict"], row.get("severity", 0), row.get("summary", ""),
        row.get("evidence"))
    out = {**row, "verdict": value, "severity": severity, "summary": summary,
           "evidence": evidence}
    if value != row["verdict"]:
        out["stored_verdict"] = row["verdict"]
    return out


def engine_row(row: dict) -> dict:
    """The inverse view of `effective_row`: the stored row as the ENGINE
    concluded it (verdict, summary and evidence, with a positive's proof back
    under `proof`), keeping the effective summary as `effective_summary`.
    Guards that must still run on a suspended claim — "cannot certify for a
    future instant", unread estate, stale freshness — evaluate this, so their
    disclosures are not lost; the policy is applied once, at the end."""
    ev = dict(row.get("evidence") or {})
    out = {**row, "effective_summary": row.get("summary", "")}
    for key in ("experimental", "suspected"):
        block = ev.get(key)
        if isinstance(block, dict) and block.get("verdict"):
            out["verdict"] = block["verdict"]
            out["summary"] = block.get("summary", row.get("summary", ""))
            ev.pop(key)
            if key == "experimental" and "proof" in block:
                ev["proof"] = block["proof"]
            out["evidence"] = ev
            return out
    if row.get("stored_verdict"):
        out["verdict"] = row["stored_verdict"]
    return out


# ------------------------------------------------------------ monitor findings

def effective_finding(f: dict) -> dict:
    """A monitor finding under the policy. Idempotent."""
    if not suspended():
        return f
    kind = f.get("kind")
    if kind in POSITIVE_MONITOR:
        experimental = {k: f.get(k) for k in ("kind", "conformance", "shape", "summary",
                                              "variation", "reasons") if k in f}
        severity = f.get("severity", 0) if kind == "contested_use" else 0
        return _requalify({**f, "kind": "unverifiable", "conformance": "unverifiable",
                           "severity": severity, "experimental": experimental,
                           "reasons": [{"code": "positive_suspended",
                                        "detail": SUSPENSION_REASON}],
                           "summary": (f"experimental match, not a conclusion — "
                                       f"{f.get('summary', '')}")})
    if kind in DEFINITIVE_NEGATIVE_MONITOR:
        return _requalify({**f, "kind": SUSPECTED_DIFFERENCE, "conformance": "suspected",
                           "observations_unverified": True,
                           "summary": f"suspected difference ({SUSPECTED_REASON}): "
                                      f"{f.get('summary', '')}"})
    return _requalify(f)


# Evidence strings that name another rule outcome in conclusion words.
_OUTCOME_WORDS = {"(conforming)": "(experimental match)",
                  "(violation)": "(suspected difference)"}


_MATCH_QUALIFIER = "under the experimental rules (not a conclusion): "


def _requalify(f: dict) -> dict:
    """Evidence fields that name an equality, worded as what they are: the
    engine's canonical comparison, an experimental one. Idempotent."""
    text = f.get("calculation_matches")
    if isinstance(text, str):
        for word, weaker in _OUTCOME_WORDS.items():
            text = text.replace(word, weaker)
        if not text.startswith(_MATCH_QUALIFIER) and not any(
                weaker in text for weaker in _OUTCOME_WORDS.values()):
            text = _MATCH_QUALIFIER + text
        f = {**f, "calculation_matches": text}
    if f.get("also_matches"):
        f = {**f, "also_matches_basis": "experimental comparison, not a conclusion"}
    if f.get("kind") == "candidate_alt_definition" and not f.get("observations_unverified"):
        f = {**f, "observations_unverified": True,
             "summary": f"candidate, suspected ({SUSPECTED_REASON}): {f.get('summary', '')}"}
    return f


# ------------------------------------------------------------------- reliance

@dataclass(frozen=True)
class Reliance:
    """Three separate answers. Only the third authorizes use, and neither of
    the first two can establish it: an authentic receipt over an unchanged
    estate still states a claim this policy does not let anyone rely on."""

    signature_authentic: bool | None   # None: not checked
    estate_unchanged: bool | None      # None: not checked
    semantic_claim_usable: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def current_use(self) -> bool:
        return bool(self.signature_authentic and self.estate_unchanged
                    and self.semantic_claim_usable)

    def as_dict(self) -> dict[str, Any]:
        return {"signature_authentic": self.signature_authentic,
                "estate_unchanged": self.estate_unchanged,
                "semantic_claim_usable": self.semantic_claim_usable,
                "current_use": self.current_use,
                "reasons": list(self.reasons),
                "policy": {"positive_verdicts": POSITIVE_VERDICTS}}


def signed_status_label(status) -> str:
    """A receipt's SIGNED status as shown beside today's assessment: a legacy
    positive or definitive negative is named as what was signed, never as a
    current conclusion."""
    if suspended() and status in POSITIVE_SCAN:
        return (f"{status} (as signed; suspended under the policy in force — an "
                f"experimental comparison, not a conclusion)")
    if suspended() and status in DEFINITIVE_NEGATIVE_SCAN:
        return f"{status} (as signed; under the policy in force a suspected divergence)"
    return str(status)


def claim_usable(status: str | None) -> tuple[bool, str]:
    """Whether a receipt's or row's stated claim may be relied on now."""
    if suspended():
        if status in POSITIVE_SCAN:
            return False, f"the claim {status} is suspended — {SUSPENSION_REASON}"
        return False, (f"the receipt states {status or 'no status'}, which is not a claim "
                       f"that the definition may be relied on; and {SUSPENSION_REASON}")
    return status in POSITIVE_SCAN, ""


def reliance(status: str | None, *, signature_authentic: bool | None,
             estate_unchanged: bool | None) -> Reliance:
    usable, why = claim_usable(status)
    reasons = []
    if signature_authentic is False:
        reasons.append("the signature is not authentic, or this store did not issue it")
    if estate_unchanged is not True:
        reasons.append("the estate was not shown unchanged against the receipt's scan"
                       if estate_unchanged is None else
                       "the estate has changed since the receipt's scan")
    if not usable:
        reasons.append(why)
    return Reliance(signature_authentic, estate_unchanged, usable, tuple(reasons))
