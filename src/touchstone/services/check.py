"""`touchstone check`: executed SQL against approved metric definitions.

One pass, no scan and no store. A pilot package (pilot.yml) names the approved
metric-view snapshots, the query history and the window; this module finds
the statements that use a governed metric, sends each distinct one to the
advisory assessment (criteria v7 by default, v6 with its rules for ratios and
safe division made explicit: one question, evidence, three review states) and returns everything the report shows.

The definitions come straight from the snapshots: every named measure of
every listed view is a definition, whatever the SQL rules can or cannot
represent. Which one a use is compared with is decided here, in code, and
never by falling back to the first one listed:

- a statement that reads a governed metric view uses THAT view's measures;
  one that reads a definition's source uses the definitions over it; else
  the name alone decides, and a name two views declare is ambiguous;
- under `compare_against: as_of_execution` the definition must have applied
  when the statement ran, so a run with no start time, or one outside the
  definition's window, is not compared with it; under `current` only the
  definitions still in force (no `applies.to`) are used.

A use that cannot be compared is reported as not assessed, with the reason,
as is a statement that cannot be parsed but mentions a governed view, source
or metric: an empty review queue must mean no drift found, never no coverage.

What the definitions say is all that is known: relationship facts and what a
dimension means come from the view's own text (its comments). Join forms other
than many-to-one (`cardinality: one_to_many`) are out of scope for this
release; their uses are `could_not_assess` without a model call.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from sqlglot import exp

from touchstone.adapters.catalog import load_catalog
from touchstone.adapters.loadutil import load_yaml
from touchstone.assess.criteria import Criteria
from touchstone.assess.record import FORMAT, validate
from touchstone.assess.runner import ADVISORY_NOTE, assess_records, priority
from touchstone.extraction.sqlutil import statement_chunks
from touchstone.naming import names_metric
from touchstone.services.conformance import measure_calls, measure_ref
from touchstone.services.history import qualify_tables, read_history
from touchstone.services.monitor import (
    _IDENTITY_KIND,
    _IDENTITY_SOURCES,
    _SAMPLE,
    _candidate,
    _contexts,
    _execution_time,
    _normalized_text,
    _row_identity,
    _run_record,
    _statement_tag,
    build_registry,
)
from touchstone.services.pilot import Pilot, PilotView, load_pilot
from touchstone.services.resolution import normalize_name
from touchstone.services.session import classify, resolve, written_relations

UNSUPPORTED = "could_not_assess"
NOT_ASSESSED = "coverage"     # error_kind of a use no definition could be compared with


@dataclass(frozen=True)
class Definition:
    """One named measure of one approved metric view, as its snapshot declares it."""

    view: PilotView
    name: str                   # lower case, as the judgment key has always spelled it
    expr: str
    source: str | None
    filters: tuple[str, ...]
    dimensions: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.view.name, self.name)

    @property
    def label(self) -> str:
        return f"declared definition uc_metrics:{self.view.name}/{self.name}"


def definitions(pilot: Pilot, registry) -> dict[tuple[str, str], Definition]:
    """Every named measure of every listed view, keyed by (view, measure).
    Compared against `current` definitions, a view whose window has an end is
    a past version and is left out."""
    out: dict[tuple[str, str], Definition] = {}
    for view in pilot.views:
        if pilot.compare_against == "current" and not view.current:
            continue
        raw = load_yaml(view.snapshot) or {}
        exprs = {str(m.get("name")).lower(): str(m.get("expr") or "")
                 for m in raw.get("measures") or [] if isinstance(m, dict) and m.get("name")}
        for spec in view.specs:
            source = (spec.model or "").replace("`", "").strip().lower() or None
            name = spec.name.lower()
            out[(view.name, name)] = Definition(
                view=view, name=name,
                expr=spec.expr or exprs.get(name, ""),
                source=registry.canonical_relation(source) if source else None,
                filters=tuple(spec.filters) or ((str(raw["filter"]),) if raw.get("filter")
                                                else ()),
                dimensions=tuple(f"{d.name} = {d.expr}" for d in spec.dimensions or []))
    return out


def unsupported_joins(snapshot: Path) -> list[str]:
    """The view's joins whose form this release does not assess: any
    cardinality other than many-to-one (the default), at any depth."""
    out: list[str] = []

    def walk(joins, depth=0):
        for join in joins or []:
            if not isinstance(join, dict):
                continue
            cardinality = str(join.get("cardinality") or "many_to_one").lower()
            if cardinality != "many_to_one":
                out.append(f"{join.get('name')}: {cardinality}")
            if depth < 8:
                walk(join.get("joins"), depth + 1)

    walk((load_yaml(snapshot) or {}).get("joins"))
    return out


def _narrow(keys, reads, defs) -> list[tuple[str, list]]:
    """(measure name, the definitions it can be compared with) for one use.
    MEASURE(revenue) on one view is that view's revenue, not every view's."""
    reads = {r.lower() for r in reads}
    for read in (lambda d: d.view.name in reads, lambda d: (d.source or "") in reads):
        narrowed = [k for k in keys if read(defs[k])]
        if narrowed:
            keys = narrowed
            break
    by_measure: dict[str, list] = {}
    for k in keys:
        by_measure.setdefault(normalize_name(defs[k].name), []).append(k)
    return sorted(by_measure.items())


def _window(d: Definition, short: bool = False) -> str:
    span = f"from {d.view.applies_from}" + (f" until {d.view.applies_to}" if d.view.applies_to
                                             else "")
    return span if short else f"the approved definition did not apply (it applies {span})"


def _presented(stmt, explicit, by_name) -> list[str] | None:
    """The names under which the statement's FINAL output presents a governed
    metric: an explicit tag or asset, and the outermost outputs whose names
    name one. A metric referenced only inside (a CTE's MEASURE(revenue) under
    an outer revenue_share) nominates the definition; it is not what the
    statement presents. None when the final output cannot be read (a star)."""
    out = set()
    if explicit and explicit[0] in ("tag", "query_tag", "asset") and explicit[1]:
        out.add(str(explicit[1]))
    for proj in getattr(stmt, "selects", None) or []:
        if isinstance(proj, exp.Star) or (isinstance(proj, exp.Column)
                                          and isinstance(proj.this, exp.Star)):
            return None
        name = proj.alias_or_name
        if not proj.alias and measure_calls(proj) and proj is measure_calls(proj)[0]:
            _, arg = measure_ref(proj)
            name = arg.name if isinstance(arg, exp.Column) else name
        if name and any(names_metric(name, metric) for metric in by_name):
            out.add(name)
    return sorted(out)


def _mentions(defs) -> re.Pattern | None:
    """Whole-word mentions of a governed view, source or metric name: an
    unparseable statement that mentions one may be a use nobody read."""
    words = set()
    for d in defs.values():
        words |= {d.name, d.view.name, d.view.name.rsplit(".", 1)[-1]}
        if d.source:
            words |= {d.source, d.source.rsplit(".", 1)[-1]}
    if not words:
        return None
    alternatives = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<![\w.])({alternatives})(?![\w])", re.IGNORECASE)


def export_records(pilot_dir: str | Path, dialect: str = "databricks") -> tuple:
    """(pilot, intake summary, the comparison records: one per distinct use of
    a governed metric, the uses that could not be compared) — what `check`
    assesses, without assessing it."""
    pilot = load_pilot(pilot_dir, dialect)
    intake = read_history(pilot.history)
    catalog = load_catalog(pilot.catalog) if pilot.catalog else {}
    context = {"default_database": pilot.context_catalog,
               "default_schema": pilot.context_schema}
    registry = build_registry(catalog, [], dialect, context)
    # The governed views' own names fix how many parts a full name has; the
    # optional column catalog supplies types, not whether short names resolve.
    registry.set_estate_depth([v.name for v in pilot.views])
    lookup = registry.without_context()     # history rows resolve by their own session
    depth = lookup.estate_depth()
    defs = definitions(pilot, lookup)
    by_name: dict[str, set] = {}
    by_source: dict[str, set] = {}
    for d in defs.values():
        by_name.setdefault(normalize_name(d.name), set()).add(d.key)
        for rel in (d.view.name, d.source):
            if rel:
                by_source.setdefault(rel.lower(), set()).add(d.key)
    mentions = _mentions(defs)
    # Compared against `current` definitions, a listed view that is no longer
    # current is left out; a statement that reads it is not compared with another.
    past = ({v.name for v in pilot.views if not v.current}
            if pilot.compare_against == "current" else set())
    asserted = ((pilot.context_catalog, pilot.context_schema) if pilot.context_asserted
                else (None, None))
    as_of = pilot.compare_against == "as_of_execution"
    log = intake.format == "sql"

    chunked = [statement_chunks(ex.statement_text, dialect) for ex in intake.executions]
    events = []
    for ri, (ex, chunks) in enumerate(zip(intake.executions, chunked)):
        ts_e, _ = _execution_time(ex)
        for si, (chunk, st) in enumerate(chunks):
            e = classify(st, ri, si, ex.session_id, ts_e, text=chunk)
            if e is not None:
                events.append(e)

    records: dict[str, dict] = {}
    runs_of: dict[str, dict] = {}
    gaps: dict[str, dict] = {}
    counts = {"statements": 0, "unparseable": 0, "unparseable_unrelated": 0,
              "outside_window": 0, "queries": 0, "candidates": 0}

    def where(ex) -> tuple:
        return (ex.start_time, ex.client_application,
                tuple(getattr(ex, "assets", None) or ([ex.asset] if ex.asset else ())))

    def gap(text, ex, ts, reason, metric, views):
        key = hashlib.sha256(json.dumps([_normalized_text(text), reason]).encode()).hexdigest()
        entry = gaps.setdefault(key, {"record_id": "gap_" + key[:16], "metric": metric,
                                      "view": ", ".join(views) or None, "statement": text,
                                      "label": None, "error": reason,
                                      "error_kind": NOT_ASSESSED, "runs": {}})
        entry["runs"].setdefault(ex.statement_id or f"run:{len(entry['runs'])}",
                                 where(ex))

    def add(run, d: Definition, reads, names, source, ex, ts, presented):
        key = hashlib.sha256(json.dumps([
            _normalized_text(run["execution"]["statement"]), run["execution"]["dialect"],
            run["session_evidence"], d.label, d.view.applies_from, d.view.sha256,
            [_IDENTITY_KIND.get(source, "none"), sorted({str(n).lower() for n in names})]],
            sort_keys=True).encode()).hexdigest()
        runs_of.setdefault(key, {}).setdefault(
            run["execution"]["id"] or f"run:{id(run)}", where(ex))
        if key in records:
            return
        evidence = source if source in _IDENTITY_SOURCES else "none"
        schemas = {r.lower() for r in reads}
        if d.view.name in schemas and d.source:
            schemas.add(d.source)
        records[key] = {
            "format": FORMAT,
            "record_id": "cmp_" + key[:16],
            "judgment_key": key,
            "execution": run["execution"],
            "claimed_metric": {"name": names[0] if names else None, "identity_evidence": [
                {"source": evidence, "value": n} for n in names]
                or [{"source": "none", "value": None}]},
            "comparison": pilot.comparison_label(),
            "presented": presented,
            "definition": {
                "system": "uc_metrics", "view": d.view.name, "name": d.name, "expr": d.expr,
                "source": d.source, "required_filters": list(d.filters),
                "dimensions": list(d.dimensions),
                "snapshot_text": d.view.snapshot.read_text(encoding="utf-8"),
                "snapshot_sha256": d.view.sha256,
                "applies": {"from": d.view.applies_from, "to": d.view.applies_to}},
            "schemas": {r: dict(lookup.column_types(r)) for r in sorted(schemas)},
            "session_evidence": {**run["session_evidence"], "unresolved": sorted(
                r for r in reads if len(r.split(".")) < lookup.estate_depth())},
        }

    for ri, (ex, chunks) in enumerate(zip(intake.executions, chunked)):
        ts, time_state = _execution_time(ex)
        if ts is not None and not pilot.in_window(ts):
            # Outside the window the package says its export covers: counted,
            # so a mismatched export shows itself.
            counts["outside_window"] += 1
            continue
        queries = [st for _, st in chunks if isinstance(st, exp.Query)]
        # A query tag or asset belongs to the EXECUTION; it names one metric
        # only when the execution holds one query.
        row_identity = (_row_identity(ex, pilot.asset_map) if len(queries) == 1 else None)
        for si, (text, stmt) in enumerate(chunks):
            counts["statements"] += 1
            if stmt is None:
                counts["unparseable"] += 1
                found = sorted({m.lower() for m in mentions.findall(text)}) if mentions else []
                if found:
                    gap(text, ex, ts, "the SQL could not be parsed, and it mentions "
                        + ", ".join(found), None, [])
                else:
                    counts["unparseable_unrelated"] += 1
                continue
            if not isinstance(stmt, exp.Query):
                continue
            counts["queries"] += 1
            (q_catalog, q_schema), _, _ = resolve(
                events, ri, si, ex.session_id, ts, log, written_relations(stmt), depth,
                (ex.catalog or asserted[0], ex.schema or asserted[1]))
            run = _run_record(ex, stmt, dialect, events, ri, si, q_catalog, q_schema,
                              ts, log, depth)
            qualify_tables(stmt, q_catalog, q_schema, depth)
            found = _candidate(stmt, _statement_tag(stmt) or row_identity, by_name, by_source,
                               lookup)
            if found is None:
                continue
            counts["candidates"] += 1
            keys, reads, names, source = found
            statement = run["execution"]["statement"]
            stale = sorted({r.lower() for r in reads} & past)
            if stale:
                gap(statement, ex, ts, f"it reads {', '.join(stale)}, an approved definition "
                    "that is no longer current", None, stale)
                continue
            presented = _presented(stmt, _statement_tag(stmt) or row_identity, by_name)
            # The order matters: the view the statement reads, then the time it ran,
            # then the name. A version that did not apply then is not a rival.
            for measure, options in _narrow(keys, reads, defs):
                views = sorted(defs[k].view.name for k in options)
                if as_of:
                    if ts is None:
                        gap(statement, ex, ts, f"its start time is {time_state}, so which "
                            f"definition applied when it ran is not known", measure, views)
                        continue
                    applying = [k for k in options if defs[k].view.applies_at(ts)]
                    if not applying:
                        gap(statement, ex, ts, f"it ran at {ts}, when "
                            + (_window(defs[options[0]]) if len(options) == 1 else
                               f"no approved definition named {measure} applied ("
                               + "; ".join(f"{defs[k].view.name}: " + _window(defs[k], short=True)
                                           for k in options) + ")"),
                            measure, views)
                        continue
                    options = applying
                    views = sorted(defs[k].view.name for k in options)
                if len(options) > 1:
                    gap(statement, ex, ts, f"more than one approved definition is named "
                        f"{measure} ({', '.join(views)}) and the statement does not say "
                        f"which it uses", measure, views)
                    continue
                add(run, defs[options[0]], reads, names, source, ex, ts, presented)

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
    not_assessed = []
    for entry in gaps.values():
        runs = entry.pop("runs")
        times = sorted(t for t, _, _ in runs.values() if t)
        occ = _contexts(runs.values())
        not_assessed.append({**entry, "executions": len(runs),
                             "first_start_time": times[0] if times else None,
                             "last_start_time": times[-1] if times else None,
                             "contexts": occ["contexts"], "advisory": ADVISORY_NOTE,
                             "priority": priority({"executions": len(runs), **occ})})
    summary = {"source": intake.source, "format": intake.format, "rows": intake.rows,
               "duplicate_executions": intake.duplicate_executions,
               "executions_outside_window": counts["outside_window"],
               "statements": counts["statements"], "queries": counts["queries"],
               "unparseable": counts["unparseable"],
               "unparseable_unrelated": counts["unparseable_unrelated"],
               "candidate_statements": counts["candidates"],
               "definitions": len(defs)}
    return pilot, summary, out, not_assessed


def run_check(pilot_dir: str | Path, provider, criteria: Criteria, *,
              dialect: str = "databricks", cache: dict | None = None) -> dict:
    if criteria.mode != "single":
        raise ValueError(f"check reports three review states, and criteria "
                         f"v{criteria.version} does not produce them; use criteria 7 or 6, "
                         f"or `touchstone assess run` for older criteria")
    pilot, history, records, not_assessed = export_records(pilot_dir, dialect)

    unsupported = {v.name: why for v in pilot.views if (why := unsupported_joins(v.snapshot))}
    supported = [r for r in records if (r["definition"].get("view") or "") not in unsupported]
    verdicts, stats = assess_records(provider, supported, criteria, cache=cache)
    for record in records:
        why = unsupported.get(record["definition"].get("view") or "")
        if why is None:
            continue
        occ = record.get("occurrences") or {}
        verdicts.append({
            "record_id": record["record_id"], "metric": record["definition"]["name"],
            "view": record["definition"].get("view"), "executions": occ.get("executions", 1),
            "first_start_time": occ.get("first_start_time"),
            "last_start_time": occ.get("last_start_time"),
            "statement": record["execution"]["statement"], "label": UNSUPPORTED,
            "treatment": criteria.treatment_of(UNSUPPORTED), "answers": {},
            "evidence": {"explanation": "the definition declares a join form this release "
                                        "does not assess: " + "; ".join(why)},
            "error": None, "from_cache": False, "advisory": ADVISORY_NOTE,
            "contexts": occ.get("contexts") or [], "priority": priority(occ)})
    verdicts.extend(not_assessed)

    failed = [v for v in verdicts if v.get("error")
              and v.get("error_kind") not in ("steering suspected", NOT_ASSESSED)]
    held = [v for v in verdicts if v.get("error_kind") == "steering suspected"]
    return {"pilot": str(pilot_dir), "comparison": pilot.comparison_label(),
            "history": history, "criteria_version": criteria.version,
            "provider": provider.name, "model": provider.model,
            "measures_performance": provider.measures_performance,
            "statements": len({r["execution"]["statement"] for r in records}),
            "uses": len(records), "records": records, "verdicts": verdicts, "stats": stats,
            "failed": [v["record_id"] for v in failed], "held": [v["record_id"] for v in held],
            "not_assessed": [v["record_id"] for v in not_assessed],
            "unsupported_views": unsupported}
