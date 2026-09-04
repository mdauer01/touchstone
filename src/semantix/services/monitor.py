"""Query-stream monitoring: alt-definition detection from executed SQL.

The scan verifies the *governed* estate — models and declarations someone
registered somewhere. This service reads the other estate: the query stream,
where BI tools and analysts compute metrics that are registered nowhere. A
recurring query computing a known concept's math with one filter changed is
a definition in use, and the warehouse's own history is the only place it
is visible.

Doctrine, applied to monitoring:

* SIMILARITY NOMINATES, EQUALITY DECIDES. Exact canonical-core equality with
  a reference definition is the only statement of sameness. A near miss —
  same measure over the same physical columns, different slice — is reported
  as a CANDIDATE with the exact component diff, never as a contradiction.
  Monitoring accuses a named person's dashboard; a finding that overstates
  its own confidence here is the product failing in its one job.
* EQUALITY IS NOT ENDORSEMENT. Matching a definition the scan found
  CONTRADICTED is agreeing with one side of an open dispute, and is reported
  as such (contested_use), never as consistency. Matching concept A's math
  under concept B's NAME is the named-alt case for B, not a sev-0 match.
* RECURRENCE GATES NOISE. Exploration is not definition. A core seen fewer
  than `min_runs` times is counted, never named.
* BLIND SPOTS DISCLOSE THEMSELVES. Statements that do not parse, queries
  whose canonicalization is below proof grade, measures computed in nested
  scopes the extractor does not read, reads of a model's aggregate output
  (a cross-level comparison the core cannot make), bind parameters, and
  reference definitions excluded for being below proof grade are all
  counted or named in the result. None of them anchors an accusation.
* READ-ONLY (v1). Monitor findings are not yet recorded in the store; the
  output says so. Persistence lands with its own review, because writing
  observations shares tables with receipts.

The observed side must resolve through the SAME schema registry as the scan
(models registered upstream-first, lineage resolved to raw terminals) or its
cores could never equal the reference cores — which is why callers hand
this service the estate sources, not just the history text.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from sqlglot import exp

from semantix.adapters.base import topo_sort
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
# statement-splitting behavior for every history consumer, not two drifting ones.
from semantix.services.resolution import normalize_name
from semantix.store import Store

# Aliases the analyst did not write: sqlglot's positional names and the
# extractor's synthesized `<agg>_<column>` names. Attributing them to a
# person, or matching them against a concept that happens to share the
# spelling (`col_0`), produced accusations about names nobody used.
_SYNTHESIZED = re.compile(
    r"^(_col_\d+|(sum|count|avg|min|max|first|last|mode|anyvalue|arrayagg|"
    r"stringagg|listagg|groupconcat|approxdistinct|median|quantile|countif|"
    r"argmax|argmin|boolor|booland)(_[a-z0-9_]+)?)$")

SEV_NAMED_ALT_BASE = 60      # same name, different math: 60 + min(30, uses)
SEV_GRAIN_ONLY_BASE = 40     # same name, same math, only the grain differs
SEV_CANDIDATE_BASE = 40      # unnamed near miss: a hypothesis
SEV_CONTESTED = 55           # exact match to one side of an open contradiction
SEV_UNREGISTERED = 25


@dataclass
class _RefDef:
    """One proof-grade definition from the latest scan's reference set."""

    concept_label: str
    kind: str  # "computed" | "declared"
    system: str
    name: str
    where: str | None
    semantic_hash: str
    components: dict[str, object]
    contested: bool  # the concept carries a CONTRADICTION verdict

    @property
    def label(self) -> str:
        if self.kind == "declared":
            return f"declared definition {self.system}:{self.name}"
        return f"computed definition at {self.where}"


@dataclass
class _Observed:
    """One distinct canonical core seen in the query stream, at one grade."""

    semantic_hash: str
    proof_grade: bool
    components: dict[str, object] = field(default_factory=dict)
    uses: int = 0
    names: set[str] = field(default_factory=set)
    relations: set[str] = field(default_factory=set)
    statements: set[str] = field(default_factory=set)
    gaps: set[str] = field(default_factory=set)
    sample_sql: str | None = None


def build_registry(catalog: dict, artifacts: list, dialect: str) -> SchemaRegistry:
    """The scan's registry, rebuilt without store writes: catalog plus every
    computed model registered upstream-first, so observed queries resolve
    columns through models to the same raw terminals reference cores carry.
    Sorted here — a caller passing multi-source artifacts unsorted produced a
    registry whose terminals differed from the scan's."""
    registry = SchemaRegistry(catalog)
    for artifact in topo_sort(list(artifacts)):
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
    refs, excluded, ref_meta = _reference(store, scan["scan_id"])
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
    non_query = unreadable = no_measure = nested_measure = 0
    for stmt in statements:
        if not isinstance(stmt, exp.Query):
            non_query += 1
            continue
        sql_text = stmt.sql(dialect=dialect)
        stmt_id = hashlib.sha256(sql_text.encode()).hexdigest()[:12]
        canon = canonicalize_sql(sql_text, registry.sqlglot_schema(dialect), dialect)
        if canon.level == CanonLevel.FAILED or canon.ast is None:
            unreadable += 1
            continue
        ast = canon.ast
        cte_names = {c.alias_or_name.lower() for c in ast.find_all(exp.CTE)}
        reads = sorted({
            registry.canonical_relation(table_fqname(t))
            for t in ast.find_all(exp.Table)
            if t.name.lower() not in cte_names and not t.name.startswith("_u_")})
        relation = reads[0] if reads else "(ad-hoc)"
        provenance = Provenance(system="query_history", object_id=f"stmt_{stmt_id}")
        fps = extract_fingerprints(canon, relation, provenance, dialect, registry)
        if not fps:
            # A measure computed in a CTE or derived table the outer scope
            # merely projects is a measure the extractor does not READ — a
            # blind spot, not "no measure"; the two were counted as one.
            if ast.find(exp.AggFunc) is not None:
                nested_measure += 1
            else:
                no_measure += 1
            continue
        # Reading a model's AGGREGATE output is a cross-level comparison the
        # static core cannot make (build_claim refuses the same shape for
        # declarations): `SUM(net_revenue) FROM rev_mart` inherits the
        # mart's filter, it does not drop it. Such a query is unverifiable
        # here, never a named alt.
        referenced = {c.name.lower() for c in ast.find_all(exp.Column)}
        reagg = sorted(rel for rel in reads
                       if {c.lower() for c in registry.aggregate_outputs(rel)} & referenced)
        by_core: dict[str, list] = {}
        for fp in fps:
            by_core.setdefault(fp.core.hash(), []).append(fp)
        for core_hash, group in by_core.items():
            fp = group[0]
            gaps = set(fp.gaps)
            if reagg:
                gaps.add(f"reads the aggregate output of {', '.join(reagg)} — "
                         f"through-model comparison not made")
            proof = fp.canon_level in PROOF_LEVELS and not gaps
            # Keyed on grade too: one sampled rerun silently downgraded five
            # clean ones and the stated reason described a single run.
            key = f"{core_hash}|{'proof' if proof else 'gapped'}"
            obs = observed.setdefault(key, _Observed(semantic_hash=core_hash,
                                                     proof_grade=proof))
            obs.uses += 1
            obs.names.update(f.concept_candidate for f in group)
            obs.relations.update(reads)
            obs.statements.add(stmt_id)
            obs.components = core_components(fp.core)
            obs.gaps.update(gaps)
            if obs.sample_sql is None:
                obs.sample_sql = fp.canonical_sql
    recurring = [o for o in observed.values() if o.uses >= min_runs]

    findings: list[dict] = []
    models = registry.known_models()
    for obs in sorted(recurring, key=lambda o: (-o.uses, o.semantic_hash, not o.proof_grade)):
        findings.extend(_match(obs, by_hash, by_name, by_concept, excluded, models))
    findings.sort(key=lambda f: (-f["severity"], -f["uses"], f["kind"]))
    return {
        "scan_id": scan["scan_id"],
        "reference": ref_meta,
        "statements": len(statements) + unparseable,
        "parsed_statements": len(statements),
        "skipped_statements": unparseable,
        "non_query_statements": non_query,
        "unreadable_statements": unreadable,
        "statements_without_measures": no_measure,
        "statements_with_nested_measures": nested_measure,
        "observed_definitions": len(observed),
        "recurring_definitions": len(recurring),
        "below_min_runs": len(observed) - len(recurring),
        "min_runs": min_runs,
        "findings": findings,
        "recorded": False,  # v1: read-only — nothing written to the store
    }


def _reference(store: Store, scan_id: str
               ) -> tuple[list[_RefDef], dict[str, str], dict]:
    """(proof-grade definitions, excluded hash -> description, meta)."""
    contested = {v["concept_label"] for v in store.verdicts_for_scan(scan_id)
                 if v["verdict"] == "CONTRADICTION"}
    refs: list[_RefDef] = []
    excluded: dict[str, str] = {}
    concepts = store.concepts_for_scan(scan_id)
    for concept in concepts:
        label = concept["label"]
        members = concept["members"]
        for fp_id in members.get("fingerprints") or []:
            row = store.fingerprint_row(fp_id)
            if row is None:
                continue
            system = (json.loads(row.get("provenance_json") or "{}")).get("system", "?")
            if row["canon_level"] not in PROOF_LEVELS:
                excluded.setdefault(row["semantic_hash"],
                                    f"computed definition at {row['relation']} "
                                    f"({row['canon_level']})")
                continue
            refs.append(_RefDef(
                concept_label=label, kind="computed", system=system,
                name=row["concept_candidate"], where=row["relation"],
                semantic_hash=row["semantic_hash"],
                components=core_components(SemanticCore.from_row(row)),
                contested=label in contested))
        for claim_id in members.get("claims") or []:
            row = store.claim_row(claim_id)
            if row is None:
                continue
            system = (json.loads(row.get("provenance_json") or "{}")).get("system", "?")
            if not row["expressible"] or row["canon_level"] not in PROOF_LEVELS \
                    or not row["semantic_hash"]:
                if row.get("semantic_hash"):
                    excluded.setdefault(row["semantic_hash"],
                                        f"declared definition {system}:{row['name']} "
                                        f"({row['canon_level']})")
                else:
                    excluded.setdefault(f"claim:{claim_id}", f"{system}:{row['name']}")
                continue
            refs.append(_RefDef(
                concept_label=label, kind="declared", system=system,
                name=row["name"], where=row["model"],
                semantic_hash=row["semantic_hash"],
                components=core_components(SemanticCore.from_row(row)),
                contested=label in contested))
    meta = {"concepts": len(concepts), "definitions": len(refs),
            "excluded_below_proof": len(excluded),
            "contested_concepts": sorted(contested)}
    return refs, excluded, meta


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


def _attributable(names: set[str]) -> list[str]:
    return sorted(n for n in names if not _SYNTHESIZED.match(n.lower()))


def _layered(diff: dict, observed: _Observed, ref: _RefDef, models: set[str]) -> str | None:
    """A join-graph difference where either side reads a registered MODEL is
    a comparison across model layers: a query over the raw table and a mart
    over a filtering view compute the same numbers with different cores,
    because the core does not inline a model's own filters (through-model
    resolution is Phase 1). Such a diff must not be stated as a dropped
    filter. Returns the reason to report it as unverifiable, else None."""
    if "join graph" not in diff:
        return None
    ref_nodes = set(ref.components.get("join graph") or [])
    if (observed.relations | ref_nodes) & models:
        return (f"differs in join graph across model layers (reads "
                f"{', '.join(sorted(observed.relations)) or 'none'}; reference reads "
                f"{', '.join(sorted(ref_nodes)) or 'none'}) — through-model "
                f"comparison not made")
    return None


def _match(observed: _Observed,
           by_hash: dict[str, list[_RefDef]],
           by_name: dict[str, set[str]],
           by_concept: dict[str, list[_RefDef]],
           excluded: dict[str, str],
           models: set[str] = frozenset()) -> list[dict]:
    names = _attributable(observed.names)
    base = {
        "uses": observed.uses,
        "distinct_statements": len(observed.statements),
        "names_used": names,
        "relations_read": sorted(observed.relations),
        "semantic_hash": observed.semantic_hash[:12],
        "sample_sql": observed.sample_sql,
    }
    named = {label for alias in names for label in by_name.get(normalize_name(alias), ())}

    if not observed.proof_grade:
        # An INCOMPLETE core omits constructs — equality or difference over it
        # proves nothing, so it anchors neither an accusation nor a
        # confirmation. Counted, described, left alone.
        reason = "; ".join(sorted(observed.gaps)) or "canonicalization below proof grade"
        return [{**base, "kind": "unverifiable", "severity": 0, "concept": None,
                 "summary": f"recurring measure could not be read at proof "
                            f"grade ({reason}) — no comparison made"}]

    exact = by_hash.get(observed.semantic_hash)
    if exact:
        ref = exact[0]
        out = []
        if ref.contested:
            out.append({**base, "kind": "contested_use", "severity": SEV_CONTESTED,
                        "concept": ref.concept_label,
                        "summary": f"matches the {ref.label} exactly — a definition "
                                   f"the scan found CONTRADICTED within "
                                   f"{ref.concept_label}; agreeing with one side of "
                                   f"an open contradiction is not consistency"})
        else:
            aliased = sorted(n for n in names
                             if normalize_name(n) != normalize_name(ref.concept_label))
            out.append({**base, "kind": "consistent_use", "severity": 0,
                        "concept": ref.concept_label,
                        "summary": f"matches the {ref.label} exactly"
                                   + (f" (used under alias {', '.join(aliased)})"
                                      if aliased else "")})
        # The math is concept A's; a NAME on it claiming concept B is B's
        # named-alt case — hidden before under the sev-0 match.
        for label in sorted(named - {ref.concept_label}):
            if label in by_concept:
                other, diff = _closest(by_concept[label], observed)
                out.append({**base, "kind": "named_alt_definition",
                            "severity": min(95, SEV_NAMED_ALT_BASE + min(30, observed.uses)),
                            "concept": label, "diff": diff,
                            "summary": f"computed under the name {label}, but its math "
                                       f"is the definition of {ref.concept_label} — "
                                       f"differs from the {other.label} in: "
                                       + ", ".join(diff)})
        return out

    if named:
        label = sorted(named)[0]
        ref, diff = _closest(by_concept[label], observed)
        layered = _layered(diff, observed, ref, models)
        if layered:
            return [{**base, "kind": "unverifiable", "severity": 0, "concept": label,
                     "diff": diff, "summary": f"named {label}; {layered}"}]
        if set(diff) == {"grain"}:
            # Same math, same filters, different grouping: an additive
            # measure sliced differently is a different grain of the same
            # definition, not a dropped filter — ranked accordingly.
            return [{**base, "kind": "named_alt_definition",
                     "severity": min(85, SEV_GRAIN_ONLY_BASE + min(20, observed.uses)),
                     "concept": label, "diff": diff,
                     "summary": f"same measure as the {ref.label}, at a different "
                                f"grain ({', '.join(diff['grain']['observed']) or 'none'} "
                                f"vs {', '.join(diff['grain']['reference']) or 'none'})"}]
        return [{**base, "kind": "named_alt_definition",
                 "severity": min(95, SEV_NAMED_ALT_BASE + min(30, observed.uses)),
                 "concept": label, "diff": diff,
                 "summary": f"computed under the name {label} but differs from the "
                            f"{ref.label} in: " + ", ".join(diff) if diff else
                            f"named {label} with a different canonical core"}]

    # Near core: same math over the same physical columns, different slice.
    # This is a HYPOTHESIS by construction — the analyst never used the name.
    near = [r for refs in by_concept.values() for r in refs
            if r.components["expression"] == observed.components["expression"]
            and r.components["physical lineage"] == observed.components["physical lineage"]]
    if near:
        ref, diff = _closest(near, observed)
        return [{**base, "kind": "candidate_alt_definition",
                 "severity": min(85, SEV_CANDIDATE_BASE + min(30, observed.uses)),
                 "concept": ref.concept_label, "diff": diff,
                 "summary": f"same measure as {ref.concept_label} over the same "
                            f"columns, differing in: " + ", ".join(diff) if diff else
                            f"near {ref.concept_label}"}]

    if observed.semantic_hash in excluded:
        return [{**base, "kind": "unverifiable", "severity": 0, "concept": None,
                 "summary": f"matches the {excluded[observed.semantic_hash]}, which "
                            f"is below proof grade in the reference — no comparison "
                            f"made"}]
    return [{**base, "kind": "unregistered_recurring_measure",
             "severity": SEV_UNREGISTERED, "concept": None,
             "summary": "recurring measure not present in the scanned estate "
                        "or its declarations"}]
