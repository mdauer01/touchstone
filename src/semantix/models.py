"""Core domain objects: fingerprints, claims, concepts, verdicts, receipts.

The SemanticCore is the unit of comparison everywhere in the system: two
definitions are statically equivalent iff their cores are equal. Names live
*outside* the core so that aliasing (same math, different name) is
detectable, and so a rename does not read as a semantic change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum


class VerdictType(str, Enum):
    CERTIFIED = "CERTIFIED"
    CONTRADICTION = "CONTRADICTION"
    ALIASING = "ALIASING"
    ORPHAN_CLAIM = "ORPHAN_CLAIM"
    SHADOW_METRIC = "SHADOW_METRIC"
    UNRESOLVED = "UNRESOLVED"


# Order decides the primary badge when a concept accrues several verdicts
# (e.g. CERTIFIED + ALIASING).
VERDICT_PRECEDENCE: list[VerdictType] = [
    VerdictType.CONTRADICTION,
    VerdictType.CERTIFIED,
    VerdictType.ALIASING,
    VerdictType.SHADOW_METRIC,
    VerdictType.ORPHAN_CLAIM,
    VerdictType.UNRESOLVED,
]


class CanonLevel(str, Enum):
    OPTIMIZED = "optimized"    # fully qualified, inlined, normalized — proof-grade
    QUALIFIED = "qualified"    # identifiers resolved, no deep rewriting
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
PROOF_LEVELS = {CanonLevel.OPTIMIZED.value, CanonLevel.QUALIFIED.value}


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

    @staticmethod
    def make_id(core: SemanticCore, concept_candidate: str) -> str:
        raw = f"{core.hash()}|{concept_candidate.lower()}"
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
            "signature": self.signature,
        }
