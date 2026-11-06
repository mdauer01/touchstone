"""`touchstone check`: executed SQL against approved metric definitions.

One pass, no scan and no store. A pilot package (pilot.yml) names the approved
metric-view snapshots, the query history and the window; this module finds
the statements that use a governed metric, sends each distinct one to the
advisory assessment (criteria v6 by default: one question, evidence, three
review states) and returns everything the report shows.

What the definitions say is all that is known: relationship facts and what a
dimension means come from the view's own text (its comments). Join forms other
than many-to-one (`cardinality: one_to_many`) are out of scope for this
release; their uses are `could_not_assess` without a model call.
"""

from __future__ import annotations

from pathlib import Path

from touchstone.adapters.catalog import load_catalog
from touchstone.adapters.loadutil import load_yaml
from touchstone.assess.criteria import Criteria
from touchstone.assess.runner import ADVISORY_NOTE, assess_records, priority
from touchstone.services.history import read_history
from touchstone.services.monitor import build_registry, monitor_history
from touchstone.services.pilot import load_pilot

UNSUPPORTED = "could_not_assess"


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


def export_records(pilot_dir: str | Path, dialect: str = "databricks") -> tuple:
    """(pilot, monitor result, the comparison records: one per distinct use of
    a governed metric) — what `check` assesses, without assessing it."""
    pilot = load_pilot(pilot_dir, dialect)
    intake = read_history(pilot.history)
    catalog = load_catalog(pilot.catalog) if pilot.catalog else {}
    context = {"default_database": pilot.context_catalog,
               "default_schema": pilot.context_schema}
    registry = build_registry(catalog, [], dialect, context)
    result = monitor_history(None, intake, registry, dialect, pilot=pilot, context=context,
                             export=True)
    return pilot, result, result.pop("comparisons")


def run_check(pilot_dir: str | Path, provider, criteria: Criteria, *,
              dialect: str = "databricks", cache: dict | None = None) -> dict:
    pilot, result, records = export_records(pilot_dir, dialect)

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

    failed = [v for v in verdicts if v.get("error") and v.get("error_kind") != "steering suspected"]
    held = [v for v in verdicts if v.get("error_kind") == "steering suspected"]
    return {"pilot": str(pilot_dir), "comparison": result.get("comparison"),
            "history": result.get("history"), "criteria_version": criteria.version,
            "provider": provider.name, "model": provider.model,
            "measures_performance": provider.measures_performance,
            "statements": len({r["execution"]["statement"] for r in records}),
            "uses": len(records), "records": records, "verdicts": verdicts, "stats": stats,
            "failed": [v["record_id"] for v in failed], "held": [v["record_id"] for v in held],
            "unsupported_views": unsupported}
