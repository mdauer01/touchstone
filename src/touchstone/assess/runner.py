"""Evaluate an advisory provider against the labeled metric uses (model-authored and
model-reviewed labels — see the dataset README; no human review yet).

Reports, overall and per query family:

* for the items labeled drift: how many were FLAGGED, how many were labeled
  permitted (incorrect reassurance — the costly error), how many were left
  UNDECIDED by the model, and how many calls FAILED. The four always sum to
  the class: a provider that abstains on every drift reassures no one
  wrongly and still catches nothing, and an outage is not an abstention;
* for the permitted items: confirmed, flagged as drift (false alerts),
  undecided, failed; likewise for the insufficient-evidence items;
* abstention and agreement over the items that were answered;
* calibration — a reliability table and ECE over the answered items that
  carry a confidence in [0, 1], with how many that is and where the
  confidence comes from (a model's self-report is not a probability);
* errors by kind; cost and latency only as the provider reports them.

Two baselines sit beside the provider: touchstone's own observations alone
(a fixed mapping from the monitor's finding kind) and a plain-prompt model
(no criteria) — not implemented yet, and reported as not run. When the
records carry touchstone's observations, the provider runs twice — with and
without them — so the ablation shows whether those unverified observations
help or mislead.

A run on a provider that does not measure performance (`fake`) says so in
the report, in those words: it exercised the machinery and measured nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import sqlglot
import yaml
from sqlglot import exp

from touchstone.adapters.loadutil import load_yaml, parse_yaml
from touchstone.assess import ADVISORY_LABELS
from touchstone.assess.criteria import Criteria, load
from touchstone.assess.record import FORMAT, OBSERVATIONS_NOTE, validate, without_observations

DATASET_DIR = Path(__file__).resolve().parent.parent / "benchmark" / "metric_uses"
LABELS = ("permitted_variation", "formula_drift", "population_drift", "grain_drift",
          "source_drift", "identity_mismatch", "insufficient_evidence")
DIALECT = "databricks"
UNMEASURED = ("classification performance is UNMEASURED: this provider does not measure it "
              "(the fake backend exercises the machinery only)")


def gold_advisory(label: str) -> str:
    if label == "permitted_variation":
        return "likely_permitted"
    if label == "insufficient_evidence":
        return "insufficient_evidence"
    return "suspected_drift"


# ------------------------------------------------------------------ dataset

def load_dataset(path: str | Path | None = None) -> dict[str, Any]:
    """The definitions, every family's items, and the adjudication record.
    Refuses a malformed dataset rather than scoring it."""
    # The strict loader: a key written twice (a second `label:`) is refused,
    # never half-read; instants stay the text written.
    root = Path(path) if path else DATASET_DIR
    definitions = load_yaml(root / "definitions.yaml", timestamps_as_text=True)
    view = parse_yaml(definitions["view"], "definitions.yaml: view")
    measures = {m["name"]: m["expr"] for m in view["measures"]}
    families, seen = [], set()
    for f in sorted(root.glob("*.yaml")):
        if f.name in ("definitions.yaml", "adjudication.yaml", "blind_labels.yaml"):
            continue
        data = load_yaml(f)
        if data.get("split") not in ("dev", "test"):
            raise ValueError(f"{f.name}: split must be dev or test (split is by family)")
        for item in data["items"]:
            if item["id"] in seen:
                raise ValueError(f"{f.name}: duplicate item id {item['id']}")
            seen.add(item["id"])
            if item["label"] not in LABELS:
                raise ValueError(f"{item['id']}: unknown label {item['label']}")
            if item["metric"] not in measures:
                raise ValueError(f"{item['id']}: {item['metric']} is not a defined measure")
            if not str(item.get("rationale") or "").strip():
                raise ValueError(f"{item['id']}: every label needs its rationale")
        families.append({"family": data["family"], "split": data["split"],
                         "items": data["items"]})
    applies = definitions.get("applies") or {}
    if applies.get("from") or applies.get("to"):
        try:
            at = _instant(execution_time({"definitions": definitions}))
            start = _instant(applies["from"]) if applies.get("from") else None
            end = _instant(applies["to"]) if applies.get("to") else None
        except ValueError as e:
            raise ValueError(f"definitions.yaml: applies: {e}") from None
        if (start is not None and at < start) or (end is not None and at >= end):
            raise ValueError("definitions.yaml: the validity window cannot contain the "
                             "benchmark's execution time (is it empty or inverted?)")
    adjudication_file = root / "adjudication.yaml"
    adjudication = load_yaml(adjudication_file) if adjudication_file.exists() else {}
    return {"definitions": definitions, "view": view, "measures": measures,
            "families": families, "adjudication": adjudication}


def items(dataset: dict, split: str = "all") -> list[tuple[str, dict]]:
    return [(f["family"], item) for f in dataset["families"]
            if split == "all" or f["split"] == split for item in f["items"]]


# ------------------------------------------------------------------ records

def _output_names(sql: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:  # noqa: BLE001
        return []
    select = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    return [p.alias for p in (select.expressions if select else [])
            if isinstance(p, exp.Alias) and p.alias]


def _parse_all(sql: str) -> list[exp.Expression]:
    try:
        return [t for t in sqlglot.parse(sql, read=DIALECT) if t is not None]
    except Exception:  # noqa: BLE001
        return []


def session_reads(dataset: dict, item: dict) -> tuple[list[str], dict | None]:
    """(the relations the item's statement READS, the catalog/schema context
    it ran in) — resolved through the session statements before it the way
    the engine resolves them, with the monitor's own rule
    (services/session.classify):

    * `USE CATALOG dev` makes `finance.orders` read dev.finance.orders and
      leaves the schema unknown; names compare without case;
    * a SET (`SET CATALOG dev` included), an unrecognized or unparseable
      statement makes the context unknown — every short name unresolved —
      exactly where the monitor calls a name's identity session-dependent;
    * a temp view (or CACHE TABLE … AS) shadows a one-part name and reads
      what its body read when it was created; a global temp view lives in
      `global_temp`; a view created in the session is found by the name it
      resolved to when created, not by its short name after a catalog switch;
    * USE, DROP and ALTER read nothing.

    A name the context does not fix stays as written (unresolved), never
    guessed."""
    from touchstone.extraction.sqlutil import table_fqname
    from touchstone.services.session import classify

    catalog = schema = None
    temp: dict[str, set[str]] = {}          # by the name it is read as
    created: dict[str, set[str]] = {}       # persistent, by its resolved name

    def resolve(name: str) -> str:
        parts = name.split(".")
        if parts[0] == "global_temp":
            return name
        if len(parts) == 2 and catalog:
            return f"{catalog}.{name}"
        if len(parts) == 1 and catalog and schema:
            return f"{catalog}.{schema}.{name}"
        return name

    def reads_of(tree: exp.Expression) -> set[str]:
        out: set[str] = set()
        for t in tree.find_all(exp.Table):
            if not t.name:
                continue
            name = table_fqname(t).lower()
            if "." not in name and _names_a_cte(t, name):
                continue
            if name in temp:
                out |= temp[name]
            elif resolve(name) in created:
                out |= created[resolve(name)]
            else:
                out.add(resolve(name))
        return out

    for text in item.get("session") or []:
        trees = _parse_all(text)
        if not trees:
            catalog = schema = None              # unparseable: anything may have changed
        for tree in trees:
            target = tree.this.this if isinstance(tree.this, exp.Schema) else tree.this
            if isinstance(tree, exp.Use):
                event = classify(tree, 0, 0, None, None)
                if event is None:
                    continue                     # USE WAREHOUSE / ROLE: no namespace
                if event.use is None:
                    catalog = schema = None      # a USE this rule cannot read
                    continue
                use_catalog, use_schema, schema_known = event.use
                catalog = use_catalog.lower() if use_catalog else catalog
                schema = use_schema.lower() if schema_known and use_schema else None
            elif isinstance(tree, (exp.Create, exp.Cache)) and isinstance(target, exp.Table) \
                    and tree.args.get("expression") is not None:
                body = reads_of(tree.args["expression"])
                name = table_fqname(target).lower()
                props = tree.args.get("properties")
                if props is not None and props.find(exp.GlobalProperty) is not None:
                    temp[f"global_temp.{name.split('.')[-1]}"] = body
                elif isinstance(tree, exp.Cache) or (
                        props is not None and props.find(exp.TemporaryProperty) is not None):
                    temp[name] = body
                else:
                    created[resolve(name)] = body
            elif isinstance(tree, exp.Drop) and isinstance(target, exp.Table):
                name = table_fqname(target).lower()
                temp.pop(name, None)
                created.pop(resolve(name), None)
            elif isinstance(tree, exp.Alter) and isinstance(target, exp.Table):
                old = table_fqname(target).lower()
                for rename in tree.find_all(exp.AlterRename):
                    if old in temp and isinstance(rename.this, exp.Table):
                        temp[table_fqname(rename.this).lower()] = temp.pop(old)
            else:
                event = classify(tree, 0, 0, None, None)
                if event is not None and event.names is None:
                    catalog = schema = None      # SET and the like: every short name
    reads: set[str] = set()
    for tree in _parse_all(item["sql"]):
        reads |= reads_of(tree.args["expression"] if isinstance(tree, exp.Create)
                          and tree.args.get("expression") is not None else tree)
    context = {"catalog": catalog, "schema": schema} if (catalog or schema) else None
    return sorted(reads), context


def _session_events(item: dict) -> list[str]:
    """The item's session statements as the monitor lists them: one per
    statement, only those that can affect the session (services.session
    .classify — a SHOW or an INSERT cannot), through session.exportable."""
    from touchstone.services.relationships import statement_chunks
    from touchstone.services.session import classify, exportable

    out = []
    for text in item.get("session") or []:
        for chunk, tree in statement_chunks(text, DIALECT):
            if classify(tree, 0, 0, None, None, text=chunk) is not None:
                out.append(exportable(chunk, DIALECT))
    return out


def _names_a_cte(table: exp.Table, name: str) -> bool:
    """Whether a one-part `name` read by `table` is a CTE in scope. Inside a
    non-recursive CTE's own body, its name is the OUTER relation
    (`WITH o AS (SELECT * FROM o)` reads the temp view o)."""
    ancestors = []
    node = table.parent
    while node is not None:
        ancestors.append(node)
        node = node.parent
    for node in ancestors:
        # sqlglot has spelled the arg `with` and (30.x) `with_`; reading one
        # alone recognized no CTE at all.
        with_ = (node.args.get("with_") or node.args.get("with")) \
            if isinstance(node, exp.Expression) else None
        if with_ is None:
            continue
        for cte in with_.expressions:
            if cte.alias_or_name.lower() != name:
                continue
            if any(a is cte for a in ancestors) and not with_.args.get("recursive"):
                continue
            return True
    return False


def _schemas(dataset: dict, item: dict) -> dict:
    """The declared columns of every relation the execution reads (resolved
    through its session, and a metric view's source table). A relation the
    catalog does not describe, or a name the session does not resolve, is
    listed with no columns: unknown, never dropped."""
    catalog = dataset["definitions"]["catalog"]
    view = dataset["definitions"]["view_name"].lower()
    reads = set(session_reads(dataset, item)[0])
    if view in reads:
        reads.add(dataset["view"]["source"].lower())
    return {r: dict(catalog.get(r) or {}) for r in sorted(reads)}


def _instant(text: str):
    from datetime import datetime

    try:
        at = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"{text!r} is not an ISO-8601 instant") from e
    if at.tzinfo is None:
        raise ValueError(f"{text!r} has no time zone (write Z or an offset): an instant "
                         f"without one cannot be placed in the validity window")
    return at


_DEFAULT_START = "2026-09-01T00:00:00Z"


def execution_time(dataset: dict) -> str:
    """When every item 'ran': twelve hours into the definition's validity
    window — its midpoint for a window shorter than a day, twelve hours
    before its end when it has no start. Inside the window by construction;
    load_dataset refuses a window it cannot be placed in."""
    from datetime import timedelta

    applies = dataset["definitions"].get("applies") or {}
    start = _instant(applies["from"]) if applies.get("from") else None
    end = _instant(applies["to"]) if applies.get("to") else None
    if start is None:
        at = end - timedelta(hours=12) if end is not None else \
            _instant(_DEFAULT_START) + timedelta(hours=12)
    else:
        at = start + timedelta(hours=12)
        if end is not None and at >= end:
            at = start + (end - start) / 2
    from datetime import timezone

    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def execution_context(dataset: dict, item: dict) -> dict:
    """The execution an item stands for — ONE definition shared by the model's
    records and the monitor baseline, so both see the same time (inside the
    definition's validity window) and the same session. The session id and
    user are bookkeeping for the monitor; prompts do not carry them."""
    return {"id": item["id"], "start_time": execution_time(dataset),
            "session_id": item["id"], "user": "benchmark", "client": None, "asset": None}


def variation_policy(dataset: dict) -> dict:
    view = dataset["view"]
    return {"source": "dataset policy (benchmark/metric_uses/definitions.yaml)",
            "text": dataset["definitions"]["policy"],
            "permitted_filter_dimensions": [d["name"] for d in view.get("dimensions", [])],
            "permitted_groupings": [f"{d['name']} = {d['expr']}"
                                    for d in view.get("dimensions", [])]}


def build_record(dataset: dict, item: dict, observations: dict | None = None) -> dict:
    """One touchstone-comparison/1 record for a dataset item. The labels are
    never part of the record."""
    d = dataset["definitions"]
    view = dataset["view"]
    record = {
        "format": FORMAT,
        "record_id": f"bench_{item['id']}",
        "execution": {**execution_context(dataset, item), "statement": item["sql"],
                      "dialect": DIALECT},
        "claimed_metric": {"name": None, "identity_evidence": [
            {"source": "output", "value": n} for n in _output_names(item["sql"])]},
        "definition": {
            "system": d["system"], "view": d["view_name"], "name": item["metric"],
            "expr": dataset["measures"][item["metric"]], "source": view["source"],
            "required_filters": [view["filter"]] if view.get("filter") else [],
            "dimensions": [f"{x['name']} = {x['expr']}" for x in view.get("dimensions", [])],
            "snapshot_text": d["view"],
            "snapshot_sha256": hashlib.sha256(d["view"].encode()).hexdigest(),
            "applies": d.get("applies"),
            "stated_facts": list(d.get("stated_facts") or []),
            "variation_policy": variation_policy(dataset)},
        "schemas": _schemas(dataset, item),
        "session_evidence": {"context": session_reads(dataset, item)[1],
                             "events": _session_events(item),
                             "unresolved": [r for r in session_reads(dataset, item)[0]
                                            if len(r.split(".")) < 3]},
    }
    if observations is not None:
        record["unverified_observations"] = {"note": OBSERVATIONS_NOTE, **observations}
    errors = validate(record)
    if errors:
        raise ValueError(f"{item['id']}: record does not validate: {errors}")
    return record


def observations_from_monitor(dataset: dict, split: str = "all") -> dict[str, dict]:
    """touchstone's own reading of every item, through the REAL monitor: a pilot
    package is synthesized from the dataset (the definitions' metric view and
    catalog, one execution per item with its session statements), scanned,
    and monitored with `export=True`. Returns item id -> the record's
    unverified observations. An item the monitor produced no record for maps
    to an empty reading (no finding)."""
    import tempfile

    from touchstone.adapters.catalog import load_catalog
    from touchstone.adapters.sql_dir import load_sql_dir
    from touchstone.engine import run_scan
    from touchstone.services.history import read_history
    from touchstone.services.monitor import build_registry, monitor_history
    from touchstone.services.pilot import load_pilot

    d = dataset["definitions"]
    chosen = items(dataset, split)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "pilot"
        (root / "views").mkdir(parents=True)
        snap = root / "views" / "view.yaml"
        snap.write_text(d["view"], encoding="utf-8")
        nested: dict = {}
        for relation, cols in d["catalog"].items():
            cat, sch, tab = relation.split(".")
            nested.setdefault(cat, {}).setdefault(sch, {})[tab] = dict(cols)
        (root / "catalog.yml").write_text(yaml.safe_dump(nested), encoding="utf-8")
        rows = []
        for _, item in chosen:
            ctx = execution_context(dataset, item)      # the model's records' context
            rows.append({"statement_id": ctx["id"], "session_id": ctx["session_id"],
                         "start_time": ctx["start_time"], "executed_by": ctx["user"],
                         "statement_text": ";\n".join([*(item.get("session") or []),
                                                       item["sql"]])})
        (root / "history.jsonl").write_text("\n".join(json.dumps(r) for r in rows),
                                            encoding="utf-8")
        applies = d.get("applies") or {}
        manifest = {"history": "history.jsonl", "compare_against": "as_of_execution",
                    "catalog": "catalog.yml",
                    "authority": {"system": d["system"], "metric_views": [{
                        "name": d["view_name"], "snapshot": "views/view.yaml",
                        "sha256": hashlib.sha256(snap.read_bytes()).hexdigest(),
                        # The monitor compares as of each execution, which needs a
                        # start; a window with only an end has applied since ever.
                        "applies": {"from": applies.get("from") or "1970-01-01T00:00:00Z",
                                    "to": applies.get("to")}}]}}
        (root / "pilot.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        models = Path(tmp) / "models"
        models.mkdir()
        # The scan needs one computed source; this one is not a metric use.
        (models / "bench_mart.sql").write_text(
            f"-- touchstone: relation={d['view_name'].rsplit('.', 1)[0]}.bench_mart\n"
            f"SELECT channel, COUNT(*) AS n FROM {dataset['view']['source']} GROUP BY channel\n",
            encoding="utf-8")
        _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                            catalog_path=str(root / "catalog.yml"),
                            db_path=str(Path(tmp) / "bench.db"), dialect=DIALECT)
        try:
            pilot = load_pilot(root, DIALECT)
            registry = build_registry(load_catalog(root / "catalog.yml"),
                                      load_sql_dir(models, DIALECT)[0], DIALECT)
            result = monitor_history(store, read_history(pilot.history), registry, DIALECT,
                                     min_runs=1, pilot=pilot, export=True)
        finally:
            store.close()
    metric_of = {item["id"]: item["metric"] for _, item in chosen}
    out: dict[str, dict] = {}
    for record in result["comparisons"]:
        observed = record.get("unverified_observations")
        if not observed:
            continue      # a candidate the rules did not read: the findings below say so
        for iid in ((record.get("occurrences") or {}).get("execution_ids")
                    or [record["execution"]["id"]]):
            if iid in metric_of and record["definition"]["name"].lower() == metric_of[iid]:
                out.setdefault(iid, {k: v for k, v in observed.items() if k != "note"})
    # A finding with no governed concept (session-dependent identity, an
    # unknown function) made no comparison record — but it IS touchstone's
    # reading of the execution, and the with-observations arm must carry it.
    # A finding about ANOTHER metric is not a reading of this use: taken, it
    # turned another metric's experimental match into "labeled permitted".
    read_as: dict[str, str] = {}
    for f in result["findings"]:
        for iid in f.get("execution_ids") or []:
            if iid in metric_of and f.get("concept") not in (None, metric_of[iid]):
                read_as.setdefault(iid, f["concept"])
            if iid in metric_of and iid not in out \
                    and f.get("concept") in (None, metric_of[iid]):
                out[iid] = {"finding_kind": f["kind"], "reasons": list(f.get("reasons") or []),
                            "suspected": list(f.get("suspected") or []),
                            "variation": f.get("variation"),
                            "calculation_matches": f.get("calculation_matches"),
                            "experimental": f.get("experimental")}
    for iid in metric_of:
        if iid in read_as:
            # Not a reading of THIS use — but that touchstone read the statement
            # as another governed metric bears on its identity.
            out.setdefault(iid, {"finding_kind": None, "reasons": [{
                "code": "other_metric",
                "detail": f"touchstone read this statement as a use of {read_as[iid]}, not of "
                          f"{metric_of[iid]}"}], "suspected": []})
        out.setdefault(iid, {"finding_kind": None, "reasons": [{
            "code": "no_reading", "detail": "touchstone produced no reading of this statement"}],
            "suspected": []})
    return out


# ------------------------------------------------------------------ metrics

def _rate(count: int, of: int) -> dict:
    return {"count": count, "of": of, "rate": round(count / of, 4) if of else None}


def _row(gold: str, assessment) -> dict:
    """One scored item. A missing assessment, one carrying an error, or one
    whose label is outside the advisory vocabulary is FAILED — a call that
    did not answer — never the model's 'undecided'; so the parts of every
    class always sum to the class."""
    error = kind = None
    if assessment is None:
        error = kind = "no assessment returned"
    elif assessment.error:
        error = assessment.error
        kind = getattr(assessment, "error_kind", None) or _error_kind(error)
    elif assessment.label not in ADVISORY_LABELS:
        error = f"label {assessment.label!r} is outside the advisory vocabulary"
        kind = "invalid label"
    failed = error is not None
    return {"gold": gold,
            "predicted": None if failed else assessment.label,
            "confidence": None if failed else assessment.confidence,
            "latency_ms": None if assessment is None else assessment.latency_ms,
            "cost": None if assessment is None else assessment.cost_estimate_usd,
            "failed": failed, "error": error, "error_kind": kind}


def _error_kind(text: str) -> str:
    """The kind of a failure a provider did not classify, read from its text
    — never the request path (`GET /api/…/<statement id>` made one 'kind'
    per statement and merged every POST failure into one)."""
    import re

    m = re.search(r"\bHTTP (\d{3})\b", text)
    if m:
        return f"HTTP {m.group(1)}"
    text = re.sub(r"^(GET|POST|PUT|DELETE|PATCH) \S+: ", "", text)
    head = text.split(":", 1)[0] if ":" in text[:80] else text[:60]
    return " ".join(head.split())[:60]


def _metrics(rows: list[dict]) -> dict:
    """Every class is partitioned, and the parts always sum to the class:
    drift = flagged + labeled permitted + undecided + failed. `undecided` is
    the model answering insufficient_evidence; `failed` is a call that did not
    answer. Abstention, agreement and calibration are over answered items."""
    n = len(rows)
    answered = [r for r in rows if not r["failed"]]

    def part(gold: str, names: dict[str, str]) -> dict:
        group = [r for r in rows if r["gold"] == gold]
        out = {"of": len(group)}
        for key, label in names.items():
            out[key] = _rate(sum(1 for r in group if not r["failed"]
                                 and r["predicted"] == label), len(group))
        out["failed"] = _rate(sum(1 for r in group if r["failed"]), len(group))
        return out

    confusion = {g: {p: sum(1 for r in answered if r["gold"] == g and r["predicted"] == p)
                     for p in ADVISORY_LABELS} for g in ADVISORY_LABELS}
    # Calibration: only answered items with a confidence in [0, 1].
    valid = [r for r in answered if isinstance(r["confidence"], (int, float))
             and not isinstance(r["confidence"], bool) and 0.0 <= r["confidence"] <= 1.0]
    out_of_range = sum(1 for r in answered if r["confidence"] is not None) - len(valid)
    bins, ece = [], 0.0
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        inside = [r for r in valid if lo <= r["confidence"] < hi
                  or (i == 9 and r["confidence"] == 1.0)]
        if not inside:
            continue
        conf = sum(r["confidence"] for r in inside) / len(inside)
        acc = sum(1 for r in inside if r["gold"] == r["predicted"]) / len(inside)
        bins.append({"bin": f"[{lo:.1f}, {hi:.1f})", "n": len(inside),
                     "mean_confidence": round(conf, 4), "accuracy": round(acc, 4)})
        ece += len(inside) / len(valid) * abs(conf - acc)
    latencies = sorted(r["latency_ms"] for r in rows if isinstance(r["latency_ms"], (int, float)))
    failed = [r for r in rows if r["failed"]]
    return {
        "n": n,
        "answered": len(answered),
        "drift": part("suspected_drift", {"flagged": "suspected_drift",
                                          "labeled_permitted": "likely_permitted",
                                          "undecided": "insufficient_evidence"}),
        "permitted": part("likely_permitted", {"confirmed": "likely_permitted",
                                               "flagged_as_drift": "suspected_drift",
                                               "undecided": "insufficient_evidence"}),
        "insufficient_evidence": part("insufficient_evidence", {
            "abstained": "insufficient_evidence", "flagged": "suspected_drift",
            "labeled_permitted": "likely_permitted"}),
        "abstention": _rate(sum(1 for r in answered
                                if r["predicted"] == "insufficient_evidence"), len(answered)),
        "agreement_with_labels": (round(sum(1 for r in answered if r["gold"] == r["predicted"])
                                        / len(answered), 4) if answered else None),
        "confusion": confusion,
        "calibration": {"scored": len(valid), "of": len(answered),
                        "out_of_range": out_of_range, "reliability": bins,
                        "ece": round(ece, 4) if valid else None},
        "errors": {"count": len(failed),
                   "by_kind": dict(Counter(r["error_kind"] for r in failed)),
                   "samples": [r["error"][:200] for r in failed[:3]]},
        # A cost or latency no backend reported is not zero (ai_query reports
        # no usage; the touchstone-alone baseline makes no call).
        "cost_usd": (round(sum(r["cost"] for r in rows), 6)
                     if rows and all(r["cost"] is not None for r in rows) else None),
        "cost_reported": bool(rows) and all(r["cost"] is not None for r in rows),
        "latency_ms": ({"mean": round(sum(latencies) / len(latencies), 2),
                        "p95": latencies[int(0.95 * (len(latencies) - 1))],
                        "reported": len(latencies)} if latencies else None),
    }


def _by_family(rows_by_family: dict[str, list]) -> dict:
    return {fam: _metrics(rows) for fam, rows in sorted(rows_by_family.items())}


def touchstone_alone(observations: dict | None) -> str:
    """The baseline that reads ONLY touchstone's monitor outcome."""
    kind = (observations or {}).get("finding_kind")
    if kind == "suspected_difference":
        return "suspected_drift"
    experimental = (observations or {}).get("experimental") or {}
    if kind == "unverifiable" and experimental.get("kind") == "conforming_use":
        return "likely_permitted"
    return "insufficient_evidence"


class _Fixed:
    """A fixed answer, scored like an assessment (the touchstone-alone baseline)."""

    def __init__(self, label: str):
        self.label, self.confidence, self.error = label, None, None
        self.latency_ms = self.cost_estimate_usd = None


def label_provenance(dataset: dict) -> dict:
    meta = (dataset.get("adjudication") or {}).get("label_provenance") or {}
    return {"authored_by": meta.get("authored_by", "not recorded"),
            "reviewed_by": meta.get("reviewed_by", "not recorded"),
            "human_review": meta.get("human_review", "not recorded"),
            "note": meta.get("note", "")}


# ------------------------------------------------------------------ runner

def run_benchmark(provider, *, dataset: dict | None = None, split: str = "test",
                  observations: dict[str, dict] | None = None,
                  criteria: Criteria | None = None) -> dict[str, Any]:
    """Score `provider` on `split` ("dev", "test" or "all"). `observations`
    maps item id -> touchstone's unverified observations (from the monitor's
    export); with them the run includes the ablation and the touchstone-alone
    baseline."""
    dataset = dataset or load_dataset()
    criteria = criteria or load(1)
    chosen = items(dataset, split)
    arms = {"without_observations": None}
    if observations is not None:
        arms["with_observations"] = None
    report: dict[str, Any] = {
        "dataset": "touchstone metric-use drift v1 (synthetic)",
        "labels": label_provenance(dataset),
        "split": split, "items": len(chosen),
        "families": sorted({fam for fam, _ in chosen}),
        "label_classes": dict(Counter(gold_advisory(i["label"]) for _, i in chosen)),
        "provider": provider.name, "model": provider.model,
        "criteria_version": criteria.version,
        "confidence_source": getattr(provider, "confidence_source", None),
        "advisory_only": "assessments order triage; they never change a finding, a funnel "
                         "bucket or an exit code",
    }
    notes = [] if provider.measures_performance else [UNMEASURED]
    for arm in list(arms):
        records = [build_record(dataset, item, (observations or {}).get(item["id"])
                                if arm == "with_observations" else None)
                   for _, item in chosen]
        if arm == "without_observations":
            records = [without_observations(r) for r in records]
        assessments = {a.record_id: a for a in provider.assess(records, criteria)}
        rows_all, rows_fam = [], {}
        for (fam, item), record in zip(chosen, records):
            row = _row(gold_advisory(item["label"]), assessments.get(record["record_id"]))
            rows_all.append(row)
            rows_fam.setdefault(fam, []).append(row)
        overall = _metrics(rows_all)
        # Per arm: an arm in which no call answered measured nothing, whatever
        # the other arm did.
        arms[arm] = {"overall": overall, "by_family": _by_family(rows_fam),
                     "performance_measured": bool(provider.measures_performance)
                     and overall["answered"] > 0}
        if overall["errors"]["count"]:
            skipped = overall["errors"]["by_kind"].get("not attempted", 0)
            failed_calls = overall["errors"]["count"] - skipped
            what = " and ".join(x for x in (
                f"{failed_calls} of {overall['n']} call(s) FAILED" if failed_calls else "",
                f"{skipped} item(s) were not attempted" if skipped else "") if x)
            notes.append(f"{arm.replace('_', ' ')}: {what} — counted as failed, not as the "
                         f"model's undecided; "
                         + ("NO call answered, so nothing was measured"
                            if overall["answered"] == 0 else "the run is partial"))
    report["arms"] = arms
    report["performance_measured"] = all(a["performance_measured"] for a in arms.values())
    report["partial"] = any(a["overall"]["errors"]["count"] for a in arms.values())
    stopped = getattr(provider, "stopped", None)
    if stopped:
        # The provider stopped calling (a refused credential, an unreachable
        # workspace, an interrupt); what was answered before is kept, and
        # every later item is failed as not attempted.
        report["stopped"] = stopped
        report["stop_kind"] = getattr(provider, "stop_kind", None)
        if getattr(provider, "credential_refused", None):
            report["credential_refused"] = provider.credential_refused
        notes.append(f"the provider stopped calling: {stopped} — every later item is "
                     f"counted as failed (not attempted)")
    if notes:
        report["notes"] = notes
        report["note"] = notes[0]
    baselines: dict[str, Any] = {
        "plain_prompt_model": "not run: the plain-prompt baseline is not implemented yet"}
    if observations is not None:
        rows_all, rows_fam = [], {}
        for fam, item in chosen:
            row = _row(gold_advisory(item["label"]),
                       _Fixed(touchstone_alone(observations.get(item["id"]))))
            rows_all.append(row)
            rows_fam.setdefault(fam, []).append(row)
        alone = {**_metrics(rows_all), "by_family": _by_family(rows_fam)}
        # touchstone's permitted readings are experimental matches: positive
        # verdicts are suspended, so none is reported as a confirmation.
        for m in (alone, *alone["by_family"].values()):
            m["permitted"]["labeled_permitted"] = m["permitted"].pop("confirmed")
            m["positive_readings"] = ("experimental matches — positive verdicts are "
                                      "suspended; agreement counts them as labels, not as "
                                      "confirmations")
        baselines["touchstone_observations_alone"] = alone
    else:
        baselines["touchstone_observations_alone"] = "not run: no observations supplied"
    report["baselines"] = baselines
    return report


# ------------------------------------------------------------ human review

REVIEW_COLUMNS = ("id", "family", "metric", "priority", "session_statements", "sql",
                  "label", "rationale", "reviewer")


ADVISORY_NOTE = ("advisory — a model's reading that orders triage; never a finding, a "
                 "positive verdict or an exit code")


def assess_records(provider, records: list[dict], criteria: Criteria | None = None, *,
                   with_observations: bool = True,
                   cache: dict | None = None) -> tuple[list[dict], dict]:
    """Advisory verdicts for exported comparison records (`monitor
    --export-comparisons`): one per record, i.e. per distinct judgment.

    `cache` maps a cache key to an earlier answered assessment and is updated
    in place. The key is the record's judgment, the EXACT prompt the provider
    would send, the provider, its model and the criteria version — so any
    change of evidence, definition, policy, model or criteria is judged anew,
    and an unchanged judgment is never paid for twice. Failed calls are not
    cached. Returns (verdicts, stats)."""
    from touchstone.assess.providers import render_prompt

    criteria = criteria or load(1)
    cache = {} if cache is None else cache
    prepared = []
    for record in records:
        sent = record if with_observations else without_observations(record)
        prompt = render_prompt(sent, criteria)
        key = hashlib.sha256(json.dumps([
            record.get("judgment_key") or record["record_id"],
            hashlib.sha256(prompt.encode()).hexdigest(), provider.name, provider.model,
            criteria.version]).encode()).hexdigest()
        prepared.append((record, sent, key))
    todo = [(sent, key) for _, sent, key in prepared if key not in cache]
    answered = provider.assess([sent for sent, _ in todo], criteria) if todo else []
    fresh = {}
    for (_, key), a in zip(todo, answered):
        fresh[key] = a.as_dict()
        if a.error is None:
            cache[key] = a.as_dict()
    verdicts = []
    for record, sent, key in prepared:
        from_cache = key not in fresh
        a = cache[key] if from_cache else fresh[key]
        occ = record.get("occurrences") or {}
        verdicts.append({
            "record_id": record["record_id"], "judgment_key": record.get("judgment_key"),
            "metric": record["definition"]["name"], "view": record["definition"].get("view"),
            "executions": occ.get("executions", 1),
            "first_start_time": occ.get("first_start_time"),
            "last_start_time": occ.get("last_start_time"),
            "statement": record["execution"]["statement"],
            "rules_reading": (record.get("unverified_observations") or {}).get("finding_kind"),
            "label": None if a["error"] else a["label"], "answers": a["answers"],
            "confidence": a["confidence"], "provider": a["provider"], "model": a["model"],
            "criteria_version": a["criteria_version"], "error": a["error"],
            "error_kind": a["error_kind"], "from_cache": from_cache,
            "observations_sent": "unverified_observations" in sent,
            "advisory": ADVISORY_NOTE})
    stats = {
        "records": len(records),
        "executions": sum(v["executions"] for v in verdicts),
        "calls": len(todo), "from_cache": len(records) - len(todo),
        "labels": dict(Counter(v["label"] for v in verdicts if v["label"])),
        "failed": dict(Counter(v["error_kind"] for v in verdicts if v["error"])),
        "without_rules_reading": sum(1 for v in verdicts if v["rules_reading"] is None),
        "measures_performance": bool(getattr(provider, "measures_performance", False))}
    return verdicts, stats


def review_sheet(dataset: dict) -> list[dict]:
    """Rows for a BLIND human review: every item without its label or
    rationale. `priority` marks what most needs a human — items whose labels
    were contested between the model passes, and every item the grouping
    policy bears on (any GROUP BY or date truncation) — computed from the SQL
    and the adjudication ids ONLY, never from a label (round 18: a priority
    read from the label told the reviewer which items were grain drift)."""
    contested = {d["id"] for d in (dataset.get("adjudication") or {}).get("decisions", [])}
    rows = []
    for fam, item in items(dataset):
        text = " ".join([item["sql"], *(item.get("session") or [])]).upper()
        grouping = "GROUP BY" in text or "DATE_TRUNC" in text or "TRUNC(" in text
        priority = "; ".join(p for p, on in (("contested between model passes",
                                               item["id"] in contested),
                                              ("grouping policy", grouping)) if on)
        rows.append({"id": item["id"], "family": fam, "metric": item["metric"],
                     "priority": priority,
                     "session_statements": " ;; ".join(item.get("session") or []),
                     "sql": item["sql"], "label": "", "rationale": "", "reviewer": ""})
    return rows


def compare_labels(dataset: dict, rows: list[dict]) -> dict:
    """A filled review sheet against the dataset's labels, counted by DISTINCT
    id. Reports agreement, every disagreement, ids never reviewed, and the
    sheet's own problems (unknown ids, invalid labels, an id labeled twice);
    it changes nothing — a disagreement is decided by a person and recorded
    in adjudication.yaml."""
    labels = {i["id"]: i["label"] for _, i in items(dataset)}

    def norm(row: dict) -> dict:
        # A short row leaves cells None (csv restval): read them as blank.
        return {str(k).strip().lower(): "" if v is None else v.strip() if isinstance(v, str)
                else v for k, v in row.items()}
    rows = [norm(r) for r in rows]
    if rows and not {"id", "label"} <= set(rows[0]):
        raise ValueError("the sheet needs `id` and `label` columns (as review-sheet wrote "
                         "them)")
    reviewed = [r for r in rows if r.get("label")]
    unknown = sorted({r["id"] or "(a row without an id)" for r in reviewed
                      if r["id"] not in labels})
    bad = sorted({r["id"] for r in reviewed if r["label"] not in LABELS})
    by_id: dict[str, list[dict]] = {}
    for r in reviewed:
        if r["id"] in labels and r["label"] in LABELS:
            by_id.setdefault(r["id"], []).append(r)
    duplicates = sorted(i for i, rs in by_id.items() if len(rs) > 1)
    conflicting = sorted(i for i, rs in by_id.items() if len({r["label"] for r in rs}) > 1)
    scored = {i: rs[0] for i, rs in by_id.items() if i not in conflicting}
    disagreements = [{"id": i, "dataset": labels[i], "reviewer_label": r["label"],
                      "reviewer_rationale": r.get("rationale", ""),
                      "reviewer": r.get("reviewer", ""),
                      "advisory_class_changes": gold_advisory(labels[i])
                      != gold_advisory(r["label"])}
                     for i, r in sorted(scored.items()) if r["label"] != labels[i]]
    return {"reviewed": len(scored), "of": len(labels),
            "agreed": len(scored) - len(disagreements), "disagreements": disagreements,
            "not_reviewed": sorted(set(labels) - set(by_id)),
            "unknown_ids": unknown, "invalid_labels": bad,
            "duplicate_ids": duplicates, "conflicting_ids": conflicting}


def to_json(report: dict) -> str:
    return json.dumps(report, indent=1, sort_keys=False, default=str)
