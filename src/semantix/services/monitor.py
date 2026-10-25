"""Query-stream monitoring: what the warehouse executed, judged against
AUTHORITATIVE definitions.

The scan verifies the governed estate — models and declarations someone
registered somewhere. This service reads the other estate: the query stream,
where BI tools, notebooks and agents compute metrics. Most of what it finds
is a governed metric USED correctly — filtered to a region, grouped by a
channel. Some of it is a definition quietly changed. The monitor's job is to
tell those apart without accusing the first and without excusing the second.

Doctrine, applied to monitoring:

* AUTHORITY IS DECLARED, NEVER INFERRED. Executions are judged against the
  definitions an operator named authoritative — a pilot package's approved
  metric views, or `authority_systems`; by default every declared system the
  scan read. A computed model is not authoritative unless its system is
  named; an exact match to one travels as evidence (`also_matches`), never
  as a verdict.
* CONFORMING IS A POSITIVE VERDICT. It comes only from the bounded rules in
  services.conformance (exact canonical equality, or a listed slice/grouping
  shape over declared dimensions). Anything outside them is unverifiable,
  with the reason — never "conforming with a warning".
* REFERENCING IS NOT PRESERVING. A query invoking MEASURE() over an approved
  metric view is a governed REFERENCE; whether it preserves the definition
  is a separate fact on the same finding (`MEASURE(net_revenue) * 2 AS
  net_revenue` references and does not preserve).
* SIMILARITY NOMINATES, EQUALITY DECIDES. A near miss nobody named is a
  CANDIDATE with the exact diff, never a violation. EQUALITY IS NOT
  ENDORSEMENT: matching one side of a disagreement between authoritative
  definitions is contested use, not consistency.
* CONTEXT IS EVIDENCE. A history export mixes sessions. A short table name
  resolves only from its own row's catalog/schema, or from a context the
  operator explicitly asserts covers every execution; otherwise the finding
  is unverifiable. In a pilot comparing as of execution time, an execution
  no definition window covers is unverifiable too.
* RECURRENCE GATES NOISE; BLIND SPOTS DISCLOSE THEMSELVES. Below-threshold
  uses are counted, not judged. Every count is reported against an explicit
  denominator, so a high governed share cannot hide unread queries.
* READ-ONLY (v1). Findings are not recorded in the store; persistence lands
  with its own review, because writing observations shares tables with
  receipts.
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
from semantix.services.conformance import (
    CATEGORY,
    EXCLUDED,
    RULE_SET,
    Authority,
    Outcome,
    check_metric_view,
    check_raw,
    measure_calls,
    metric_view_relation,
    outside_measure_refs,
    raw_shape_problem,
    unverifiable,
)
from semantix.services.history import (
    Execution,
    HistoryIntake,
    from_sql_text,
    qualify_tables,
)
from semantix.services.relationships import _parse_statements  # same package: one
# statement-splitting behavior for every history consumer, not two drifting ones.
from semantix.services.resolution import normalize_name
from semantix.store import Store, normalize_ts

# Aliases the analyst did not write: sqlglot's positional names and the
# extractor's synthesized `<agg>_<column>` names. Attributing them to a
# person, or matching them against a concept that happens to share the
# spelling (`col_0`), produced accusations about names nobody used.
_SYNTHESIZED = re.compile(
    r"^(_col_\d+|(sum|count|avg|min|max|first|last|mode|anyvalue|arrayagg|"
    r"stringagg|listagg|groupconcat|approxdistinct|median|quantile|countif|"
    r"argmax|argmin|boolor|booland)(_[a-z0-9_]+)?)$")
# Explicit identity: a comment the query's author wrote, or the session's
# query tag. Either outranks the projection's alias.
_TAG = re.compile(r"semantix\s*:\s*metric\s*=\s*([A-Za-z_][\w.]*)", re.I)
_QUERY_TAG = re.compile(r"(?:^|[\s,;{\"'])metric\s*[=:]\s*\"?([A-Za-z_][\w.]*)", re.I)

SEV_VIOLATION_BASE = 60      # 60 + min(30, uses)
SEV_CANDIDATE_BASE = 40      # unnamed near miss: a hypothesis
SEV_CONTESTED = 55           # exact match to one side of an authority dispute
SEV_UNREGISTERED = 25
_SAMPLE = 10                 # attribution lists are capped in the output
_NO_TIME = "<no start time>"


@dataclass
class _RefDef:
    """One proof-grade definition from the latest scan's reference set."""

    concept_label: str
    kind: str  # "computed" | "declared"
    system: str
    name: str
    where: str | None
    semantic_hash: str
    core: SemanticCore
    contradicted: bool  # the scan found a CONTRADICTION within the concept

    @property
    def label(self) -> str:
        if self.kind == "declared":
            return f"declared definition {self.system}:{self.name}"
        return f"computed definition at {self.where}"


@dataclass
class _Observed:
    """One distinct thing seen in the query stream: a canonical core (shape
    A) or a metric-view projection (shape B), at one grade and one state of
    evidence, so a single degraded run never relabels clean ones."""

    key: str
    shape: str                      # "A" | "B"
    # What is judged together shares a calculation AND a claimed identity:
    # two analysts computing the same SUM under different metric names are
    # two observations, or one analyst's verdict lands on the other. The
    # base key (calculation, grade, shape, evidence) gates recurrence.
    base_key: str = ""
    identity: str = "none"          # alias:<name> | tag:<name> | output:<name> | none
    semantic_hash: str = ""
    proof_grade: bool = False
    core: SemanticCore | None = None
    components: dict[str, object] = field(default_factory=dict)
    shape_problem: str | None = None
    evidence: frozenset | None = None   # views whose window covers the run
    explicit: tuple[str, str] | None = None   # (source, name)
    view: str | None = None
    measure: str | None = None
    output: str | None = None
    view_outcome: Outcome | None = None
    expression: str | None = None   # the metric-view projection as written
    governed_reference: bool = False
    uses: int = 0
    names: set[str] = field(default_factory=set)
    relations: set[str] = field(default_factory=set)
    statements: set[str] = field(default_factory=set)
    gaps: set[str] = field(default_factory=set)
    executed_by: set[str] = field(default_factory=set)
    clients: set[str] = field(default_factory=set)
    assets: set[str] = field(default_factory=set)
    execution_ids: list[str] = field(default_factory=list)
    sample_sql: str | None = None

    def attribute(self, ex: Execution) -> None:
        for target, value in ((self.executed_by, ex.executed_by),
                              (self.clients, ex.client_application),
                              (self.assets, ex.asset)):
            if value:
                target.add(value)
        if ex.statement_id and len(self.execution_ids) < _SAMPLE:
            self.execution_ids.append(ex.statement_id)


def build_registry(catalog: dict, artifacts: list, dialect: str,
                   execution_context: dict | None = None) -> SchemaRegistry:
    """The scan's registry, rebuilt without store writes: catalog plus every
    computed model registered upstream-first, so observed queries resolve
    columns through models to the same raw terminals reference cores carry.
    Sorted here — a caller passing multi-source artifacts unsorted produced a
    registry whose terminals differed from the scan's. The execution context
    and estate depth must match the scan's too, or relation identities would
    be computed in a different space than the reference cores."""
    ctx = execution_context or {}
    registry = SchemaRegistry(catalog, ctx.get("default_database"), ctx.get("default_schema"))
    ordered = topo_sort(list(artifacts))
    registry.set_estate_depth([a.relation for a in ordered]
                              + [d for a in ordered for d in a.depends_on])
    for artifact in ordered:
        canon = canonicalize_sql(
            artifact.sql, registry.sqlglot_schema(dialect), dialect)
        registry.register_model(artifact.relation, canon.ast)
    return registry


def monitor_history(
    store: Store,
    history: str | HistoryIntake,
    registry: SchemaRegistry,
    dialect: str,
    min_runs: int = 2,
    *,
    authority_systems: set[str] | None = None,
    pilot=None,
    context: dict | None = None,
    assert_context: bool = False,
    asset_map: dict[str, str] | None = None,
) -> dict:
    """Findings from executed SQL, judged against authoritative definitions.

    `history` is SQL text (one execution per statement) or a HistoryIntake
    (one execution per row, deduplicated by statement id). `pilot` (a
    services.pilot.Pilot) supplies approved metric views, their definition
    windows, and any asserted context; without one, `context` applies to
    history rows only when `assert_context` says it covers every execution.
    """
    scan = store.latest_scan()
    if scan is None:
        raise ValueError("no scans recorded — monitor needs a scan's "
                         "reference set to compare against; run `semantix "
                         "scan` first")
    intake = history if isinstance(history, HistoryIntake) else from_sql_text(history)
    if pilot is not None:
        from semantix.services.pilot import verify_against_scan

        verify_against_scan(pilot, store, scan["scan_id"])
        asset_map = {**(asset_map or {}), **pilot.asset_map}
    authorities, computed, excluded, unreadable, ref_meta = _authority_set(
        store, scan["scan_id"], registry, dialect, pilot, authority_systems)

    by_hash: dict[str, list[Authority]] = {}
    by_name: dict[str, set[str]] = {}
    by_concept: dict[str, list[Authority]] = {}
    approved: dict[str, dict[str, Authority]] = {}
    for auth in authorities:
        by_hash.setdefault(auth.core.hash(), []).append(auth)
        by_concept.setdefault(auth.concept, []).append(auth)
        for alias in {auth.name, auth.concept}:
            by_name.setdefault(normalize_name(alias), set()).add(auth.concept)
        if auth.view:
            approved.setdefault(auth.view, {})[auth.measure] = auth
    for view in (pilot.views if pilot is not None else []):
        for spec in view.specs:
            approved.setdefault(view.name, {}).setdefault(spec.name.lower(), None)
    contested = {c for c, defs in by_concept.items()
                 if len({d.core.hash() for d in defs}) > 1}
    computed_by_hash: dict[str, list[_RefDef]] = {}
    for ref in computed:
        computed_by_hash.setdefault(ref.semantic_hash, []).append(ref)

    # Session context: a row's own catalog/schema, else an asserted one.
    if pilot is not None and pilot.context_asserted:
        asserted = (pilot.context_catalog, pilot.context_schema)
    elif assert_context and context:
        asserted = (context.get("default_database"), context.get("default_schema"))
    else:
        asserted = (None, None)
    history_registry = registry.without_context()
    depth = registry.estate_depth()
    as_of = pilot is not None and pilot.compare_against == "as_of_execution"

    observed: dict[str, _Observed] = {}
    recurrence: dict[str, int] = {}   # base key -> statements using the calculation
    counts = {"statements": 0, "parsed": 0, "unparseable": 0, "non_query": 0,
              "unreadable": 0, "no_measure": 0, "nested_measure": 0,
              "without_time": 0, "mixed_measures": 0}
    executions_read = 0
    for ex in intake.executions:
        statements, unparseable = _parse_statements(ex.statement_text, dialect)
        counts["statements"] += len(statements) + unparseable
        counts["parsed"] += len(statements)
        counts["unparseable"] += unparseable
        executions_read += (len(statements) + unparseable) if intake.format == "sql" else 1
        catalog = ex.catalog or asserted[0]
        schema = ex.schema or asserted[1]
        evidence = None
        if as_of:
            ts = _execution_time(ex)
            if ts is None:
                counts["without_time"] += 1
                evidence = frozenset({_NO_TIME})
            else:
                evidence = frozenset(a.view for a in authorities
                                     if a.view and a.applies_at(ts))
        explicit = _explicit_identity(ex, asset_map or {})
        for stmt in statements:
            if not isinstance(stmt, exp.Query):
                counts["non_query"] += 1
                continue
            qualify_tables(stmt, catalog, schema, depth)
            if measure_calls(stmt):
                if any(outside_measure_refs(a) for a in stmt.find_all(exp.AggFunc)):
                    # Raw aggregates beside governed ones: the measure
                    # references are judged (unverifiable), the raw ones are
                    # not read — say so rather than drop them silently.
                    counts["mixed_measures"] += 1
                _observe_view(stmt, ex, approved, evidence, observed, recurrence, dialect)
                continue
            outcome = _observe_core(stmt, ex, history_registry, dialect, evidence,
                                    explicit, observed, recurrence)
            if outcome:
                counts[outcome] += 1

    recurring = [o for o in observed.values() if recurrence[o.base_key] >= min_runs]
    models = registry.known_models()
    label = pilot.comparison_label() if pilot is not None else (
        "compared against the latest scan's definitions; not verified as of "
        "execution time")
    judged: list[tuple[dict, _Observed]] = []
    funnel = _Funnel()
    for obs in sorted(recurring, key=lambda o: (-o.uses, o.key)):
        out = _judge(obs, by_hash, by_name, by_concept, contested, computed_by_hash,
                     excluded, unreadable, models, dialect, as_of)
        # The use's headline outcome is its MOST SEVERE finding: a supporting
        # match must never outrank a violation of the metric it claims.
        primary = max(out, key=lambda f: _PRECEDENCE[f["kind"]])
        for f in out:
            f["compare_against"] = label
            f["role"] = "primary" if f is primary else "additional"
            judged.append((f, obs))
        funnel.add(obs, primary)
    for obs in observed.values():
        if recurrence[obs.base_key] < min_runs:
            funnel.add_below(obs)
    findings = _aggregate(judged)
    findings.sort(key=lambda f: (-f["severity"], -f["uses"], f["kind"], f["concept"] or ""))
    bases = {o.base_key for o in observed.values()}
    recurring_bases = {b for b in bases if recurrence[b] >= min_runs}
    return {
        "scan_id": scan["scan_id"],
        "reference": ref_meta,
        "rule_set": RULE_SET,
        "excluded_semantics": list(EXCLUDED),
        "comparison": (pilot.comparison_label() if pilot is not None else
                       "latest scan's definitions"),
        "context": {"asserted": list(asserted) if any(asserted) else None,
                    "per_row": any(e.catalog or e.schema for e in intake.executions)},
        "history": {"source": intake.source, "format": intake.format,
                    "rows": intake.rows, "duplicate_executions": intake.duplicate_executions,
                    "empty_rows": intake.empty_rows,
                    "ignored_columns": intake.ignored_columns,
                    "executions": executions_read,
                    "executions_without_time": counts["without_time"]},
        "statements": counts["statements"],
        "parsed_statements": counts["parsed"],
        "skipped_statements": counts["unparseable"],
        "non_query_statements": counts["non_query"],
        "unreadable_statements": counts["unreadable"],
        "statements_without_measures": counts["no_measure"],
        "statements_with_nested_measures": counts["nested_measure"],
        "statements_mixing_raw_aggregates": counts["mixed_measures"],
        "observed_definitions": len(bases),
        "recurring_definitions": len(recurring_bases),
        "below_min_runs": len(bases) - len(recurring_bases),
        "min_runs": min_runs,
        "funnel": funnel.as_dict(),
        "findings": findings,
        "recorded": False,  # v1: read-only — nothing written to the store
    }


# ------------------------------------------------------------ observation

def _observe_core(stmt: exp.Expression, ex: Execution, registry: SchemaRegistry,
                  dialect: str, evidence, explicit, observed: dict,
                  recurrence: dict) -> str | None:
    """Shape A intake for one statement. Returns a disclosure counter name
    when the statement yields no measure to judge."""
    sql_text = stmt.sql(dialect=dialect)
    stmt_id = hashlib.sha256(sql_text.encode()).hexdigest()[:12]
    shape_problem = raw_shape_problem(stmt)
    canon = canonicalize_sql(sql_text, registry.sqlglot_schema(dialect), dialect)
    if canon.level == CanonLevel.FAILED or canon.ast is None:
        return "unreadable"
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
        return "nested_measure" if ast.find(exp.AggFunc) is not None else "no_measure"
    # Reading a model's AGGREGATE output is a cross-level comparison the
    # static core cannot make (build_claim refuses the same shape for
    # declarations): `SUM(net_revenue) FROM rev_mart` inherits the mart's
    # filter, it does not drop it. Unverifiable here, never a violation.
    referenced = {c.name.lower() for c in ast.find_all(exp.Column)}
    reagg = sorted(rel for rel in reads
                   if {c.lower() for c in registry.aggregate_outputs(rel)} & referenced)
    # An explicit identity names ONE metric; on a multi-measure statement it
    # is ambiguous which projection it means, so it is not applied.
    explicit = explicit if len(fps) == 1 else None
    groups: dict[tuple[str, str], list] = {}
    for fp in fps:
        groups.setdefault((fp.core.hash(), _identity(fp.concept_candidate, explicit)),
                          []).append(fp)
    counted: set[str] = set()
    for (core_hash, identity), group in groups.items():
        fp = group[0]
        gaps = set(fp.gaps)
        if reagg:
            gaps.add(f"reads the aggregate output of {', '.join(reagg)} — "
                     f"through-model comparison not made")
        proof = fp.canon_level in PROOF_LEVELS and not gaps
        base_key = "|".join(["A", core_hash, "proof" if proof else "gapped",
                             shape_problem or "", _evidence_key(evidence)])
        key = f"{base_key}|{identity}"
        obs = observed.setdefault(key, _Observed(
            key=key, shape="A", base_key=base_key, identity=identity,
            semantic_hash=core_hash, proof_grade=proof,
            core=fp.core, components=core_components(fp.core),
            shape_problem=shape_problem, evidence=evidence, explicit=explicit))
        obs.uses += 1
        obs.names.update(f.concept_candidate for f in group)
        obs.relations.update(reads)
        obs.statements.add(stmt_id)
        obs.gaps.update(gaps)
        obs.attribute(ex)
        if obs.sample_sql is None:
            obs.sample_sql = fp.canonical_sql
        if base_key not in counted:
            counted.add(base_key)
            recurrence[base_key] = recurrence.get(base_key, 0) + 1
    return None


def _identity(alias: str | None, explicit: tuple[str, str] | None) -> str:
    if explicit:
        return f"{explicit[0]}:{normalize_name(explicit[1])}"
    if alias and not _SYNTHESIZED.match(alias.lower()):
        return f"alias:{normalize_name(alias)}"
    return "none"


def _observe_view(stmt: exp.Expression, ex: Execution,
                  approved: dict[str, dict[str, Authority]], evidence,
                  observed: dict, recurrence: dict, dialect: str) -> None:
    """Shape B intake: one observation per measure-reference projection."""
    sql_text = stmt.sql(dialect=dialect)
    stmt_id = hashlib.sha256(sql_text.encode()).hexdigest()[:12]
    view = metric_view_relation(stmt)
    counted: set[str] = set()
    for proj in check_metric_view(stmt, approved, dialect):
        signature = json.dumps(proj.outcome.as_dict(), sort_keys=True)
        base_key = "|".join(["B", view or "", proj.measure,
                             hashlib.sha256(signature.encode()).hexdigest()[:16],
                             _evidence_key(evidence), proj.expression or ""])
        identity = f"output:{normalize_name(proj.output)}" if proj.output else "none"
        key = f"{base_key}|{identity}"
        obs = observed.setdefault(key, _Observed(
            key=key, shape="B", base_key=base_key, identity=identity, view=view,
            measure=proj.measure, output=proj.output, view_outcome=proj.outcome,
            expression=proj.expression, evidence=evidence,
            governed_reference=proj.governed_reference,
            semantic_hash=hashlib.sha256(f"{view}/{proj.measure}".encode()).hexdigest()))
        obs.uses += 1
        if base_key not in counted:
            counted.add(base_key)
            recurrence[base_key] = recurrence.get(base_key, 0) + 1
        if proj.output:
            obs.names.add(proj.output)
        if view:
            obs.relations.add(view)
        obs.statements.add(stmt_id)
        obs.attribute(ex)
        if obs.sample_sql is None:
            obs.sample_sql = sql_text


def _evidence_key(evidence) -> str:
    if evidence is None:
        return ""
    return "window:" + ",".join(sorted(evidence))


def _execution_time(ex: Execution) -> str | None:
    if not ex.start_time:
        return None
    try:
        return normalize_ts(ex.start_time.replace(" ", "T", 1))
    except ValueError:
        return None


def _explicit_identity(ex: Execution, asset_map: dict[str, str]) -> tuple[str, str] | None:
    m = _TAG.search(ex.statement_text)
    if m:
        return ("tag", m.group(1))
    if ex.query_tag:
        m = _QUERY_TAG.search(ex.query_tag)
        if m:
            return ("query_tag", m.group(1))
    if ex.asset and ex.asset in asset_map:
        return ("asset_map", asset_map[ex.asset])
    return None


# --------------------------------------------------------------- authority

def _authority_set(store: Store, scan_id: str, registry: SchemaRegistry,
                   dialect: str, pilot, systems: set[str] | None):
    """(authorities, non-authoritative computed refs, excluded, meta)."""
    refs, excluded, contradicted = _reference(store, scan_id)
    authorities: list[Authority] = []
    unreadable: dict[str, str] = {}   # normalized metric name -> why
    if pilot is not None:
        from semantix.engine import build_claim

        for view in pilot.views:
            for spec in view.specs:
                claim = build_claim(spec, registry, dialect)
                if not claim.expressible or claim.core is None \
                        or claim.canon_level not in PROOF_LEVELS or claim.gaps:
                    why = (f"approved metric view {view.name}/{spec.name} "
                           f"({claim.error or claim.canon_level})")
                    excluded.setdefault(f"pilot:{view.name}/{spec.name}", why)
                    unreadable.setdefault(normalize_name(spec.name), why)
                    continue
                authorities.append(Authority(
                    concept=spec.name, system=spec.system, name=spec.name,
                    core=claim.core, dimensions=spec.dimensions, view=view.name,
                    measure=spec.name.lower(), applies_from=view.applies_from,
                    applies_to=view.applies_to, where=spec.path))
        # Everything the scan read except the approved definitions themselves
        # is evidence (`also_matches`), never authority.
        approved = {(pilot.system, s.name.lower()) for v in pilot.views for s in v.specs}
        computed = [r for r in refs
                    if not (r.kind == "declared" and (r.system, r.name.lower()) in approved)]
        chosen = sorted({pilot.system})
    else:
        def is_authority(r: _RefDef) -> bool:
            return r.system in systems if systems else r.kind == "declared"
        for r in refs:
            if is_authority(r):
                authorities.append(Authority(
                    concept=r.concept_label, system=r.system, name=r.name,
                    core=r.core, where=r.where, kind=r.kind))
        computed = [r for r in refs if not is_authority(r)]
        chosen = sorted(systems) if systems else sorted(
            {r.system for r in refs if r.kind == "declared"})
    concepts = {a.concept for a in authorities}
    meta = {"concepts": len(concepts), "definitions": len(authorities),
            "authority_systems": chosen,
            "non_authoritative_definitions": len(computed),
            "excluded_below_proof": len(excluded),
            "contested_concepts": sorted(
                c for c in concepts
                if len({a.core.hash() for a in authorities if a.concept == c}) > 1),
            "scan_contradicted_concepts": sorted(contradicted)}
    return authorities, computed, excluded, unreadable, meta


def _reference(store: Store, scan_id: str
               ) -> tuple[list[_RefDef], dict[str, str], set[str]]:
    """(proof-grade definitions, excluded hash -> description, concepts the
    scan found CONTRADICTED)."""
    contradicted = {v["concept_label"] for v in store.verdicts_for_scan(scan_id)
                    if v["verdict"] == "CONTRADICTION"}
    refs: list[_RefDef] = []
    excluded: dict[str, str] = {}
    for concept in store.concepts_for_scan(scan_id):
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
                semantic_hash=row["semantic_hash"], core=SemanticCore.from_row(row),
                contradicted=label in contradicted))
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
                semantic_hash=row["semantic_hash"], core=SemanticCore.from_row(row),
                contradicted=label in contradicted))
    return refs, excluded, contradicted


# ---------------------------------------------------------------- judging

def _attributable(names: set[str]) -> list[str]:
    return sorted(n for n in names if not _SYNTHESIZED.match(n.lower()))


def _diff(reference: SemanticCore, observed: dict) -> dict:
    return core_diff(core_components(reference), observed, "reference", "observed")


def _base(obs: _Observed, names: list[str]) -> dict:
    return {
        "uses": obs.uses,
        "distinct_statements": len(obs.statements),
        "names_used": names,
        "relations_read": sorted(obs.relations),
        "semantic_hash": obs.semantic_hash[:12],
        "sample_sql": obs.sample_sql,
        "governed_reference": obs.governed_reference,
        "executed_by": sorted(obs.executed_by)[:_SAMPLE],
        "clients": sorted(obs.clients)[:_SAMPLE],
        "assets": sorted(obs.assets)[:_SAMPLE],
        "execution_ids": list(obs.execution_ids),
        "rule_set": RULE_SET,
    }


def _finding(base: dict, kind: str, severity: int, concept: str | None,
             conformance: str, identity: str, summary: str,
             outcome: Outcome | None = None, **extra) -> dict:
    f = {**base, "kind": kind, "severity": severity, "concept": concept,
         "conformance": conformance, "identity_source": identity,
         "summary": summary}
    if outcome is not None:
        f["shape"] = outcome.shape
        f["reasons"] = list(outcome.reasons)
        f["variation"] = outcome.variation
    f.update(extra)
    return f


def _violation_severity(uses: int) -> int:
    return min(95, SEV_VIOLATION_BASE + min(30, uses))


def _reason_text(outcome: Outcome) -> str:
    return "; ".join(r["detail"] for r in outcome.reasons)


def _variation_text(outcome: Outcome) -> str:
    v = outcome.variation or {}
    parts = []
    if v.get("slices"):
        parts.append("sliced by " + " AND ".join(v["slices"]))
    if v.get("grain"):
        parts.append("grouped by " + ", ".join(v["grain"]))
    return (" — " + "; ".join(parts)) if parts else ""


def _judge(obs: _Observed, by_hash, by_name, by_concept, contested, computed_by_hash,
           excluded, unreadable, models, dialect, as_of) -> list[dict]:
    if obs.shape == "B":
        return _judge_view(obs, by_name, by_concept, contested, as_of)
    names = _attributable(obs.names)
    base = _base(obs, names)

    if not obs.proof_grade:
        # An INCOMPLETE core omits constructs — equality or difference over it
        # proves nothing, so it anchors neither an accusation nor a
        # confirmation. Counted, described, left alone.
        reason = "; ".join(sorted(obs.gaps)) or "canonicalization below proof grade"
        code = ("context_unresolved" if "relation identity depends on the session" in reason
                else "through_model" if "through-model" in reason else "below_proof_grade")
        out = unverifiable(code, reason)
        return [_finding(base, "unverifiable", 0, None, "unverifiable", "none",
                         f"recurring measure could not be read at proof grade "
                         f"({reason}) — no comparison made", out)]

    if obs.explicit:
        source, name = obs.explicit
        named = set(by_name.get(normalize_name(name), ()))
        identity = source
        if not named:
            base = {**base, "identity_note": f"{source} names {name}, which is not "
                                             f"an authoritative metric"}
            named = {label for alias in names
                     for label in by_name.get(normalize_name(alias), ())}
            identity = "alias"
    else:
        named = {label for alias in names for label in by_name.get(normalize_name(alias), ())}
        identity = "alias"
    also = [r.label for r in computed_by_hash.get(obs.semantic_hash, [])]
    if also:
        base = {**base, "also_matches": also}

    exact = by_hash.get(obs.semantic_hash)
    out: list[dict] = []
    if exact:
        auth = sorted(exact, key=lambda a: (a.concept, a.label))[0]
        if named and auth.concept not in named:
            # The math is concept A's; the query CLAIMS concept B. B's verdict
            # is the finding, and the match to A is evidence on it — never a
            # separate conforming finding that could stand in for it. The
            # match is only evidence if A's definition applied at the time.
            matched = auth.label if not as_of or _covered(obs, auth) else (
                f"{auth.label} as snapshotted — not evidenced for the execution time")
            evidence = {**base, "calculation_matches": matched}
            return [_judge_against(obs, evidence, label, by_concept[label], contested,
                                   models, dialect, as_of, identity)
                    for label in sorted(named)]
        out.append(_judge_against(obs, base, auth.concept, [auth], contested, models,
                                  dialect, as_of, "definition_match"
                                  if auth.concept not in named else identity))
        for label in sorted(named - {auth.concept}):
            out.append(_judge_against(obs, base, label, by_concept[label], contested,
                                      models, dialect, as_of, identity))
        return out

    if named:
        for label in sorted(named):
            out.append(_judge_against(obs, base, label, by_concept[label], contested,
                                      models, dialect, as_of, identity))
        return out

    # Named after an authoritative metric whose definition could not be
    # read: the comparison cannot be made, which is not the same as there
    # being nothing to compare against.
    dark = sorted((n, unreadable[normalize_name(n)]) for n in names
                  if normalize_name(n) in unreadable)
    if dark:
        name, why = dark[0]
        return [_finding(base, "unverifiable", 0, name, "unverifiable", identity,
                         f"named after an authoritative metric whose definition "
                         f"could not be read: {why}",
                         unverifiable("below_proof_grade", why))]

    # Nobody named it. Same math over the same physical columns as an
    # authority: if the bounded rules say it IS that definition sliced, it
    # conforms (identity established by the definition, as for an exact
    # match); if they say it differs, it is a CANDIDATE — a hypothesis.
    near = [a for defs in by_concept.values() for a in defs
            if a.core.measure_expr == obs.core.measure_expr
            and a.core.source_columns == obs.core.source_columns]
    if near:
        judged = []
        for auth in near:
            if as_of and not _covered(obs, auth):
                continue
            judged.append((auth, check_raw(obs.core, auth, dialect,
                                           obs.shape_problem, models)))
        conforming = [(a, o) for a, o in judged if o.status == "conforming"]
        if conforming:
            auth, outcome = sorted(conforming, key=lambda t: t[0].concept)[0]
            return [_conforming(obs, base, auth, outcome, contested, "definition_match")]
        violated = [(a, o) for a, o in judged if o.status == "violation"]
        if violated:
            auth, outcome = sorted(violated, key=lambda t: (len(t[1].reasons),
                                                             t[0].concept))[0]
            diff = _diff(auth.core, obs.components)
            return [_finding(
                base, "candidate_alt_definition",
                min(85, SEV_CANDIDATE_BASE + min(30, obs.uses)), auth.concept,
                "not_applicable", "similarity",
                f"same measure as {auth.concept} over the same columns, "
                f"unnamed — {_reason_text(outcome)}", outcome, diff=diff)]
        auth = near[0]
        outcome = (judged[0][1] if judged else unverifiable(
            "no_definition_evidence", "no definition window covers these executions"))
        return [_finding(base, "unverifiable", 0, auth.concept, "unverifiable",
                         "similarity",
                         f"near {auth.concept}, unnamed — {_reason_text(outcome)}",
                         outcome)]

    if obs.semantic_hash in excluded:
        return [_finding(base, "unverifiable", 0, None, "unverifiable", "none",
                         f"matches the {excluded[obs.semantic_hash]}, which is "
                         f"below proof grade in the reference — no comparison made",
                         unverifiable("below_proof_grade", excluded[obs.semantic_hash]))]
    summary = "recurring measure not defined by any authoritative definition"
    if also:
        summary += f" (matches the {also[0]}, which is not declared authoritative)"
    return [_finding(base, "unregistered_recurring_measure", SEV_UNREGISTERED, None,
                     "not_applicable", "none", summary)]


def _covered(obs: _Observed, auth: Authority) -> bool:
    return auth.view is not None and obs.evidence is not None and auth.view in obs.evidence


def _judge_against(obs: _Observed, base: dict, concept: str, defs: list[Authority],
                   contested: set[str], models, dialect, as_of, identity) -> dict:
    """One identified concept: judge the observed core against each of its
    authoritative definitions and report the strongest true statement."""
    if as_of:
        covered = [a for a in defs if _covered(obs, a)]
        if not covered:
            outcome = unverifiable(
                "no_definition_evidence",
                f"no definition of {concept} is evidenced for the execution time"
                + (" (the executions carry no start time)"
                   if obs.evidence and _NO_TIME in obs.evidence else ""))
            return _finding(base, "unverifiable", 0, concept, "unverifiable", identity,
                            f"{concept}: {_reason_text(outcome)}", outcome)
        defs = covered
    judged = [(a, check_raw(obs.core, a, dialect, obs.shape_problem, models))
              for a in defs]
    conforming = [(a, o) for a, o in judged if o.status == "conforming"]
    if conforming:
        auth, outcome = sorted(conforming, key=lambda t: t[0].label)[0]
        return _conforming(obs, base, auth, outcome, contested, identity)
    violated = [(a, o) for a, o in judged if o.status == "violation"]
    if violated:
        auth, outcome = sorted(violated, key=lambda t: (len(t[1].reasons), t[0].label))[0]
        diff = _diff(auth.core, obs.components)
        matched = (f" — its calculation is the {base['calculation_matches']}"
                   if base.get("calculation_matches") else "")
        return _finding(base, "definition_violation", _violation_severity(obs.uses),
                        concept, "violation", identity,
                        f"computed under the name {concept} but does not preserve the "
                        f"{auth.label}: {_reason_text(outcome)}{matched}", outcome,
                        diff=diff)
    auth, outcome = judged[0]
    return _finding(base, "unverifiable", 0, concept, "unverifiable", identity,
                    f"named {concept}; {_reason_text(outcome)}", outcome,
                    diff=_diff(auth.core, obs.components))


def _conforming(obs: _Observed, base: dict, auth: Authority, outcome: Outcome,
                contested: set[str], identity: str) -> dict:
    if auth.concept in contested:
        return _finding(base, "contested_use", SEV_CONTESTED, auth.concept, "conforming",
                        identity,
                        f"conforms to the {auth.label}, but the authoritative "
                        f"definitions of {auth.concept} disagree with each other — "
                        f"agreeing with one side of an open contradiction is not "
                        f"consistency", outcome)
    how = "matches the" if outcome.shape == "exact" else "conforms to the"
    return _finding(base, "conforming_use", 0, auth.concept, "conforming", identity,
                    f"{how} {auth.label}{_variation_text(outcome)}", outcome)


def _judge_view(obs: _Observed, by_name, by_concept, contested, as_of) -> list[dict]:
    """Shape B, in a fixed order — each step can only narrow what the next
    may conclude:

    1. definition evidence, for the REFERENCED metric and for any metric the
       output name CLAIMS;
    2. the supported-SQL boundary (the conformance rules' verdict on the
       statement);
    3. the claimed metric;
    4. the primary finding, chosen by the caller from these conclusions.

    A naming mismatch found before evidence or the boundary failed is kept
    as context (`suspected`), never promoted to a violation: round ten's
    name branch ran last and overwrote "unverifiable" unconditionally."""
    names = sorted(obs.names)
    base = _base(obs, names)
    outcome = obs.view_outcome
    auth = None
    for defs in by_concept.values():
        for a in defs:
            if a.view == obs.view and a.measure == obs.measure:
                auth = a
    concept = auth.concept if auth else None
    # Presented under ANOTHER authoritative metric's name. Only for an
    # approved, readable measure — over an unknown view the identity of what
    # was computed is itself unknown.
    claimed = (sorted(by_name.get(normalize_name(obs.output), set()) - {concept})
               if obs.output and concept is not None else [])
    suspected = (f"presented as {obs.output} but computed as {obs.expression} over "
                 f"{obs.view}") if claimed else None

    # 1. Definition evidence, referenced and claimed.
    if as_of:
        missing = []
        if auth is not None and not _covered(obs, auth):
            missing.append(f"{obs.view}/{obs.measure}")
        missing += [label for label in claimed
                    if not any(_covered(obs, a) for a in by_concept[label])]
        if missing:
            outcome = unverifiable(
                "no_definition_evidence",
                f"no definition of {', '.join(missing)} is evidenced for the execution "
                f"time" + (" (the executions carry no start time)"
                           if obs.evidence and _NO_TIME in obs.evidence else ""),
                shape="B")

    # 2. The supported-SQL boundary. `derived_expression` is not a boundary:
    # it is the projection's own formula, which step 3 judges.
    outside = outcome.status == "unverifiable" and set(outcome.codes) - {"derived_expression"}

    out: list[dict] = []
    if claimed:
        context = {**base, "suspected": suspected}
        for label in claimed:
            if outside:
                out.append(_finding(context, "unverifiable", 0, label, "unverifiable",
                                    "alias",
                                    f"named {label}, {suspected} — not judged: "
                                    f"{_reason_text(outcome)}", outcome))
                continue
            # 3. The claimed metric, with what the reference computes as evidence.
            other = Outcome("violation", [{
                "code": "presented_under_other_name", "detail": suspected}],
                outcome.variation, "B")
            out.append(_finding({**base, "calculation_matches":
                                 f"{obs.view}/{obs.measure} ({outcome.status})"},
                                "definition_violation", _violation_severity(obs.uses),
                                label, "violation", "alias",
                                f"computed under the name {label}: "
                                f"{_reason_text(other)}", other))
        return out
    if outcome.status == "conforming" and auth is not None:
        out.append(_conforming(obs, base, auth, outcome, contested, "measure_invocation"))
    elif outcome.status == "violation":
        out.append(_finding(base, "definition_violation", _violation_severity(obs.uses),
                            concept, "violation", "measure_invocation",
                            f"references {obs.view}/{obs.measure} but does not preserve "
                            f"it: {_reason_text(outcome)}", outcome))
    else:
        out.append(_finding(base, "unverifiable", 0, concept, "unverifiable",
                            "measure_invocation",
                            f"references {obs.view or '?'}/{obs.measure}: "
                            f"{_reason_text(outcome)}", outcome))
    return out


# A use's headline bucket is its most severe finding.
_PRECEDENCE = {"definition_violation": 5, "contested_use": 4,
               "candidate_alt_definition": 3, "unverifiable": 2,
               "unregistered_recurring_measure": 1, "conforming_use": 0}
_ATTRIBUTION = frozenset({"uses", "distinct_statements", "names_used", "relations_read",
                          "sample_sql", "executed_by", "clients", "assets",
                          "execution_ids", "severity", "summary"})


def _aggregate(judged: list[tuple[dict, _Observed]]) -> list[dict]:
    """Merge findings that genuinely share a verdict — same kind, concept,
    reasons, identity source, calculation and role — and nothing else, so
    one analyst's attribution never lands on another's finding."""
    groups: dict[str, list[tuple[dict, _Observed]]] = {}
    for f, obs in judged:
        sig = json.dumps({k: v for k, v in f.items() if k not in _ATTRIBUTION},
                         sort_keys=True, default=str)
        groups.setdefault(sig, []).append((f, obs))
    out = []
    for items in groups.values():
        f = dict(items[0][0])
        if len(items) > 1:
            obss = [o for _, o in items]
            f["uses"] = sum(o.uses for o in obss)
            f["distinct_statements"] = len(set().union(*(o.statements for o in obss)))
            f["names_used"] = sorted(set().union(*(set(g["names_used"]) for g, _ in items)))
            f["relations_read"] = sorted(set().union(*(o.relations for o in obss)))
            for key in ("executed_by", "clients", "assets"):
                f[key] = sorted(set().union(*(getattr(o, key) for o in obss)))[:_SAMPLE]
            f["execution_ids"] = [i for o in obss for i in o.execution_ids][:_SAMPLE]
            if f["kind"] == "definition_violation":
                f["severity"] = _violation_severity(f["uses"])
            elif f["kind"] == "candidate_alt_definition":
                f["severity"] = min(85, SEV_CANDIDATE_BASE + min(30, f["uses"]))
        out.append(f)
    return out


class _Funnel:
    """Every measure use lands in exactly one bucket; governed references
    are counted alongside, never instead."""

    _KINDS = ("conforming_use", "contested_use", "definition_violation",
              "candidate_alt_definition", "unregistered_recurring_measure",
              "unverifiable")

    def __init__(self):
        self.uses = 0
        self.governed = 0
        self.by_kind = {k: 0 for k in self._KINDS}
        self.below_threshold = 0
        self.conforming_by_shape = {"exact": 0, "A": 0, "B": 0}
        self.unverifiable_by = {}

    def add(self, obs: _Observed, primary: dict) -> None:
        self.uses += obs.uses
        if obs.governed_reference:
            self.governed += obs.uses
        kind = primary["kind"]
        self.by_kind[kind] += obs.uses
        if kind in ("conforming_use", "contested_use") and primary.get("shape"):
            self.conforming_by_shape[primary["shape"]] += obs.uses
        if kind == "unverifiable":
            code = (primary.get("reasons") or [{"code": "below_proof_grade"}])[0]["code"]
            cat = CATEGORY.get(code, "other")
            self.unverifiable_by[cat] = self.unverifiable_by.get(cat, 0) + obs.uses

    def add_below(self, obs: _Observed) -> None:
        self.uses += obs.uses
        self.below_threshold += obs.uses
        if obs.governed_reference:
            self.governed += obs.uses

    def as_dict(self) -> dict:
        checked = self.by_kind["conforming_use"] + self.by_kind["definition_violation"]
        return {"measure_uses": self.uses, "governed_references": self.governed,
                "conforming": self.by_kind["conforming_use"],
                "conforming_by_shape": dict(self.conforming_by_shape),
                "contested": self.by_kind["contested_use"],
                "violations": self.by_kind["definition_violation"],
                "candidates": self.by_kind["candidate_alt_definition"],
                "unregistered": self.by_kind["unregistered_recurring_measure"],
                "unverifiable": self.by_kind["unverifiable"],
                "unverifiable_by_reason": dict(sorted(self.unverifiable_by.items())),
                "below_threshold": self.below_threshold,
                "checked": checked}
