"""Query-stream monitoring: alt-definition detection from executed SQL.

The scan verifies the *governed* estate — models and declarations someone
registered somewhere. This service reads the other estate: the query stream,
where BI tools and analysts compute metrics that are registered nowhere. A
recurring query computing a known concept's math with one filter changed is
a definition in use, and the warehouse's own history is the only place it
is visible.

Doctrine, applied to monitoring:

* SIMILARITY NOMINATES, EQUALITY DECIDES. Exact canonical-core equality with
  a certified definition is the only statement of sameness. A near miss —
  same measure over the same physical columns, different slice — is reported
  as a CANDIDATE with the exact component diff, never as a contradiction.
  Monitoring accuses a named person's dashboard; a finding that overstates
  its own confidence here is the product failing in its one job.
* RECURRENCE GATES NOISE. Exploration is not definition. A core seen fewer
  than `min_runs` times is counted, never named.
* BLIND SPOTS DISCLOSE THEMSELVES. Statements that do not parse, queries
  whose canonicalization is below proof grade, and reference definitions
  excluded for the same reason are all counted in the result. A query we
  could not read at proof grade produces no accusation — an INCOMPLETE core
  omits exactly the constructs that could make two different queries look
  alike.
* READ-ONLY (v1). Monitor findings are not yet recorded in the store; the
  output says so. Persistence lands with its own review, because writing
  observations shares tables with receipts.

The observed side must resolve through the SAME schema registry as the scan
(models registered, lineage resolved to raw terminals) or its cores could
never equal the reference cores — which is why callers hand this service the
estate sources, not just the history text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from sqlglot import exp

from semantix.extraction.canonicalize import canonicalize_sql
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.extraction.sqlutil import table_fqname
from semantix.models import (
    PROOF_LEVELS,
    CanonLevel,
    Provenance,
    SemanticCore,
    core_components,
    core_diff,
)
from semantix.services.relationships import _parse_statements  # same package: one
# statement-salvage behavior for every history consumer, not two drifting ones.
from semantix.services.resolution import normalize_name
from semantix.store import Store

@dataclass
class _RefDef:
    """One proof-grade definition from the latest scan's reference set."""

    concept_label: str
    kind: str  # "computed" | "declared"
    name: str
    where: str | None
    semantic_hash: str
    components: dict[str, object]


@dataclass
class _Observed:
    """One distinct canonical core seen in the query stream."""

    semantic_hash: str
    components: dict[str, object] = field(default_factory=dict)
    uses: int = 0
    names: set[str] = field(default_factory=set)
    relations: set[str] = field(default_factory=set)
    statements: set[str] = field(default_factory=set)
    proof_grade: bool = True
    gaps: set[str] = field(default_factory=set)
    sample_sql: str | None = None


def build_registry(catalog: dict, artifacts: list, dialect: str) -> SchemaRegistry:
    """The scan's registry, rebuilt without store writes: catalog plus every
    computed model registered upstream-first, so observed queries resolve
    columns through models to the same raw terminals reference cores carry."""
    registry = SchemaRegistry(catalog)
    for artifact in artifacts:  # callers pass topo-sorted adapter output
        canon = canonicalize_sql(
            artifact.sql, registry.sqlglot_schema(dialect), dialect)
        registry.register_model(artifact.relation, canon.ast)
    return registry


def monitor_history(
    store: Store,
    history_text: str,
    registry: SchemaRegistry,
    dialect: str,
    min_runs: int = 2,
) -> dict:
    """Findings from executed SQL, matched against the latest scan.

    Each executed statement counts as one use — history logs carry one row
    per execution, and a byte-identical rerun is a rerun, not a duplicate.
    """
    scan = store.latest_scan()
    if scan is None:
        raise ValueError("no scans recorded — monitor needs a scan's "
                         "reference set to compare against; run `semantix "
                         "scan` first")
    refs, ref_meta = _reference(store, scan["scan_id"])
    by_hash: dict[str, list[_RefDef]] = {}
    by_name: dict[str, set[str]] = {}
    by_concept: dict[str, list[_RefDef]] = {}
    for ref in refs:
        by_hash.setdefault(ref.semantic_hash, []).append(ref)
        by_name.setdefault(normalize_name(ref.name), set()).add(ref.concept_label)
        by_name.setdefault(normalize_name(ref.concept_label), set()).add(ref.concept_label)
        by_concept.setdefault(ref.concept_label, []).append(ref)

    statements, unparseable = _parse_statements(history_text, dialect)
    observed: dict[str, _Observed] = {}
    non_query = 0
    unreadable = 0
    no_measure = 0
    for stmt in statements:
        if not isinstance(stmt, exp.Query):
            non_query += 1
            continue
        sql_text = stmt.sql(dialect=dialect)
        stmt_id = hashlib.sha256(sql_text.encode()).hexdigest()[:12]
        canon = canonicalize_sql(sql_text, registry.sqlglot_schema(dialect), dialect)
        if canon.level == CanonLevel.FAILED:
            unreadable += 1
            continue
        reads = sorted({table_fqname(t)
                        for t in (canon.ast or stmt).find_all(exp.Table)})
        relation = reads[0] if reads else "(ad-hoc)"
        provenance = Provenance(system="query_history", object_id=f"stmt_{stmt_id}")
        fps = extract_fingerprints(canon, relation, provenance, dialect, registry)
        if not fps:
            no_measure += 1
            continue
        # One statement contributes one use per DISTINCT core it computes —
        # a query selecting the same measure twice is one computation.
        for core_hash in {fp.core.hash() for fp in fps}:
            fp = next(f for f in fps if f.core.hash() == core_hash)
            obs = observed.setdefault(core_hash, _Observed(semantic_hash=core_hash))
            obs.uses += 1
            obs.names.add(fp.concept_candidate)
            obs.relations.update(reads)
            obs.statements.add(stmt_id)
            obs.components = core_components(fp.core)
            if fp.canon_level not in PROOF_LEVELS or fp.gaps:
                obs.proof_grade = False
                obs.gaps.update(fp.gaps)
            if obs.sample_sql is None:
                obs.sample_sql = fp.canonical_sql
    recurring = {h: o for h, o in observed.items() if o.uses >= min_runs}
    singles = len(observed) - len(recurring)

    findings = [_match(obs, by_hash, by_name, by_concept)
                for obs in sorted(recurring.values(),
                                  key=lambda o: (-o.uses, o.semantic_hash))]
    findings.sort(key=lambda f: (-f["severity"], -f["uses"]))
    return {
        "scan_id": scan["scan_id"],
        "reference": ref_meta,
        "statements": len(statements),
        "skipped_statements": unparseable,
        "non_query_statements": non_query,
        "unreadable_statements": unreadable,
        "statements_without_measures": no_measure,
        "observed_definitions": len(observed),
        "recurring_definitions": len(recurring),
        "below_min_runs": singles,
        "min_runs": min_runs,
        "findings": findings,
        "recorded": False,  # v1: read-only — nothing written to the store
    }


def _reference(store: Store, scan_id: str) -> tuple[list[_RefDef], dict]:
    refs: list[_RefDef] = []
    excluded = 0
    concepts = store.concepts_for_scan(scan_id)
    for concept in concepts:
        members = concept["members"]
        for fp_id in members.get("fingerprints") or []:
            row = store.fingerprint_row(fp_id)
            if row is None:
                continue
            if row["canon_level"] not in PROOF_LEVELS:
                excluded += 1
                continue
            refs.append(_RefDef(
                concept_label=concept["label"], kind="computed",
                name=row["concept_candidate"], where=row["relation"],
                semantic_hash=row["semantic_hash"],
                components=core_components(SemanticCore.from_row(row))))
        for claim_id in members.get("claims") or []:
            row = store.claim_row(claim_id)
            if row is None:
                continue
            if not row["expressible"] or row["canon_level"] not in PROOF_LEVELS \
                    or not row["semantic_hash"]:
                excluded += 1
                continue
            refs.append(_RefDef(
                concept_label=concept["label"], kind="declared",
                name=row["name"], where=row["model"],
                semantic_hash=row["semantic_hash"],
                components=core_components(SemanticCore.from_row(row))))
    return refs, {"concepts": len(concepts), "definitions": len(refs),
                  "excluded_below_proof": excluded}


def _diff(reference: dict, observed: dict) -> dict:
    return core_diff(reference, observed, "reference", "observed")


def _closest(refs: list[_RefDef], observed: _Observed) -> tuple[_RefDef, dict]:
    """The reference definition the observed core differs least from —
    fewest differing components, deterministic tiebreak."""
    scored = sorted(
        ((len(_diff(r.components, observed.components)), r.semantic_hash, r)
         for r in refs),
        key=lambda t: (t[0], t[1]))
    ref = scored[0][2]
    return ref, _diff(ref.components, observed.components)


def _match(observed: _Observed,
           by_hash: dict[str, list[_RefDef]],
           by_name: dict[str, set[str]],
           by_concept: dict[str, list[_RefDef]]) -> dict:
    base = {
        "uses": observed.uses,
        "distinct_statements": len(observed.statements),
        "names_used": sorted(observed.names),
        "relations_read": sorted(observed.relations),
        "semantic_hash": observed.semantic_hash[:12],
        "sample_sql": observed.sample_sql,
    }

    exact = by_hash.get(observed.semantic_hash)
    if exact and observed.proof_grade:
        ref = exact[0]
        aliased = sorted(n for n in observed.names
                         if normalize_name(n) != normalize_name(ref.concept_label))
        return {**base, "kind": "consistent_use", "severity": 0,
                "concept": ref.concept_label,
                "summary": f"matches the {ref.kind} definition of "
                           f"{ref.concept_label} at {ref.where} exactly"
                           + (f" (used under alias {', '.join(aliased)})"
                              if aliased else "")}

    if not observed.proof_grade:
        # An INCOMPLETE core omits constructs — equality or difference over it
        # proves nothing, so it must never anchor an accusation OR a
        # confirmation. Counted, described, left alone.
        reason = ("; ".join(sorted(observed.gaps))
                  or "canonicalization below proof grade")
        return {**base, "kind": "unverifiable", "severity": 0,
                "concept": None,
                "summary": f"recurring measure could not be read at proof "
                           f"grade ({reason}) — no comparison made"}

    named = {label
             for alias in observed.names
             for label in by_name.get(normalize_name(alias), ())}
    if named:
        label = sorted(named)[0]
        ref, diff = _closest(by_concept[label], observed)
        return {**base, "kind": "named_alt_definition",
                "severity": min(95, 60 + min(30, observed.uses)),
                "concept": label, "diff": diff,
                "summary": f"computed under the name {label} but differs from "
                           f"the {ref.kind} definition at {ref.where} in: "
                           + ", ".join(diff) if diff else
                           f"named {label} with a different canonical core"}

    # Near core: same math over the same physical columns, different slice.
    # This is a HYPOTHESIS by construction — the analyst never used the name.
    near = [r for refs in by_concept.values() for r in refs
            if r.components["expression"] == observed.components["expression"]
            and r.components["physical lineage"] == observed.components["physical lineage"]]
    if near:
        ref, diff = _closest(near, observed)
        return {**base, "kind": "candidate_alt_definition",
                "severity": min(85, 40 + min(30, observed.uses)),
                "concept": ref.concept_label, "diff": diff,
                "summary": f"same measure as {ref.concept_label} over the "
                           f"same columns, differing in: "
                           + ", ".join(diff) if diff else
                           f"near {ref.concept_label}"}

    return {**base, "kind": "unregistered_recurring_measure", "severity": 25,
            "concept": None,
            "summary": "recurring measure not present in the scanned estate "
                       "or its declarations"}
