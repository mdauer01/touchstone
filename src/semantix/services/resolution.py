"""Concept resolution (§5), Phase 0 signals:

1. name clustering — normalized-name equality buckets fingerprints & claims;
2. semantic-hash merge — buckets that share an identical canonical core are
   the same concept regardless of name (this is what makes aliasing visible
   to reconciliation at all).

Lineage-overlap and embedding similarity are Phase 1 signals; human merge
overrides are a Phase 1 table. Both slot in here without structural change.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from semantix.models import Claim, Concept, Fingerprint


def normalize_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


@dataclass
class _Bucket:
    fingerprints: list[Fingerprint] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)


def resolve_concepts(fingerprints: list[Fingerprint], claims: list[Claim]) -> list[Concept]:
    buckets: dict[str, _Bucket] = {}
    for fp in fingerprints:
        buckets.setdefault(normalize_name(fp.concept_candidate), _Bucket()).fingerprints.append(fp)
    for claim in claims:
        buckets.setdefault(normalize_name(claim.name), _Bucket()).claims.append(claim)

    parent = {name: name for name in buckets}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            # keep the lexicographically smaller root for determinism
            lo, hi = sorted((ra, rb))
            parent[hi] = lo

    # Merge name buckets that share a canonical core, at MEMBER granularity.
    #
    # Two failure modes bound this. Unioning whole buckets let a bucket
    # holding two different cores act as a bridge, chaining unrelated concepts
    # together. But skipping such buckets entirely was equally wrong: a name
    # that legitimately aliases another concept stopped merging the moment it
    # picked up a second definition, splitting a certified concept into a
    # false ORPHAN_CLAIM plus SHADOW_METRIC.
    #
    # So a bucket participates through each core it contains, and only buckets
    # that genuinely share a core are joined — a heterogeneous bucket links to
    # each of its cores' owners without those owners linking to each other,
    # because union is applied per core rather than per bucket pair.
    # Two definitions belong to one concept when they share a NAME (same name,
    # different math — the contradiction we exist to find) or a CANONICAL CORE
    # (same math, different names — aliasing). Both relations are required, so
    # concept identity is their transitive closure, and a name that means two
    # things genuinely does bridge the concepts it spans.
    #
    # Two attempts to break that transitivity both failed in review: unioning
    # whole buckets let a bridge chain unrelated concepts silently, and then
    # excluding bridges split legitimate aliases into a false ORPHAN_CLAIM
    # plus SHADOW_METRIC. The closure is kept, and the ambiguity is reported
    # as a verdict instead of being resolved by a heuristic — see
    # reconcile's ambiguous-name handling.
    core_owner: dict[str, str] = {}
    for name in sorted(buckets):
        bucket = buckets[name]
        hashes = sorted({fp.core.hash() for fp in bucket.fingerprints}
                        | {cl.core.hash() for cl in bucket.claims if cl.core is not None})
        for h in hashes:
            if h in core_owner:
                union(name, core_owner[h])
            else:
                core_owner[h] = name

    grouped: dict[str, _Bucket] = {}
    for name, bucket in buckets.items():
        root = find(name)
        merged = grouped.setdefault(root, _Bucket())
        merged.fingerprints.extend(bucket.fingerprints)
        merged.claims.extend(bucket.claims)

    concepts = []
    for bucket in grouped.values():
        label = _pick_label(bucket)
        concepts.append(
            Concept(
                concept_id="c_" + hashlib.sha256(label.encode()).hexdigest()[:10],
                label=label,
                fingerprints=sorted(bucket.fingerprints, key=lambda f: f.object_key),
                claims=sorted(bucket.claims, key=lambda c: c.name),
            )
        )
    return sorted(concepts, key=lambda c: c.label)


def _pick_label(bucket: _Bucket) -> str:
    """Declared names win; otherwise the most common computed candidate.

    Ties break lexicographically rather than by input order: an order-
    dependent label yields a different concept_id on a rescan of an unchanged
    estate, which splits the concept's bitemporal history in two.
    """
    if bucket.claims:
        return min(normalize_name(c.name) for c in bucket.claims)
    counts: dict[str, int] = {}
    for fp in bucket.fingerprints:
        n = normalize_name(fp.concept_candidate)
        counts[n] = counts.get(n, 0) + 1
    return min(sorted(counts), key=lambda n: (-counts[n], n))
