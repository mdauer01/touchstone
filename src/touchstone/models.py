"""Core domain objects: fingerprints, claims, concepts, verdicts, receipts.

The SemanticCore is the unit of comparison everywhere in the system: two
definitions are statically equivalent iff their cores are equal. Names live
*outside* the core so that aliasing (same math, different name) is
detectable, and so a rename does not read as a semantic change.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum


class VerdictType(str, Enum):
    # CERTIFIED / ALIASING / CONTRADICTION are what the ENGINE computes. While
    # positive verdicts are suspended (touchstone.conclusions) users see
    # UNRESOLVED (with the experimental result in evidence) and
    # SUSPECTED_DIVERGENCE instead; the old values still parse stored rows.
    CERTIFIED = "CERTIFIED"
    CONTRADICTION = "CONTRADICTION"
    SUSPECTED_DIVERGENCE = "SUSPECTED_DIVERGENCE"
    ALIASING = "ALIASING"
    ORPHAN_CLAIM = "ORPHAN_CLAIM"
    SHADOW_METRIC = "SHADOW_METRIC"
    UNRESOLVED = "UNRESOLVED"


# Order decides the primary badge when a concept accrues several verdicts
# (e.g. CERTIFIED + ALIASING).
VERDICT_PRECEDENCE: list[VerdictType] = [
    VerdictType.SUSPECTED_DIVERGENCE,
    VerdictType.CONTRADICTION,
    VerdictType.CERTIFIED,
    VerdictType.ALIASING,
    VerdictType.SHADOW_METRIC,
    VerdictType.ORPHAN_CLAIM,
    VerdictType.UNRESOLVED,
]


class CanonLevel(str, Enum):
    # Inside the supported grammar of the proof policy (extraction.proof):
    # the ONLY proof grade. Reached through stated transformations alone.
    SUPPORTED = "supported"
    # The general optimizer's output. A HYPOTHESIS: an optimizer completing
    # does not establish that its rewrite preserved the query's meaning.
    OPTIMIZED = "optimized"
    QUALIFIED = "qualified"    # identifiers resolved by sqlglot, no proof
    INCOMPLETE = "incomplete"  # canonicalized fine, but the SemanticCore does
                               # not model some construct this query uses
    PARSED = "parsed"          # AST only; canonical equality NOT trustworthy
    FAILED = "failed"


# Canonical equality at these levels counts as proof of sameness (§3 of the
# spec: equality proves sameness, difference is only a hypothesis).
#
# INCOMPLETE is deliberately excluded. The core is a WHITELIST of modeled
# constructs, so anything it does not read defaults to "identical" — which
# turns a blind spot into a false certification, the one failure a verifier
# cannot survive. An adversarial review on 2026-07-25 found six of these
# (CTE bodies, join type, HAVING, LIMIT, grouping sets, SELECT DISTINCT).
# The rule is therefore inverted: a construct we do not model must PREVENT
# proof grade, never be silently ignored.
#
# Since policy touchstone-proof/2 only SUPPORTED is proof grade: the general
# optimizer rewrote predicates below an ordered LIMIT, merged outer joins'
# null-supplying sides, unnested NOT IN, and simplified booleans in ways
# that are wrong under NULLs — each reproduced as a false certification.
PROOF_LEVELS = {CanonLevel.SUPPORTED.value}


def _short(h: str, n: int = 12) -> str:
    return h[:n]


@dataclass(frozen=True)
class SemanticCore:
    """The canonical, name-free content of one measure definition."""

    measure_expr: str
    filters: tuple[str, ...] = ()
    grain: tuple[str, ...] = ()
    join_nodes: tuple[str, ...] = ()
    join_edges: tuple[str, ...] = ()
    source_columns: tuple[str, ...] = ()

    def hash(self) -> str:
        payload = json.dumps(
            {
                "measure_expr": self.measure_expr,
                "filters": list(self.filters),
                "grain": list(self.grain),
                "join_nodes": list(self.join_nodes),
                "join_edges": list(self.join_edges),
                "source_columns": list(self.source_columns),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def components(self) -> dict[str, object]:
        """Component view used for evidence diffs in verdicts."""
        return {
            "expression": self.measure_expr,
            "filters": list(self.filters),
            "grain": list(self.grain),
            "join graph": list(self.join_nodes) + list(self.join_edges),
            "physical lineage": list(self.source_columns),
        }

    @classmethod
    def from_row(cls, row: dict) -> "SemanticCore":
        """Rehydrate from a fingerprints/claims store row (the *_json columns)."""
        def js(key: str) -> tuple:
            return tuple(json.loads(row.get(key) or "[]"))
        return cls(
            measure_expr=row.get("measure_expr") or "",
            filters=js("filters_json"), grain=js("grain_json"),
            join_nodes=js("join_nodes_json"), join_edges=js("join_edges_json"),
            source_columns=js("source_columns_json"))


COMPONENT_ORDER = ("expression", "filters", "grain", "join graph", "physical lineage")


def core_components(core: SemanticCore) -> dict[str, object]:
    """components() with list members sorted — the order-insensitive view
    the monitor and proposal diffs compare on."""
    return {k: (sorted(v) if isinstance(v, list) else v)
            for k, v in core.components().items()}


# Positional aliases (`t0.`) and relation qualifier chains
# (`analytics.fct_orders.`) in canonical text. Anchored on a leading letter
# so `1.5` survives.
QUALIFIER_RE = re.compile(r"\b(?:[A-Za-z_][\w$]*\.)+(?=[\w\"$])")


def strip_qualifiers(text) -> object:
    if isinstance(text, str):
        return QUALIFIER_RE.sub("", text)
    if isinstance(text, list):
        return sorted(strip_qualifiers(t) for t in text)
    return text


def core_diff(left: dict, right: dict, left_label: str, right_label: str) -> dict:
    """Per-component differences between two core_components() views,
    labelled for whoever reads them (reference/observed, declared/computed).

    When the JOIN GRAPHS differ, a component whose only difference is how
    its columns are qualified is not listed: single-source cores render
    unqualified and multi-source cores by relation, so a mart and a joined
    query computing the same math would otherwise be blamed for a
    different "expression". When the join graphs are equal, a qualifier
    difference names a different table's column and stays."""
    diff = {k: {left_label: left[k], right_label: right[k]}
            for k in COMPONENT_ORDER if left[k] != right[k]}
    if "join graph" in diff:
        for k in ("expression", "filters", "grain"):
            if k in diff and strip_qualifiers(left[k]) == strip_qualifiers(right[k]):
                del diff[k]
    return diff


@dataclass(frozen=True)
class Provenance:
    system: str
    object_id: str
    path: str | None = None
    author: str | None = None

    def as_dict(self) -> dict:
        return {
            "system": self.system,
            "object_id": self.object_id,
            "path": self.path,
            "author": self.author,
        }


@dataclass
class Fingerprint:
    """One measure expression as the code actually computes it."""

    fingerprint_id: str
    concept_candidate: str
    core: SemanticCore
    relation: str
    dialect: str
    canon_level: str
    canonical_sql: str
    provenance: Provenance
    # Constructs this query uses that the SemanticCore cannot represent
    # (extraction.completeness). Non-empty forces canon_level=INCOMPLETE, so
    # the fingerprint can be reported and diffed but never certified equal.
    gaps: tuple[str, ...] = ()
    # The proof policy's rules (extraction.proof.RULES) that produced this
    # form; empty below proof grade. Recorded on positive verdicts.
    rules: tuple[str, ...] = ()
    # The parser-disagreement detector's status for this form's ORIGINAL SQL
    # ("no_disagreement_found" / "not_checked"; a disagreement is refused).
    parse_check: dict | None = None

    @staticmethod
    def make_id(core: SemanticCore, concept_candidate: str, relation: str = "",
                canon_level: str = "", gaps: tuple[str, ...] = ()) -> str:
        """Identity of a definition SITE, not just of a definition.

        Keyed on core+name alone, two models computing the same thing shared
        one content-addressed store row (record_fingerprint is INSERT OR
        IGNORE), so the first site recorded owned the relation forever: the
        verdicts projection never named the second model, a renamed model
        stayed attributed to its deleted relation, and a core that later
        picked up completeness gaps kept the stale proof-grade level — the
        monitor then certified against a definition that was no longer proof
        grade. Relation, level and gaps are part of what the row asserts, so
        they are part of what identifies it.
        """
        from touchstone.extraction.proof import identity_gap

        raw = "|".join((core.hash(), concept_candidate.lower(), relation,
                        canon_level, ",".join(identity_gap(g) for g in gaps)))
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def short_id(self) -> str:
        return _short(self.fingerprint_id)

    @property
    def object_key(self) -> str:
        """Definition site: where in the estate this measure lives."""
        return f"{self.provenance.system}:{self.provenance.object_id}:{self.concept_candidate.lower()}"


@dataclass
class Claim:
    """A declared definition, given the same canonical treatment where expressible."""

    claim_id: str
    name: str
    provenance: Provenance
    description: str | None = None
    owner: str | None = None
    model: str | None = None
    expr_raw: str | None = None
    filters_raw: tuple[str, ...] = ()
    group_by_raw: tuple[str, ...] = ()
    expressible: bool = False
    core: SemanticCore | None = None
    canon_level: str = CanonLevel.FAILED.value
    canonical_sql: str | None = None
    error: str | None = None
    # Constructs the declaration uses that the SemanticCore cannot represent.
    # Mirrors Fingerprint.gaps: a claim carrying gaps is below proof grade and
    # can be reported and diffed but never certified equal to anything.
    gaps: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()   # as Fingerprint.rules
    parse_check: dict | None = None   # as Fingerprint.parse_check

    @staticmethod
    def make_id(name: str, provenance: Provenance, expr_raw: str | None) -> str:
        raw = f"{provenance.system}|{name}|{expr_raw or ''}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def short_id(self) -> str:
        return _short(self.claim_id)

    @property
    def object_key(self) -> str:
        return f"{self.provenance.system}:{self.name.lower()}"


@dataclass
class Concept:
    """A business concept: the cluster of definitions that claim the same meaning."""

    concept_id: str
    label: str
    fingerprints: list[Fingerprint] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)

    @property
    def declared(self) -> bool:
        return bool(self.claims)


@dataclass
class Verdict:
    verdict_type: VerdictType
    concept_id: str
    concept_label: str
    severity: int
    summary: str
    evidence: dict


@dataclass
class Receipt:
    """Semantic receipt (§6): schema frozen now so history is attestation-ready.

    definition_version / lineage / known_divergences reference immutable
    fingerprint content, so any receipt ever issued can be replayed.
    """

    receipt_id: str
    concept: str
    status: str
    definition_version: list[dict]
    lineage: list[str]
    known_divergences: list[str]
    as_of: str | None
    issued_at: str
    scan_id: str | None
    # EVERY verdict for this concept, not just the primary. A concept can be
    # CERTIFIED and ALIASING at once; reporting only the reassuring one hides
    # a real finding from whoever trusts the receipt.
    verdicts: list[dict] = field(default_factory=list)
    # Scope limitation, in the auditor's sense: what the issuing scan could
    # not read. A clean opinion over an incomplete estate is not a clean
    # opinion, so this travels with every receipt and inside the signature.
    scope: dict | None = None
    # When the verdicts behind this receipt were actually established (the
    # scan's start time). status is a statement about the estate at THIS
    # moment, not at issued_at — verification decays honestly.
    verified_as_of: str | None = None
    # Freshness block from services.freshness: did the estate still match the
    # scanned snapshot when this receipt was issued? Embedded before signing,
    # so "fresh as of T" is itself tamper-evident.
    freshness: dict | None = None
    # The proof policy (extraction.proof.POLICY_ID) the scan's verdicts were
    # computed under; None for a scan that predates policy versioning. A
    # receipt is current only under the policy in force.
    proof_policy: str | None = None
    # Ed25519 signature block over the canonical payload (signing.py); None
    # until signed, {"alg": None, ...} when no crypto backend exists.
    signature: dict | None = None

    def as_dict(self) -> dict:
        return {
            "receipt_id": self.receipt_id,
            "concept": self.concept,
            "status": self.status,
            "definition_version": self.definition_version,
            "lineage": self.lineage,
            "known_divergences": self.known_divergences,
            "as_of": self.as_of,
            "issued_at": self.issued_at,
            "scan_id": self.scan_id,
            "verdicts": self.verdicts,
            "scope": self.scope,
            "verified_as_of": self.verified_as_of,
            "freshness": self.freshness,
            "proof_policy": self.proof_policy,
            "signature": self.signature,
        }
