"""Adapter output types (§2): everything an adapter ingests is normalized to
a ComputedArtifact (executable definition logic) or a DeclaredSpec (a claimed
definition). Adapters stay thin — parse, attach provenance, hand off."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class ComputedArtifact:
    system: str
    object_id: str
    relation: str
    sql: str
    path: str | None = None
    author: str | None = None
    depends_on: list[str] = field(default_factory=list)  # upstream relations


@dataclass
class DeclaredSpec:
    system: str
    name: str
    description: str | None = None
    owner: str | None = None
    model: str | None = None
    expr: str | None = None
    filters: list[str] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    path: str | None = None


def spec_content_text(spec: DeclaredSpec) -> str:
    """Stable content identity of a declared definition: every field that
    changes its meaning. A metric whose filters change while its expr stays
    put HAS changed. Used as record_artifact's raw_text at scan time and
    mirrored by the freshness re-check — change it only in both directions
    at once (it feeds content-addressed artifact ids)."""
    return json.dumps(
        {
            "model": spec.model,
            "expr": spec.expr,
            "filters": list(spec.filters),
            "group_by": list(spec.group_by),
        },
        sort_keys=True,
    )


def artifact_key(artifact: ComputedArtifact) -> tuple[str, str]:
    """Identity of an artifact. NOT its relation: two artifacts can legitimately
    target the same relation (a dbt model and a hand-written view, two sources
    scanned together), and that collision is itself a finding."""
    return (artifact.system, artifact.object_id)


def duplicate_relations(artifacts: list[ComputedArtifact]) -> dict[str, list[str]]:
    """relation -> the object ids of every artifact claiming to produce it."""
    producers: dict[str, list[str]] = {}
    for a in artifacts:
        producers.setdefault(a.relation, []).append(f"{a.system}:{a.object_id}")
    return {rel: ids for rel, ids in sorted(producers.items()) if len(ids) > 1}


def topo_sort(artifacts: list[ComputedArtifact]) -> list[ComputedArtifact]:
    """Kahn's algorithm over model-to-model edges, alphabetical tiebreak so
    scans are deterministic. Cycles degrade to alphabetical order. Shared by
    every adapter so combined-source scans register models upstream-first.

    Keyed by artifact identity, never by relation. Keying on relation silently
    DELETED every artifact but one whenever two produced the same relation —
    and deleting an artifact deletes a definition, so a genuine CONTRADICTION
    would come back CERTIFIED for want of its contradictor. This function is
    total: every input artifact appears exactly once in the output.
    """
    by_key: dict[tuple[str, str], ComputedArtifact] = {}
    for a in artifacts:
        by_key.setdefault(artifact_key(a), a)

    producers: dict[str, list[tuple[str, str]]] = {}
    for key, a in by_key.items():
        producers.setdefault(a.relation, []).append(key)

    indegree = {key: 0 for key in by_key}
    dependents: dict[tuple[str, str], list[tuple[str, str]]] = {key: [] for key in by_key}
    for key, a in by_key.items():
        for dep in a.depends_on:
            for producer in producers.get(dep, []):
                if producer != key:  # self-reference is not a dependency
                    indegree[key] += 1
                    dependents[producer].append(key)

    ready = sorted(key for key, deg in indegree.items() if deg == 0)
    ordered: list[ComputedArtifact] = []
    while ready:
        key = ready.pop(0)
        ordered.append(by_key[key])
        for nxt in sorted(dependents[key]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort()

    if len(ordered) < len(by_key):  # cycle fallback
        seen = {artifact_key(a) for a in ordered}
        ordered.extend(a for _, a in sorted(by_key.items())
                       if artifact_key(a) not in seen)

    assert len(ordered) == len(by_key), "topo_sort must not drop artifacts"
    return ordered
