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

import contextvars
import hashlib
import json
import re
from dataclasses import dataclass, field

from sqlglot import exp

from touchstone.adapters.base import topo_sort
from touchstone.conclusions import (
    POSITIVE_VERDICTS,
    SUSPECTED_REASON,
    SUSPENSION_REASON,
    effective_finding,
)
from touchstone.extraction.canonicalize import canonicalize, canonicalize_sql
from touchstone.extraction.fingerprint import extract_fingerprints, unrecognized_measures
from touchstone.extraction.proof import is_grammar_gap
from touchstone.extraction.registry import SchemaRegistry
from touchstone.extraction.sqlutil import (
    faithful_render,
    misread_literal,
    table_fqname,
    unaccepted_truncation,
)
from touchstone.models import (
    PROOF_LEVELS,
    CanonLevel,
    Provenance,
    SemanticCore,
    core_components,
    core_diff,
)
from touchstone.services.conformance import (
    CATEGORY,
    EXCLUDED,
    RULE_SET,
    Authority,
    Outcome,
    check_metric_view,
    check_raw,
    measure_calls,
    measure_ref,
    metric_view_relation,
    outside_measure_refs,
    raw_shape_problem,
    unverifiable,
)
from touchstone.services.history import (
    Execution,
    HistoryIntake,
    from_sql_text,
    qualify_tables,
)
from touchstone.services.relationships import statement_chunks  # same package: one
from touchstone.services.resolution import normalize_name

# statement-splitting behavior for every history consumer, not two drifting ones.
from touchstone.services.session import classify, resolve, written_relations
from touchstone.store import Store, utcnow
from touchstone.toolchain import toolchain_status

# Aliases the analyst did not write: sqlglot's positional names and the
# extractor's synthesized `<agg>_<column>` names. Attributing them to a
# person, or matching them against a concept that happens to share the
# spelling (`col_0`), produced accusations about names nobody used.
_SYNTHESIZED = re.compile(
    r"^(_col_\d+|(sum|count|avg|min|max|first|last|mode|anyvalue|arrayagg|"
    r"stringagg|listagg|groupconcat|approxdistinct|median|quantile|countif|"
    r"argmax|argmin|boolor|booland)(_[a-z0-9_]+)?)$")
# Explicit identity: a comment the query's author wrote, or the session's
# query tag. Either outranks the projection's alias. `semantix:` is the
# project's former name; a tag written under it still names the metric.
_TAG = re.compile(r"(?:touchstone|semantix)\s*:\s*metric\s*=\s*([A-Za-z_][\w.]*)", re.I)
# `metric=net_revenue`, `metric: net_revenue`, and JSON `{"metric": "net_revenue"}`.
_QUERY_TAG = re.compile(r"(?:^|[\s,;{\"'])metric[\"']?\s*[=:]\s*[\"']?([A-Za-z_][\w.]*)", re.I)

SEV_VIOLATION_BASE = 60      # 60 + min(30, uses)
SEV_CANDIDATE_BASE = 40      # unnamed near miss: a hypothesis
SEV_CONTESTED = 55           # exact match to one side of an authority dispute
SEV_UNREGISTERED = 25
_SAMPLE = 10                 # attribution lists are capped in the output
# The run's reference instant for `compare_against: current` — one value per
# monitor run, set by monitor_history.
_NOW: contextvars.ContextVar[str] = contextvars.ContextVar("touchstone_monitor_now",
                                                           default="")
# The run an observation is being made from, when the caller asked for
# evidence records (monitor_history(export=True)); None otherwise.
_RUN: contextvars.ContextVar[dict | None] = contextvars.ContextVar("touchstone_monitor_run",
                                                                  default=None)
_NO_TIME = "<no start time>"
_BAD_TIME = "<unreadable start time>"


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
    # Outside the proof grammar, the optimizer's reading of the query: the
    # gap that says so. It is judged only for CONTEXT (`suspected`), always
    # inside a boundary — never conforming, never a violation.
    hypothesis: str | None = None
    core: SemanticCore | None = None
    components: dict[str, object] = field(default_factory=dict)
    shape_problem: str | None = None
    # Why the statement AS WRITTEN cannot be compared at all (a truncation
    # unit the engine does not accept): never an exact match.
    refusal: str | None = None
    evidence: frozenset | None = None   # views whose window covers the run
    explicit: tuple[str, str] | None = None   # (source, name)
    # Aliases the analyst WROTE: identity even when they look synthesized
    # (`sum_revenue`), which the regex alone dropped.
    explicit_aliases: set[str] = field(default_factory=set)
    view: str | None = None
    measure: str | None = None
    output: str | None = None
    view_outcome: Outcome | None = None
    # Each run this observation was made from (export only): the original
    # statement, the execution's fields, the session events before it.
    runs: list = field(default_factory=list)
    # Columns whose declared type makes literal comparison engine-specific
    # (a collation, fixed-length CHAR): no replaced/broadened decision on them.
    literal_undecided: frozenset = frozenset()
    # Declared types of the columns read (lower case) — to tell a narrowing
    # cast, kept as an observation, from one that cannot change a value.
    column_types: dict = field(default_factory=dict)
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
        run = _RUN.get()
        if run is not None:
            self.runs.append(run)


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
    export: bool = False,
) -> dict:
    """Findings from executed SQL, judged against authoritative definitions.

    `history` is SQL text (one execution per statement) or a HistoryIntake
    (one execution per row, deduplicated by statement id). `pilot` (a
    services.pilot.Pilot) supplies approved metric views, their definition
    windows, and any asserted context; without one, `context` applies to
    history rows only when `assert_context` says it covers every execution.

    `export` adds `comparisons`: touchstone-comparison/1 evidence records
    (touchstone.assess.record) for the advisory classifier — one per DISTINCT
    judgment (statement without comments, its session evidence, the governed
    metric and definition), with the executions it covers. A record exists
    for every candidate use — a statement that aggregates and names a
    governed metric or reads its source — whether or not the rules could
    read it and however often it ran: a measure in a CTE, an ad-hoc run and a
    shape outside the grammar are exactly the uses the rules leave unread.
    Findings, funnel and exit codes are the same either way.
    """
    from touchstone.services.pilot import precise_ts

    _NOW.set(precise_ts(utcnow()))
    scan = store.latest_scan()
    if scan is None:
        raise ValueError("no scans recorded — monitor needs a scan's "
                         "reference set to compare against; run `touchstone "
                         "scan` first")
    intake = history if isinstance(history, HistoryIntake) else from_sql_text(history)
    if pilot is not None:
        from touchstone.services.pilot import verify_against_scan

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
    computed_by_hash: dict[str, list[_RefDef]] = {}
    for ref in computed:
        computed_by_hash.setdefault(ref.semantic_hash, []).append(ref)
    # What each governed relation (a definition's source, or its metric view)
    # can compute: a statement reading one is a candidate use of these.
    by_source: dict[str, set[str]] = {}
    for auth in authorities:
        for rel in (auth.base_relation, auth.view):
            if rel:
                by_source.setdefault(rel.lower(), set()).add(auth.concept)
    candidates: list = []   # (run, concepts, relations, names, identity source)

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
              "without_time": 0, "bad_time": 0, "mixed_measures": 0,
              "outside_window": 0, "after_session_change": 0,
              "session_events": 0, "session_reconstructed": 0}
    windowed = pilot is not None and (pilot.window_from or pilot.window_to)
    executions_read = 0
    # Session events first, across the WHOLE history: a temp view created in
    # an earlier execution of the session — or of a session the export does
    # not identify — can shadow a name read later (services.session).
    log = intake.format == "sql"
    chunked = [statement_chunks(ex.statement_text, dialect) for ex in intake.executions]
    parsed_rows = [[tree for _, tree in chunks] for chunks in chunked]
    events = []
    for ri, (ex, chunks) in enumerate(zip(intake.executions, chunked)):
        ts_e, _ = _execution_time(ex)
        for si, (chunk, st) in enumerate(chunks):
            # An unparseable statement keeps its text: what it created is
            # evidence (exported only through session.exportable).
            e = classify(st, ri, si, ex.session_id, ts_e, text=chunk)
            if e is not None:
                events.append(e)
    counts["session_events"] = len(events)
    for ri, (ex, listed) in enumerate(zip(intake.executions, parsed_rows)):
        statements = [st for st in listed if st is not None]
        unparseable = len(listed) - len(statements)
        counts["statements"] += len(listed)
        counts["parsed"] += len(statements)
        counts["unparseable"] += unparseable
        executions_read += len(listed) if log else 1
        catalog = ex.catalog or asserted[0]
        schema = ex.schema or asserted[1]
        ts, time_state = _execution_time(ex)
        if windowed and ts is not None and not pilot.in_window(ts):
            # Outside the window the package says its export covers: not
            # judged, and counted, so a mismatched export shows itself.
            counts["outside_window"] += 1
            continue
        evidence = None
        if as_of:
            if time_state == "missing":
                counts["without_time"] += 1
                evidence = frozenset({_NO_TIME})
            elif time_state == "unreadable":
                counts["bad_time"] += 1
                evidence = frozenset({_BAD_TIME})
            else:
                # Every approved view whose window covers the run — readable
                # or not, so an unreadable applicable definition is known.
                evidence = frozenset(v.name for v in pilot.views if v.applies_at(ts))
        queries = [st for st in statements if isinstance(st, exp.Query)]
        # A query tag or asset belongs to the EXECUTION; it names one metric
        # only when the execution holds one query.
        row_identity = (_row_identity(ex, asset_map or {}) if len(queries) == 1 else None)
        for si, stmt in enumerate(listed):
            if stmt is None:
                continue
            if not isinstance(stmt, exp.Query):
                counts["non_query"] += 1
                continue
            (q_catalog, q_schema), session_gap, rebuilt = resolve(
                events, ri, si, ex.session_id, ts, log, written_relations(stmt), depth,
                (catalog, schema))
            if session_gap:
                counts["after_session_change"] += 1
            if rebuilt:
                counts["session_reconstructed"] += 1
            explicit = _statement_tag(stmt) or row_identity
            if export:
                _RUN.set(_run_record(ex, stmt, dialect, events, ri, si, q_catalog, q_schema,
                                     ts, log, depth))
            qualify_tables(stmt, q_catalog, q_schema, depth)
            if export:
                found = _candidate(stmt, explicit, by_name, by_source, history_registry)
                if found is not None:
                    candidates.append((_RUN.get(), *found))
            if measure_calls(stmt):
                if any(outside_measure_refs(a) for a in stmt.find_all(exp.AggFunc)):
                    # Raw aggregates beside governed ones: the measure
                    # references are judged (unverifiable), the raw ones are
                    # not read — say so rather than drop them silently.
                    counts["mixed_measures"] += 1
                _observe_view(stmt, ex, approved, evidence, observed, recurrence, dialect,
                              explicit, session_gap,
                              column_types=history_registry.column_types)
                continue
            outcome = _observe_core(stmt, ex, history_registry, dialect, evidence,
                                    explicit, observed, recurrence, session_gap)
            if outcome:
                counts[outcome] += 1

    # No positive verdict from a parser nobody qualified — this run's, or the
    # one that produced the reference cores. The scan withholds CERTIFIED
    # under the same condition; monitoring kept calling the same equality
    # "conforming".
    run_toolchain = toolchain_status()
    scan_toolchain = (json.loads(scan.get("stats_json") or "{}") or {}).get("toolchain") or {}
    unqualified = []
    if not run_toolchain.get("qualified"):
        unqualified.append(f"this run's parser, sqlglot {run_toolchain.get('sqlglot')}, "
                           f"is not a qualified version")
    if not scan_toolchain.get("qualified"):
        unqualified.append(
            f"the reference scan's parser, sqlglot {scan_toolchain.get('sqlglot', 'unrecorded')}, "
            f"is not a qualified version" if scan_toolchain else
            "the reference scan did not record a qualified parser version")

    recurring = [o for o in observed.values() if recurrence[o.base_key] >= min_runs]
    models = registry.known_models()
    label = pilot.comparison_label() if pilot is not None else (
        "compared against the latest scan's definitions; not verified as of "
        "execution time")
    judged: list[tuple[dict, _Observed]] = []
    funnel = _Funnel()
    for obs in sorted(recurring, key=lambda o: (-o.uses, o.key)):
        out = _judge(obs, by_hash, by_name, by_concept, computed_by_hash,
                     excluded, unreadable, models, dialect, as_of)
        if unqualified:
            out = [_withhold(f, unqualified) for f in out]
        # Under the policy in force (touchstone.conclusions): a match is an
        # experimental comparison, a violation a suspected difference.
        out = [effective_finding(f) for f in out]
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
    _RUN.set(None)
    findings = _aggregate(judged)
    findings.sort(key=lambda f: (-f["severity"], -f["uses"], f["kind"], f["concept"] or ""))
    bases = {o.base_key for o in observed.values()}
    recurring_bases = {b for b in bases if recurrence[b] >= min_runs}
    return {
        "scan_id": scan["scan_id"],
        "reference": ref_meta,
        "rule_set": RULE_SET,
        "excluded_semantics": list(EXCLUDED),
        # The policy in force, beside every result: `toolchain` withholding is
        # a separate, additional gate (an unqualified parser), not the policy.
        "policy": {"positive_verdicts": POSITIVE_VERDICTS,
                   "reason": SUSPENSION_REASON,
                   "negative_verdicts": "suspected — " + SUSPECTED_REASON},
        "toolchain": {"run": run_toolchain, "reference_scan": scan_toolchain or None,
                      "positive_verdicts_withheld": bool(unqualified)},
        "comparison": (pilot.comparison_label() if pilot is not None else
                       "latest scan's definitions"),
        "context": {"asserted": list(asserted) if any(asserted) else None,
                    "per_row": any(e.catalog or e.schema for e in intake.executions)},
        "history": {"source": intake.source, "format": intake.format,
                    "rows": intake.rows, "duplicate_executions": intake.duplicate_executions,
                    "empty_rows": intake.empty_rows,
                    "ignored_columns": intake.ignored_columns,
                    "executions": executions_read,
                    "executions_without_time": counts["without_time"],
                    "executions_with_unreadable_time": counts["bad_time"],
                    "executions_outside_window": counts["outside_window"],
                    "session_events": counts["session_events"],
                    "statements_after_session_change": counts["after_session_change"],
                    "statements_with_reconstructed_context": counts["session_reconstructed"]},
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
        **({"comparisons": _comparisons(judged, candidates, by_concept, pilot,
                                         history_registry)} if export else {}),
    }


# ------------------------------------------------------- evidence records

def _how_related(e, ri: int, ex: Execution, ts: str | None, log: bool) -> str:
    """What is NOT certain about an event that possibly precedes a statement:
    its session, its order, or both — in the monitor's own words."""
    if e.row == ri:
        return "earlier in this log; whether it ran in the same session is not recorded"
    before = e.ts is not None and ts is not None and e.ts < ts
    if e.session and ex.session_id:
        return ("in an earlier execution of this session" if before else
                "in another execution of this session; whether it ran before this "
                "statement is not certain")
    missing = ("neither session is recorded" if not e.session and not ex.session_id else
               "its session is not recorded" if not e.session else
               "this statement's session is not recorded")
    return (f"in an earlier execution; {missing}, so whether it ran in the same session is "
            f"not certain" if before else
            f"in another execution; {missing}, and its order relative to this statement is "
            f"not certain either")


_SESSION_EVENTS_MAX = 20
_SET_ASSIGN = re.compile(r"^\s*SET\s+([^=]+?)\s*=", re.IGNORECASE)


def _run_record(ex: Execution, stmt: exp.Expression, dialect: str, events, ri: int, si: int,
                catalog: str | None, schema: str | None, ts: str | None = None,
                log: bool = False, depth: int = 3) -> dict:
    """What one run contributes to an evidence record, captured BEFORE the
    tree is qualified: the statement as it ran and the session before it.
    "Before" is the rule name resolution itself uses (session.precedes): by
    time across executions, by position within one — never file row order
    alone. Each earlier statement is exported as it ran — through
    session.exportable, which keeps credentials out — not as a summary.

    Exported: the statement's own execution and its identified session, and
    from executions whose session is not recorded only what can change what
    its names read (session.affects) — every SET of a session-less history
    beside every query grew each record without bound. Repeated SETs of a
    key collapse to the last; past _SESSION_EVENTS_MAX, what decides which
    relation a name reads is kept before configuration, with a count of what
    was left out."""
    from touchstone.services.session import affects, exportable, precedes, written_relations

    written = written_relations(stmt)
    picked = []
    for e in sorted(events, key=lambda e: (e.ts or "", e.row, e.index)):
        # The same rule the monitor resolved names with: another identified
        # session never precedes; an unidentified one only possibly does.
        order = precedes(e, ri, si, ex.session_id, ts, log)
        if order is None:
            continue
        own = e.row == ri or (e.session and ex.session_id)
        if not own and not affects(e, written, depth):
            continue
        picked.append((e, order))
    # A session's configuration is the LAST value of each key: a driver's
    # repeated SETs collapse to one each.
    last_set: dict[str, int] = {}
    for n, (e, _) in enumerate(picked):
        m = _SET_ASSIGN.match(e.text or "")
        if m:
            last_set[m.group(1).strip().lower()] = n
    picked = [(e, o) for n, (e, o) in enumerate(picked)
              if not (_SET_ASSIGN.match(e.text or "")
                      and last_set[_SET_ASSIGN.match(e.text).group(1).strip().lower()] != n)]
    if len(picked) > _SESSION_EVENTS_MAX:
        # What decides which relation a name reads (USE, CREATE, DROP,
        # unparseable or unknown statements) is kept before configuration.
        decisive = [n for n, (e, _) in enumerate(picked) if not _SET_ASSIGN.match(e.text or "")]
        keep = set(decisive[-_SESSION_EVENTS_MAX:])
        for n in range(len(picked) - 1, -1, -1):
            if len(keep) >= _SESSION_EVENTS_MAX:
                break
            keep.add(n)
        dropped = len(picked) - len(keep)
        picked = [p for n, p in enumerate(picked) if n in keep]
    else:
        dropped = 0
    earlier = [(f"({dropped} earlier statement(s) that may affect this one are not listed)")
               ] if dropped else []
    for e, order in picked:
        text = exportable(e.text, dialect) if e.text else e.what
        earlier.append(text if order == "certain"
                       else f"{text} ({_how_related(e, ri, ex, ts, log)})")
    return {"execution": {"id": ex.statement_id, "start_time": ex.start_time,
                          "session_id": ex.session_id, "user": ex.executed_by,
                          "client": ex.client_application, "asset": ex.asset,
                          "assets": list(getattr(ex, "assets", None)
                                         or ([ex.asset] if ex.asset else [])),
                          "statement": stmt.meta.get("source_text") or stmt.sql(dialect=dialect),
                          "dialect": dialect},
            "session_evidence": {"context": {"catalog": catalog, "schema": schema}
                                 if (catalog or schema) else None,
                                 "events": earlier}}


_IDENTITY_SOURCES = {"alias", "tag", "asset", "output", "query_tag"}
# The same evidence reaches the export by two paths under two names (the
# rules' reading says "alias", candidate selection "output"): one kind each.
_IDENTITY_KIND = {"alias": "output", "output": "output", "tag": "tag", "query_tag": "tag",
                  "asset": "asset"}


def _record_schemas(relations, auth: Authority, registry: SchemaRegistry) -> dict:
    """Declared columns of every relation the use read — a metric view
    reads its source table, so that is listed too. A relation the catalog
    does not describe is listed with no columns (unknown), never dropped."""
    reads = {r.lower() for r in relations}
    if auth.view and auth.view.lower() in reads and auth.base_relation:
        reads.add(auth.base_relation.lower())
    return {r: dict(registry.column_types(r)) for r in sorted(reads)}


def _variation_policy(auth: Authority) -> dict:
    """The rules this monitor judged the use under, stated for a reader who
    must answer against THEM: declared dimensions, approved time groupings,
    required filters, and what the rule set does not cover."""
    dims = [d for d in auth.dimensions or [] if d.supported]
    approved = [f"DATE_TRUNC('{u}', {dim}) (approved for {auth.measure or auth.name})"
                for dim, units in sorted((auth.approved_groupings or {}).items())
                for u in sorted(units)]
    # What the rule set DECIDES — permitted, a difference, or undecided —
    # stated as such: an undecided boundary is not a requirement, and a
    # classifier answering against "must" would call drift what the rules
    # leave open.
    text = (f"Rule set {RULE_SET}. PERMITTED: filters on declared dimensions (in a filter, "
            f"a truncation of a declared date dimension is a time window on it), written as "
            f"a column compared with constants, IN, BETWEEN with bounds of one kind, or IS "
            f"NULL; groupings by declared dimensions exactly as declared, or by a time "
            f"grouping approved for this metric (a declared date dimension does not "
            f"authorize grouping by every truncation of it). A DIFFERENCE: a required filter "
            f"that is missing, weakened through OR, broadened or replaced; a filter on a "
            f"column that is not a declared dimension; a measure that reads other columns or "
            f"is scaled, offset or negated. UNDECIDED (neither permitted nor a difference): "
            f"a required filter not stated as a top-level condition; casts, rounding or "
            f"neutral arithmetic on the measure or its columns; predicates of any other "
            f"shape; groupings outside the declared and approved ones. Not covered at all: "
            f"{'; '.join(EXCLUDED)}.")
    if auth.dimensions is None:
        text += " This definition's dimensions are not modelled: no slice or grouping is " \
                "judged."
    return {"source": f"touchstone monitor ({RULE_SET})" + (" + pilot.yml approvals"
                                                         if approved else ""),
            "text": text,
            "permitted_filter_dimensions": [d.name for d in dims],
            "permitted_groupings": [f"{d.name} = {d.expr}" for d in dims] + approved}


def _candidate(stmt: exp.Expression, explicit, by_name: dict, by_source: dict,
               registry: SchemaRegistry):
    """(concepts, relations read, names, identity source) when a query may be a
    use of a governed metric; None when it is not a metric computation or
    touches nothing governed. Decided from what it NAMES — an explicit tag or
    asset, MEASURE(m), an output named for the metric (`orders_net_revenue`)
    — and else from what it READS (a definition's source or metric view),
    never from whether a measure could be extracted."""
    calls = measure_calls(stmt)
    if not calls and stmt.find(exp.AggFunc) is None:
        return None
    ctes = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    reads = sorted({registry.canonical_relation(table_fqname(t))
                    for t in stmt.find_all(exp.Table) if t.name.lower() not in ctes})
    named: set[str] = set()
    names: set[str] = set()
    if explicit:
        named |= by_name.get(normalize_name(explicit[1]), set())
        names.add(explicit[1])
    for call in calls:
        _, arg = measure_ref(call)
        if isinstance(arg, exp.Column) and normalize_name(arg.name) in by_name:
            named |= by_name[normalize_name(arg.name)]
            names.add(arg.name)
    for proj in getattr(stmt, "selects", []):
        alias = normalize_name(proj.alias_or_name or "")
        for metric, concepts in by_name.items():
            if alias and (alias == metric or alias.startswith(metric + "_")
                          or alias.endswith("_" + metric) or f"_{metric}_" in alias):
                named |= concepts
                names.add(proj.alias_or_name)
    concepts = named or set().union(*(by_source.get(r.lower(), set()) for r in reads))
    if not concepts:
        return None
    source = (explicit[0] if explicit and explicit[0] in _IDENTITY_SOURCES
              else "output" if names else "none")
    return sorted(concepts), reads, sorted(names), source


def _normalized_text(text: str) -> str:
    """The statement as it decides a judgment: comments removed, whitespace
    outside quotes collapsed. A BI tool's per-run context comment (user id,
    history slug) made every run of one dashboard tile a new statement — and
    a new paid model call. Quoted text is kept byte for byte."""
    out, i, n, quote = [], 0, len(text), None
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
        elif c in "'\"`":
            quote = c
            out.append(c)
            i += 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            if out and out[-1] != " ":
                out.append(" ")
        elif c.isspace():
            if out and out[-1] != " ":
                out.append(" ")
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out).strip()


def _comparisons(judged, candidates, by_concept, pilot, registry: SchemaRegistry) -> list:
    """One record per judgment key: the comment-free statement, its session
    evidence and the definition it is compared with. Every execution of the
    same judgment is counted in `occurrences` instead of becoming a record of
    its own — a dashboard tile run 500 times is one judgment. touchstone's
    reading is attached when the rules judged the use; a candidate the rules
    did not read (a nested measure, a single run, an unreadable shape) is
    exported without it, so the classifier still sees it."""
    from touchstone.assess.record import FORMAT, OBSERVATIONS_NOTE, validate

    specs = {(v.name, s.name.lower()): (v, s)
             for v in (pilot.views if pilot is not None else []) for s in v.specs}
    records: dict[str, dict] = {}
    runs_of: dict[str, dict] = {}

    def add(run, defs, relations, names, source, observations):
        ts = run["execution"]["start_time"]
        auth = next((a for a in defs if a.applies_at(ts)), defs[0])
        view, spec = specs.get((auth.view, (auth.measure or "").lower()), (None, None))
        # Only equivalent ASSESSMENT INPUTS share a judgment: the statement,
        # its session, the definition (and its window and snapshot) and how
        # the statement names its result. Where it ran is not an input — every
        # context is kept in `occurrences` and orders review without a call.
        key = hashlib.sha256(json.dumps([
            _normalized_text(run["execution"]["statement"]), run["execution"]["dialect"],
            run["session_evidence"], auth.label, auth.applies_from,
            view.sha256 if view is not None else None,
            [_IDENTITY_KIND.get(source, "none"), sorted({str(n).lower() for n in names})]],
            sort_keys=True).encode()).hexdigest()
        seen = runs_of.setdefault(key, {})
        # Every execution keeps its own consumption context: the same SQL run
        # in the editor and behind a dashboard is one judgment with two
        # contexts, never one context (the first) for both.
        seen.setdefault(run["execution"]["id"] or f"run:{id(run)}",
                        (ts, run["execution"].get("client"),
                         tuple(run["execution"].get("assets") or ())))
        record = records.get(key)
        if record is not None:
            if observations and "unverified_observations" not in record:
                record["unverified_observations"] = observations
            return
        snapshot = view.snapshot.read_text(encoding="utf-8") if view is not None else None
        records[key] = {
            "format": FORMAT,
            "record_id": "cmp_" + key[:16],
            "judgment_key": key,
            "execution": run["execution"],
            "claimed_metric": {"name": names[0] if names else None, "identity_evidence": [
                {"source": source if source in _IDENTITY_SOURCES else "none",
                 "value": n} for n in names] or [{"source": "none", "value": None}]},
            "definition": {
                "system": auth.system, "view": auth.view, "name": auth.measure or auth.name,
                "expr": (spec.expr if spec is not None and spec.expr
                         else auth.core.measure_expr),
                "source": auth.base_relation,
                "required_filters": (list(spec.filters) if spec is not None
                                     else list(auth.core.filters)),
                "dimensions": [f"{d.name} = {d.expr}" for d in (auth.dimensions or [])],
                "snapshot_text": snapshot,
                "snapshot_sha256": view.sha256 if view is not None else None,
                "applies": {"from": auth.applies_from, "to": auth.applies_to},
                "variation_policy": _variation_policy(auth),
                **({"approved_variations": list(view.approved_variations)}
                   if view is not None and view.approved_variations else {}),
                **({"prohibited_variations": list(view.prohibited_variations)}
                   if view is not None and view.prohibited_variations else {})},
            "schemas": _record_schemas(relations, auth, registry),
            "session_evidence": {**run["session_evidence"], "unresolved": sorted(
                r for r in relations if len(r.split(".")) < registry.estate_depth())},
            **({"unverified_observations": observations} if observations else {}),
        }

    for f, obs in judged:
        concept = f.get("concept")
        defs = by_concept.get(concept) if concept else None
        if not defs:
            continue      # nothing authoritative to compare with
        observations = {
            "note": OBSERVATIONS_NOTE, "finding_kind": f["kind"],
            "reasons": list(f.get("reasons") or []),
            "suspected": list(f.get("suspected") or []),
            "variation": f.get("variation"),
            "calculation_matches": f.get("calculation_matches"),
            "experimental": f.get("experimental")}
        for run in obs.runs:
            add(run, defs, obs.relations, f.get("names_used") or sorted(obs.names),
                f.get("identity_source"), observations)
    for run, concepts, relations, names, source in candidates:
        for concept in concepts:
            defs = by_concept.get(concept)
            if defs and run is not None:
                add(run, defs, relations, names, source, None)
    out = []
    for key, record in records.items():
        runs = runs_of[key]
        times = sorted(t for t, _, _ in runs.values() if t)
        record["occurrences"] = {
            "executions": len(runs),
            "execution_ids": [i for i in runs if not i.startswith("run:")][:_SAMPLE],
            "first_start_time": times[0] if times else None,
            "last_start_time": times[-1] if times else None,
            **_contexts(runs.values())}
        errors = validate(record)
        if errors:   # never hand a classifier a malformed record
            raise ValueError(f"comparison record does not validate: {errors}")
        out.append(record)
    return out


_CONTEXTS = 50


def _contexts(runs) -> dict:
    """The consumption contexts of one judgment: per (client, assets), how
    many executions and when. An execution the history attributes to nothing
    is counted as unattributed, never guessed. Evidence for ordering review,
    not a reading of anyone's intent."""
    groups: dict[tuple, list] = {}
    for ts, client, assets in runs:
        groups.setdefault((client, tuple(assets)), []).append(ts)
    contexts = []
    for (client, assets), stamps in groups.items():
        known = sorted(t for t in stamps if t)
        contexts.append({"client": client, "assets": list(assets), "executions": len(stamps),
                         "first_start_time": known[0] if known else None,
                         "last_start_time": known[-1] if known else None})
    contexts.sort(key=lambda c: (-c["executions"], c["client"] or "", c["assets"]))
    kinds: dict[str, int] = {}
    for c in contexts:
        for kind in {a.split(":", 1)[0] for a in c["assets"]} or {"unattributed"}:
            kinds[kind] = kinds.get(kind, 0) + c["executions"]
    out = {"contexts": contexts[:_CONTEXTS], "asset_kinds": kinds}
    if len(contexts) > _CONTEXTS:
        out["contexts_omitted"] = len(contexts) - _CONTEXTS
    return out


# ------------------------------------------------------------ observation

_ENGINE_SPECIFIC_TEXT = re.compile(r"\bCOLLATE\b|^\s*(N?CHAR|BPCHAR|CHARACTER)\b(?!\s+VARYING)",
                                   re.IGNORECASE)


def _typed_columns(registry: SchemaRegistry, reads) -> dict:
    """Declared column types for the narrowing-cast observations: by
    `relation.column` always, and by bare column name only where every
    relation read agrees — in a join, a same-named column of the other table
    decided the type of the one the query cast."""
    qualified: dict[str, str] = {}
    bare: dict[str, set] = {}
    for r in reads:
        for c, t in registry.column_types(r).items():
            qualified[f"{r}.{c}".lower()] = t
            bare.setdefault(c.lower(), set()).add(t)
    return {**{c: next(iter(ts)) for c, ts in bare.items() if len(ts) == 1}, **qualified}


def _literal_undecided(registry: SchemaRegistry, reads: list[str]) -> frozenset:
    """Columns of the relations read whose declared type makes `col = 'x'`
    engine-specific: a collation (UTF8_LCASE matches 'Completed') or
    fixed-length CHAR (blank-padded comparison). Round 17 found literals on
    such columns called replaced or broadened required filters."""
    return frozenset(c.lower() for r in reads
                     for c, t in registry.column_types(r).items()
                     if _ENGINE_SPECIFIC_TEXT.search(t))



def _observe_core(stmt: exp.Expression, ex: Execution, registry: SchemaRegistry,
                  dialect: str, evidence, explicit, observed: dict,
                  recurrence: dict, session_gap: str | None = None) -> str | None:
    """Shape A intake for one statement. Returns a disclosure counter name
    when the statement yields no measure to judge."""
    written = _written_aliases(stmt)
    sql_text = stmt.sql(dialect=dialect)
    stmt_id = hashlib.sha256(sql_text.encode()).hexdigest()[:12]
    # The tree carries the execution's context (short names qualified in
    # place), so it is judged as RENDERED — but only when the rendering is
    # faithful: the Spark generator hoists a derived table's WITH to the top,
    # changing what its names read (round 17). Unfaithful = unreadable.
    source = faithful_render(stmt, dialect)
    if source is None:
        return "unreadable"
    canon = canonicalize(source, registry, dialect)
    if canon.level == CanonLevel.FAILED or canon.ast is None:
        return "unreadable"
    # Inside the proof grammar, CTEs and derived tables were inlined by stated
    # rules, so the shape is judged on the proof form (one SELECT over one
    # table); outside it, on the statement as written.
    shape_problem = raw_shape_problem(
        canon.ast if canon.level == CanonLevel.SUPPORTED else stmt)
    # Judged on the statement AS WRITTEN: the canonical form re-renders the
    # parser's unit (DATE_TRUNC('W', d) as the WEEK grain), so a core equal
    # to the definition's would otherwise pass as an exact match.
    refusal = unaccepted_truncation(stmt, dialect) or \
        misread_literal(stmt.meta.get("source_text") or "", dialect)
    ast = canon.ast
    cte_names = {c.alias_or_name.lower() for c in ast.find_all(exp.CTE)}
    reads = sorted({
        registry.canonical_relation(table_fqname(t))
        for t in ast.find_all(exp.Table)
        if t.name.lower() not in cte_names and not t.name.startswith("_u_")})
    relation = reads[0] if reads else "(ad-hoc)"
    provenance = Provenance(system="query_history", object_id=f"stmt_{stmt_id}")
    fps = extract_fingerprints(canon, relation, provenance, dialect, registry)
    # Projections computed by functions the parser does not recognize are
    # measure uses it cannot read: counted, unverifiable, never dropped.
    for name, proj_sql, unknown in unrecognized_measures(stmt):
        gap = (f"computed by a function touchstone does not recognize "
               f"({', '.join(f'{u}()' for u in unknown)}) — not read")
        base_key = "|".join(["U", proj_sql, _evidence_key(evidence)])
        identity = _identity(name, None, written)
        key = f"{base_key}|{identity}"
        obs = observed.setdefault(key, _Observed(
            key=key, shape="A", base_key=base_key, identity=identity,
            semantic_hash=hashlib.sha256(proj_sql.encode()).hexdigest(),
            proof_grade=False, evidence=evidence))
        obs.uses += 1
        if name:
            obs.names.add(name)
        obs.relations.update(reads)
        obs.statements.add(stmt_id)
        obs.gaps.add(gap)
        obs.attribute(ex)
        obs.sample_sql = obs.sample_sql or sql_text
        recurrence[base_key] = recurrence.get(base_key, 0) + 1
    if not fps and unrecognized_measures(stmt):
        return None
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
        groups.setdefault((fp.core.hash(),
                           _identity(fp.concept_candidate, explicit, written)),
                          []).append(fp)
    counted: set[str] = set()
    for (core_hash, identity), group in groups.items():
        fp = group[0]
        gaps = set(fp.gaps)
        if reagg:
            gaps.add(f"reads the aggregate output of {', '.join(reagg)} — "
                     f"through-model comparison not made")
        if session_gap:
            gaps.add(session_gap)
        proof = fp.canon_level in PROOF_LEVELS and not gaps
        hypothesis = None
        if not proof and gaps and all(is_grammar_gap(g) for g in gaps):
            hypothesis = sorted(gaps)[0]
        base_key = "|".join(["A", core_hash, "proof" if proof else
                             "hypothesis" if hypothesis else "gapped",
                             shape_problem or "", refusal or "", _evidence_key(evidence)])
        key = f"{base_key}|{identity}"
        obs = observed.setdefault(key, _Observed(
            key=key, shape="A", base_key=base_key, identity=identity,
            semantic_hash=core_hash, proof_grade=proof, hypothesis=hypothesis,
            core=fp.core, components=core_components(fp.core),
            shape_problem=shape_problem, refusal=refusal, evidence=evidence,
            explicit=explicit, literal_undecided=_literal_undecided(registry, reads),
            column_types=_typed_columns(registry, reads)))
        obs.uses += 1
        obs.names.update(f.concept_candidate for f in group)
        obs.explicit_aliases |= {f.concept_candidate.lower() for f in group} & written
        obs.relations.update(reads)
        obs.statements.add(stmt_id)
        obs.gaps.update(gaps)
        obs.attribute(ex)
        if obs.sample_sql is None:
            # A statement refused as written is shown as written: its proof
            # form names the grain the parser read ('W' as WEEK).
            obs.sample_sql = (stmt.meta.get("source_text") if refusal else None) \
                or fp.canonical_sql
        if base_key not in counted:
            counted.add(base_key)
            recurrence[base_key] = recurrence.get(base_key, 0) + 1
    return None


_SESSION_GAP = ("relation identity depends on the session: an earlier statement in this "
                "execution (USE / SET / CREATE …) can change what its names resolve to")


def _written_aliases(stmt: exp.Expression) -> set[str]:
    return {p.alias.lower() for p in getattr(stmt, "selects", [])
            if isinstance(p, exp.Alias) and p.alias}


def _identity(alias: str | None, explicit: tuple[str, str] | None,
              written: set[str] = frozenset()) -> str:
    if explicit:
        return f"{explicit[0]}:{normalize_name(explicit[1])}"
    if alias and (alias.lower() in written or not _SYNTHESIZED.match(alias.lower())):
        return f"alias:{normalize_name(alias)}"
    return "none"


def _observe_view(stmt: exp.Expression, ex: Execution,
                  approved: dict[str, dict[str, Authority]], evidence,
                  observed: dict, recurrence: dict, dialect: str,
                  explicit: tuple[str, str] | None = None,
                  session_gap: str | None = None, column_types=None) -> None:
    """Shape B intake: one observation per measure-reference projection."""
    sql_text = stmt.sql(dialect=dialect)
    stmt_id = hashlib.sha256(sql_text.encode()).hexdigest()[:12]
    view = metric_view_relation(stmt)
    counted: set[str] = set()
    projections = check_metric_view(stmt, approved, dialect, column_types)
    # An explicit identity names one metric: applied only to a single use.
    explicit = explicit if len(projections) == 1 else None
    for proj in projections:
        if session_gap:
            # Which metric view a name reads is itself in question.
            proj.outcome = unverifiable("context_unresolved", session_gap, shape="B")
            proj.governed_reference = False
        signature = json.dumps(proj.outcome.as_dict(), sort_keys=True)
        base_key = "|".join(["B", view or "", proj.measure,
                             hashlib.sha256(signature.encode()).hexdigest()[:16],
                             _evidence_key(evidence), proj.expression or ""])
        identity = (_identity(None, explicit) if explicit else
                    f"output:{normalize_name(proj.output)}" if proj.output else "none")
        key = f"{base_key}|{identity}"
        obs = observed.setdefault(key, _Observed(
            key=key, shape="B", base_key=base_key, identity=identity, view=view,
            measure=proj.measure, output=proj.output, view_outcome=proj.outcome,
            expression=proj.expression, evidence=evidence, explicit=explicit,
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
            obs.sample_sql = stmt.meta.get("source_text") or sql_text


def _evidence_key(evidence) -> str:
    if evidence is None:
        return ""
    return "window:" + ",".join(sorted(evidence))


def _execution_time(ex: Execution) -> tuple[str | None, str]:
    """(instant at nanosecond width, "ok" | "missing" | "unreadable")."""
    from touchstone.services.pilot import precise_ts

    if not ex.start_time:
        return None, "missing"
    try:
        return precise_ts(ex.start_time), "ok"
    except ValueError:
        return None, "unreadable"


def _statement_tag(stmt: exp.Expression) -> tuple[str, str] | None:
    """A `touchstone: metric=` comment written on THIS statement — not anywhere
    in the execution text, where one statement's tag named every other."""
    for node in stmt.walk():
        for comment in node.comments or []:
            m = _TAG.search(comment)
            if m:
                return ("tag", m.group(1))
    return None


def _row_identity(ex: Execution, asset_map: dict[str, str]) -> tuple[str, str] | None:
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
    # normalized metric name -> approved definitions that could not be read,
    # with their windows: an unreadable definition can still be the one that
    # applies, and saying "no definition is evidenced" then is false.
    unreadable: dict[str, list[dict]] = {}
    now = _NOW.get()
    if pilot is not None:
        from touchstone.engine import build_claim

        for view in pilot.views:
            for spec in view.specs:
                claim = build_claim(spec, registry, dialect)
                if not claim.expressible or claim.core is None \
                        or claim.canon_level not in PROOF_LEVELS or claim.gaps:
                    why = (f"approved metric view {view.name}/{spec.name} "
                           f"({claim.error or claim.canon_level})")
                    excluded.setdefault(f"pilot:{view.name}/{spec.name}", why)
                    unreadable.setdefault(normalize_name(spec.name), []).append(
                        {"why": why, "view": view.name,
                         "current": view.applies_to is None or view.applies_to > now})
                    continue
                # Case variants (`Net_Revenue`, `net_revenue`) are one metric,
                # as the scan already treats them: one concept key.
                authorities.append(Authority(
                    concept=normalize_name(spec.name), system=spec.system, name=spec.name,
                    core=claim.core, dimensions=spec.dimensions, view=view.name,
                    measure=spec.name.lower(), applies_from=view.applies_from,
                    applies_to=view.applies_to, where=spec.path,
                    approved_groupings=view.approvals_for(spec.name)))
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
    # Comparing against CURRENT definitions, only those still applicable can
    # disagree with each other.
    current_mode = pilot is not None and pilot.compare_against == "current"
    pool = [a for a in authorities if a.current_at(now)] if current_mode else authorities
    meta = {"concepts": len(concepts), "definitions": len(authorities),
            "authority_systems": chosen,
            "non_authoritative_definitions": len(computed),
            "excluded_below_proof": len(excluded),
            # Definitions that differ but never apply at the same time are
            # VERSIONS of a metric, not a dispute about it.
            "contested_concepts": sorted(
                c for c in concepts
                if any(a.core.hash() != b.core.hash() and _overlap(a, b)
                       for a in pool if a.concept == c
                       for b in pool if b.concept == c)),
            # Concepts the scan reports as SUSPECTED divergences (the key keeps
            # its name for consumers; nothing in it is a verified contradiction).
            "scan_contradicted_concepts": sorted(contradicted),
            "contested_basis": "suspected — authoritative definitions that differ "
                               "under the experimental rules, not verified"}
    return authorities, computed, excluded, unreadable, meta


def _reference(store: Store, scan_id: str
               ) -> tuple[list[_RefDef], dict[str, str], set[str]]:
    """(proof-grade definitions, excluded hash -> description, concepts the
    scan found CONTRADICTED)."""
    contradicted = {v["concept_label"] for v in store.verdicts_for_scan(scan_id)
                    if v["verdict"] in ("CONTRADICTION", "SUSPECTED_DIVERGENCE")}
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

def _attributable(names: set[str], written: set[str] = frozenset()) -> list[str]:
    return sorted(n for n in names
                  if n.lower() in written or not _SYNTHESIZED.match(n.lower()))


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
        if outcome.suspected:
            f["suspected"] = [*f.get("suspected", []), *outcome.suspected]
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


def _eligible(obs: _Observed, defs: list[Authority], as_of: bool) -> list[Authority]:
    """The definitions that APPLY to this observation's executions. Every
    match candidate — exact, claimed, equivalent, contested — is drawn from
    these first: an expired definition matching the calculation is history,
    not evidence about the run (it chose a definition that ended October 1
    for an October 2 execution)."""
    if as_of:
        return [a for a in defs if _covered(obs, a)]
    # Against CURRENT definitions: those not ended by now (all of them,
    # outside a pilot or when no windows are given).
    now = _NOW.get()
    return [a for a in defs if a.current_at(now)]


def _applicable_unreadable(obs: _Observed, claim: str, unreadable: dict,
                           as_of: bool) -> str | None:
    """Why the approved definition of `claim` that applies here could not be
    read, if one applies."""
    for entry in unreadable.get(claim, []):
        applies = (obs.evidence is not None and entry["view"] in obs.evidence) if as_of \
            else entry["current"]
        if applies:
            return entry["why"]
    return None


def _no_evidence_detail(obs: _Observed, what: str) -> str:
    detail = f"no definition of {what} is evidenced for the execution time"
    if obs.evidence and _NO_TIME in obs.evidence:
        detail += " (the executions carry no start time)"
    elif obs.evidence and _BAD_TIME in obs.evidence:
        detail += " (the executions' start time could not be read)"
    return detail


def _overlap(a: Authority, b: Authority) -> bool:
    return ((a.applies_from is None or b.applies_to is None or a.applies_from < b.applies_to)
            and (b.applies_from is None or a.applies_to is None
                 or b.applies_from < a.applies_to))


def _contested_now(obs: _Observed, by_concept: dict, as_of: bool) -> set[str]:
    """Concepts whose definitions applicable to THIS observation disagree."""
    return {c for c, defs in by_concept.items()
            if len({a.core.hash() for a in _eligible(obs, defs, as_of)}) > 1}


def _judge(obs: _Observed, by_hash, by_name, by_concept, computed_by_hash,
           excluded, unreadable, models, dialect, as_of) -> list[dict]:
    contested = _contested_now(obs, by_concept, as_of)
    if obs.shape == "B":
        return _judge_view(obs, by_name, by_concept, contested, as_of, unreadable)
    names = _attributable(obs.names, obs.explicit_aliases)
    base = _base(obs, names)

    if not obs.proof_grade and obs.hypothesis is None:
        # An INCOMPLETE core omits constructs — equality or difference over it
        # proves nothing, so it anchors neither an accusation nor a
        # confirmation. Counted, described, left alone.
        reason = "; ".join(sorted(obs.gaps)) or "canonicalization below proof grade"
        code = ("context_unresolved" if "relation identity depends on the session" in reason
                else "through_model" if "through-model" in reason
                else "unrecognized_function" if "does not recognize" in reason
                else "below_proof_grade")
        out = unverifiable(code, reason)
        return [_finding(base, "unverifiable", 0, None, "unverifiable", "none",
                         f"recurring measure could not be read at proof grade "
                         f"({reason}) — no comparison made", out)]

    # What the query CLAIMS to compute: an explicit tag / query tag / asset
    # map first, else its own (analyst-written) aliases.
    claims = [normalize_name(n) for n in names]
    identity = "alias"
    if obs.explicit:
        source, name = obs.explicit
        if normalize_name(name) in by_name or normalize_name(name) in unreadable:
            claims, identity = [normalize_name(name)], source
        else:
            base = {**base, "identity_note": f"{source} names {name}, which is not "
                                             f"an authoritative metric"}
    named = {label for c in claims for label in by_name.get(c, ())}
    # A claimed metric whose APPLICABLE definition could not be read cannot
    # be judged — and the query is still named: it must not fall through to
    # "unnamed" and conform to whatever else its calculation matches.
    dark = {c: why for c in claims
            if (why := _applicable_unreadable(obs, c, unreadable, as_of))}
    also = [r.label for r in computed_by_hash.get(obs.semantic_hash, [])]
    if also:
        base = {**base, "also_matches": also}

    exact_all = by_hash.get(obs.semantic_hash) or []
    exact = _eligible(obs, exact_all, as_of)
    if exact_all and not exact:
        # The calculation matches only definitions that do not apply to these
        # executions: context, never a match. Judging continues against the
        # definitions that do.
        base = {**base, "calculation_matches": "; ".join(sorted(
            f"{a.label} as snapshotted — not evidenced for the execution time"
            for a in exact_all))}
    # Named after an approved metric whose definitions could not be read and
    # none of which applies here: still NAMED. Falling through to "unnamed"
    # let it conform to whatever else its calculation matched (sweep 2, #40).
    unread_named = sorted(c for c in claims if c in unreadable and c not in by_name
                          and c not in dark)
    if dark or unread_named:
        evidence = base
        if exact:
            evidence = {**base, "calculation_matches": sorted(
                exact, key=lambda a: (a.concept, a.label))[0].label}
        out = [_finding(evidence, "unverifiable", 0, c, "unverifiable", identity,
                        f"named {c}, whose applicable definition could not be read: "
                        f"{why}", unverifiable("below_proof_grade", why))
               for c, why in sorted(dark.items())]
        for c in unread_named:
            why = (f"the approved definitions of {c} could not be read, and none is "
                   f"evidenced for the execution time")
            out.append(_finding(evidence, "unverifiable", 0, c, "unverifiable", identity,
                                f"named {c}: {why}",
                                unverifiable("no_definition_evidence", why)))
        return out
    out: list[dict] = []
    if exact:
        # Several authoritative metrics can share one definition; the one the
        # query claims is the one it is judged as.
        claimed_exact = [a for a in exact if a.concept in named]
        auth = sorted(claimed_exact or exact, key=lambda a: (a.concept, a.label))[0]
        if named and auth.concept not in named:
            # The math is concept A's; the query CLAIMS concept B. B's verdict
            # is the finding, and the match to A is evidence on it — never a
            # separate conforming finding that could stand in for it.
            evidence = {**base, "calculation_matches": auth.label}
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

    # Nobody named it. Same math over the same physical columns as an
    # APPLICABLE authority: if the bounded rules say it IS that definition
    # sliced, it conforms (identity established by the definition, as for an
    # exact match); if they say it differs, it is a CANDIDATE — a hypothesis.
    def same_measure(a: Authority) -> bool:
        return (a.core.measure_expr == obs.core.measure_expr
                and a.core.source_columns == obs.core.source_columns)
    near = [a for defs in by_concept.values() for a in _eligible(obs, defs, as_of)
            if same_measure(a)]
    stale_near = [a for defs in by_concept.values() for a in defs
                  if same_measure(a) and a not in near]
    if stale_near and not near:
        base = {**base, "calculation_matches": "; ".join(sorted(
            f"near {a.label} as snapshotted — not evidenced for the execution time"
            for a in stale_near))}
    if near:
        judged = [(auth, check_raw(obs.core, auth, dialect, obs.shape_problem, models,
                                   obs.hypothesis, obs.literal_undecided, obs.column_types,
                                   refusal=obs.refusal))
                  for auth in near]
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
        auth, outcome = sorted(judged, key=lambda t: (t[0].concept, t[0].label))[0]
        return [_finding(base, "unverifiable", 0, auth.concept, "unverifiable",
                         "similarity",
                         f"near {auth.concept}, unnamed — {_reason_text(outcome)}",
                         outcome)]

    if obs.hypothesis is not None:
        # "Not defined by any authoritative definition" is a statement about
        # the query's meaning; a hypothesis reading cannot make it.
        return [_finding(base, "unverifiable", 0, None, "unverifiable", "none",
                         f"recurring measure outside the supported grammar — no "
                         f"comparison made ({obs.hypothesis})",
                         unverifiable("shape_unsupported", obs.hypothesis))]
    if obs.semantic_hash in excluded:
        return [_finding(base, "unverifiable", 0, None, "unverifiable", "none",
                         f"appears to match (experimental comparison) the "
                         f"{excluded[obs.semantic_hash]}, which is outside the supported "
                         f"grammar in the reference — no comparison made",
                         unverifiable("below_proof_grade", excluded[obs.semantic_hash]))]
    summary = ("recurring measure that no authoritative definition matched under the "
               "experimental comparison rules")
    if also:
        summary += (f" (appears to match, under the experimental rules, the {also[0]}, "
                    f"which is not declared authoritative)")
    return [_finding(base, "unregistered_recurring_measure", SEV_UNREGISTERED, None,
                     "not_applicable", "none", summary)]


def _covered(obs: _Observed, auth: Authority) -> bool:
    return auth.view is not None and obs.evidence is not None and auth.view in obs.evidence


def _judge_against(obs: _Observed, base: dict, concept: str, defs: list[Authority],
                   contested: set[str], models, dialect, as_of, identity) -> dict:
    """One identified concept: judge the observed core against each of its
    authoritative definitions and report the strongest true statement."""
    applicable = _eligible(obs, defs, as_of)
    if not applicable:
        outcome = unverifiable(
            "no_definition_evidence",
            _no_evidence_detail(obs, concept) if as_of else
            f"no current definition of {concept} (every recorded one ended before "
            f"{_NOW.get()})")
        return _finding(base, "unverifiable", 0, concept, "unverifiable", identity,
                        f"{concept}: {_reason_text(outcome)}", outcome)
    defs = applicable
    judged = [(a, check_raw(obs.core, a, dialect, obs.shape_problem, models,
                            obs.hypothesis, obs.literal_undecided, obs.column_types,
                            refusal=obs.refusal))
              for a in defs]
    conforming = [(a, o) for a, o in judged if o.status == "conforming"]
    if conforming:
        auth, outcome = sorted(conforming, key=lambda t: t[0].label)[0]
        return _conforming(obs, base, auth, outcome, contested, identity)
    violated = [(a, o) for a, o in judged if o.status == "violation"]
    if violated:
        auth, outcome = sorted(violated, key=lambda t: (len(t[1].reasons), t[0].label))[0]
        diff = _diff(auth.core, obs.components)
        matched = (f" — its calculation appears to equal the "
                   f"{base['calculation_matches']} under the experimental rules"
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
                        f"definitions of {auth.concept} appear to disagree with each "
                        f"other (a suspected divergence, not verified) — agreeing with "
                        f"one side of a suspected divergence is not consistency",
                        outcome)
    how = "matches the" if outcome.shape == "exact" else "conforms to the"
    return _finding(base, "conforming_use", 0, auth.concept, "conforming", identity,
                    f"{how} {auth.label}{_variation_text(outcome)}", outcome)


def _judge_view(obs: _Observed, by_name, by_concept, contested, as_of,
                unreadable: dict | None = None) -> list[dict]:
    """Shape B, in a fixed order — each step can only narrow what the next
    may conclude:

    1. definition evidence, for the REFERENCED metric and for any metric the
       use CLAIMS (an explicit tag / query tag / asset map, else its output
       name) — in as-of mode the windows covering the run, in current mode
       the definitions still applicable; an applicable definition that could
       not be read is said to be unreadable, not missing;
    2. the supported-SQL boundary (the conformance rules' verdict on the
       statement);
    3. the claimed metric — a bare reference is judged by DEFINITION
       (identical definitions under different names conform); a derived
       expression under another metric's name is not decided;
    4. the primary finding, chosen by the caller from these conclusions.

    A naming mismatch found before evidence or the boundary failed is kept
    as context (`suspected`), never promoted to a violation."""
    unreadable = unreadable or {}
    names = sorted(obs.names)
    base = _base(obs, names)
    outcome = obs.view_outcome
    auth = None
    for defs in by_concept.values():
        for a in defs:
            if a.view == obs.view and a.measure == obs.measure:
                auth = a
    concept = auth.concept if auth else None
    # What the use claims to be. Only for an approved, readable measure —
    # over an unknown view the identity of what was computed is unknown.
    claim_name, identity = obs.output, "alias"
    if obs.explicit:
        source, name = obs.explicit
        if normalize_name(name) in by_name or normalize_name(name) in unreadable:
            claim_name, identity = name, source
        else:
            base = {**base, "identity_note": f"{source} names {name}, which is not "
                                             f"an authoritative metric"}
    claim = normalize_name(claim_name) if claim_name else None
    claimed = (sorted(by_name.get(claim, set()) - {concept})
               if claim and concept is not None else [])
    dark_claim = (_applicable_unreadable(obs, claim, unreadable, as_of)
                  if claim and concept is not None and claim != concept else None)
    unread_claim = (claim is not None and concept is not None and claim != concept
                    and claim in unreadable and claim not in by_name and not dark_claim)
    if (dark_claim or unread_claim) and not claimed:
        claimed = [claim]
    suspected = (f"presented as {claim_name} but computed as {obs.expression} over "
                 f"{obs.view}") if claimed else None

    # 1. Definition evidence, referenced and claimed.
    missing = []
    if auth is not None and not _eligible(obs, [auth], as_of):
        missing.append(f"{obs.view}/{obs.measure}")
    missing += [label for label in claimed
                if label in by_concept and not _eligible(obs, by_concept[label], as_of)
                and not _applicable_unreadable(obs, label, unreadable, as_of)]
    if missing:
        outcome = unverifiable(
            "no_definition_evidence",
            _no_evidence_detail(obs, ", ".join(missing)) if as_of else
            f"no current definition of {', '.join(missing)}", shape="B")
    elif dark_claim:
        outcome = unverifiable("below_proof_grade",
                               f"the applicable definition of {claim} could not be read: "
                               f"{dark_claim}", shape="B")
    elif unread_claim:
        outcome = unverifiable("no_definition_evidence",
                               f"the approved definitions of {claim} could not be read, "
                               f"and none is evidenced for the execution time", shape="B")

    # 2. The supported-SQL boundary. `derived_expression` is not a boundary:
    # it is the projection's own formula, which step 3 judges.
    outside = outcome.status == "unverifiable" and set(outcome.codes) - {"derived_expression"}

    out: list[dict] = []
    if claimed:
        context = {**base, "suspected": [suspected]}
        for label in claimed:
            if outside:
                out.append(_finding(context, "unverifiable", 0, label, "unverifiable",
                                    identity,
                                    f"named {label}, {suspected} — not judged: "
                                    f"{_reason_text(outcome)}", outcome))
                continue
            if outcome.status != "conforming":
                # A derived expression under another metric's name may compute
                # that metric exactly (`MEASURE(gross) - MEASURE(refunds) AS
                # net_revenue`): whether it does is not decided here.
                held = unverifiable(
                    "implication_undecided",
                    f"presented as {claim_name}, computed as {obs.expression} from "
                    f"governed measures — whether that equals {label}'s definition is "
                    f"not decided", shape="B")
                out.append(_finding(context, "unverifiable", 0, label, "unverifiable",
                                    identity, f"named {label}: {_reason_text(held)}", held))
                continue
            # 3. The claimed metric. Different NAMES alone do not establish
            # different definitions: when the claimed metric's definition is
            # identical to the referenced one, a bare reference IS the claimed
            # metric (same view: same dimensions, so the slices hold too).
            same = [a for a in _eligible(obs, by_concept[label], as_of)
                    if a.core.hash() == auth.core.hash()]
            if same:
                eq = next((a for a in same if a.view == auth.view), None)
                variation = outcome.variation or {}
                if eq is not None or not (variation.get("slices") or variation.get("grain")):
                    out.append(_conforming(
                        obs, {**base, "calculation_matches":
                              f"{obs.view}/{obs.measure} — the same definition as {label}"},
                        eq or same[0], outcome, contested, identity))
                else:
                    held = unverifiable(
                        "dimension_unsupported",
                        f"{label}'s definition equals {obs.view}/{obs.measure}, but it is "
                        f"declared in another view — the slices were judged against "
                        f"{obs.view}'s dimensions, not {label}'s", shape="B")
                    out.append(_finding(base, "unverifiable", 0, label, "unverifiable",
                                        identity, f"named {label}: {_reason_text(held)}",
                                        held))
                continue
            other = Outcome("violation", [{
                "code": "presented_under_other_name", "detail": suspected}],
                outcome.variation, "B")
            out.append(_finding({**base, "calculation_matches":
                                 f"{obs.view}/{obs.measure} ({outcome.status})"},
                                "definition_violation", _violation_severity(obs.uses),
                                label, "violation", identity,
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


def _withhold(f: dict, unqualified: list[str]) -> dict:
    """A positive verdict under an unqualified parser becomes unverifiable;
    what it would have said is kept, as context."""
    if f.get("conformance") != "conforming":
        return f
    detail = "; ".join(unqualified) + " — equality under it is untested"
    # What it would have said is labelled for what it is: at most an
    # experimental match (round seventeen: the raw "conforming_use: conforms
    # to …" text reached the JSON unlabelled). A contested use keeps its
    # severity, as on the policy path, so the authority dispute is not lost.
    return {**f, "kind": "unverifiable", "conformance": "unverifiable",
            "severity": f["severity"] if f["kind"] == "contested_use" else 0,
            "reasons": [{"code": "toolchain_unqualified", "detail": detail}],
            "withheld_kind": f["kind"],
            "withheld": (f"experimental at most, not a conclusion — would have been "
                         f"{f['kind']}: {f['summary']}"),
            "summary": f"not judged: {detail}"}


# A use's headline bucket is its most severe finding.
_PRECEDENCE = {"definition_violation": 5, "suspected_difference": 5, "contested_use": 4,
               "candidate_alt_definition": 3, "unverifiable": 2,
               "unregistered_recurring_measure": 1, "conforming_use": 0}
# The summary is NOT attribution: it names the definition a verdict was
# reached against, and merging findings judged against different versions
# kept one version's label for all of them.
_ATTRIBUTION = frozenset({"uses", "distinct_statements", "names_used", "relations_read",
                          "sample_sql", "executed_by", "clients", "assets",
                          "execution_ids", "severity"})


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
            if f["kind"] in ("definition_violation", "suspected_difference"):
                f["severity"] = _violation_severity(f["uses"])
            elif f["kind"] == "candidate_alt_definition":
                f["severity"] = min(85, SEV_CANDIDATE_BASE + min(30, f["uses"]))
        out.append(f)
    return out


class _Funnel:
    """Every measure use lands in exactly one bucket; governed references
    are counted alongside, never instead. While positive verdicts are
    suspended there is no conforming bucket: an experimental match is one
    reason a use is unverifiable, and a violation is a suspected difference."""

    _KINDS = ("conforming_use", "contested_use", "definition_violation",
              "suspected_difference", "candidate_alt_definition",
              "unregistered_recurring_measure", "unverifiable")

    def __init__(self):
        self.uses = 0
        self.governed = 0
        self.by_kind = {k: 0 for k in self._KINDS}
        self.below_threshold = 0
        self.experimental_by_shape = {"exact": 0, "A": 0, "B": 0}
        self.experimental = {"conforming_use": 0, "contested_use": 0}
        self.unverifiable_by = {}

    def add(self, obs: _Observed, primary: dict) -> None:
        self.uses += obs.uses
        if obs.governed_reference:
            self.governed += obs.uses
        kind = primary["kind"]
        self.by_kind[kind] += obs.uses
        experimental = primary.get("experimental") or {}
        if experimental.get("kind") in self.experimental:
            self.experimental[experimental["kind"]] += obs.uses
        elif primary.get("withheld_kind") == "contested_use":
            # Withheld by the toolchain gate: still an authority dispute.
            self.experimental["contested_use"] += obs.uses
        if experimental.get("kind") == "conforming_use" and experimental.get("shape"):
            self.experimental_by_shape[experimental["shape"]] += obs.uses
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
        from touchstone.conclusions import POSITIVE_VERDICTS

        return {"measure_uses": self.uses, "governed_references": self.governed,
                "positive_verdicts": POSITIVE_VERDICTS,
                # Engine matches, reported as experimental comparisons only
                # (both are counted under `unverifiable`).
                "experimental_matches": self.experimental["conforming_use"],
                "experimental_matches_by_shape": dict(self.experimental_by_shape),
                "experimental_contested": self.experimental["contested_use"],
                "suspected_differences": (self.by_kind["suspected_difference"]
                                          + self.by_kind["definition_violation"]),
                "candidates": self.by_kind["candidate_alt_definition"],
                "unregistered": self.by_kind["unregistered_recurring_measure"],
                "unverifiable": self.by_kind["unverifiable"],
                "unverifiable_by_reason": dict(sorted(self.unverifiable_by.items())),
                "below_threshold": self.below_threshold}
