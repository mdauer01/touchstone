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

    # Merge name buckets that share a canonical core — but ONLY when the
    # bucket speaks with one voice. A bucket holding two different cores (two
    # models both calling their measure `sales` while computing different
    # things) would otherwise act as a bridge, chaining unrelated concepts
    # into one and destroying every verdict for all of them. A heterogeneous
    # bucket keeps its own identity rather than fusing its neighbours.
    core_owner: dict[str, str] = {}
    for name in sorted(buckets):
        bucket = buckets[name]
        hashes = {fp.core.hash() for fp in bucket.fingerprints}
        hashes.update(cl.core.hash() for cl in bucket.claims if cl.core is not None)
        if len(hashes) != 1:
            continue
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
