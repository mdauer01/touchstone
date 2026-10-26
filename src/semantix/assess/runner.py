"""Evaluate an advisory provider against the independently labeled metric uses.

Reports, overall and per query family:

* missed drift — items whose gold class is `suspected_drift` that the
  provider called `likely_permitted` (the costly error);
* false alerts — gold `likely_permitted` called `suspected_drift`;
* abstention — how often it answered `insufficient_evidence`;
* calibration — a reliability table over the confidence of the predicted
  class, and the expected calibration error (ECE);
* cost and latency, as the provider reports them.

Two baselines sit beside the provider: semantix's own observations alone
(a fixed mapping from the monitor's finding kind) and a plain-prompt model
(no criteria) — the latter needs a model backend and is reported as not run
until one exists. When the records carry semantix's observations, the
provider runs twice — with and without them — so the ablation shows whether
those unverified observations help or mislead.

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

from semantix.assess import ADVISORY_LABELS
from semantix.assess.criteria import Criteria, load
from semantix.assess.record import FORMAT, OBSERVATIONS_NOTE, validate, without_observations

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
    root = Path(path) if path else DATASET_DIR
    definitions = yaml.safe_load((root / "definitions.yaml").read_text(encoding="utf-8"))
    view = yaml.safe_load(definitions["view"])
    measures = {m["name"]: m["expr"] for m in view["measures"]}
    families, seen = [], set()
    for f in sorted(root.glob("*.yaml")):
        if f.name in ("definitions.yaml", "adjudication.yaml", "blind_labels.yaml"):
            continue
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
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
    adjudication_file = root / "adjudication.yaml"
    adjudication = (yaml.safe_load(adjudication_file.read_text(encoding="utf-8")) or {}
                    if adjudication_file.exists() else {})
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


def _relations(sql: str) -> list[str]:
    from semantix.extraction.sqlutil import table_fqname

    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:  # noqa: BLE001
        return []
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return sorted({table_fqname(t).lower() for t in tree.find_all(exp.Table)
                   if t.name and t.name.lower() not in ctes})


def build_record(dataset: dict, item: dict, observations: dict | None = None) -> dict:
    """One semantix-comparison/1 record for a dataset item. The labels are
    never part of the record."""
    d = dataset["definitions"]
    view = dataset["view"]
    catalog = d["catalog"]
    reads = _relations(item["sql"])
    record = {
        "format": FORMAT,
        "record_id": f"bench_{item['id']}",
        "execution": {"id": item["id"], "start_time": None, "session_id": None, "user": None,
                      "client": None, "asset": None, "statement": item["sql"],
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
            "applies": d.get("applies")},
        "schemas": {r: dict(catalog[r]) for r in reads if r in catalog},
        "session_evidence": {"context": None, "events": list(item.get("session") or [])},
    }
    if observations is not None:
        record["unverified_observations"] = {"note": OBSERVATIONS_NOTE, **observations}
    errors = validate(record)
    if errors:
        raise ValueError(f"{item['id']}: record does not validate: {errors}")
    return record


def observations_from_monitor(dataset: dict, split: str = "all") -> dict[str, dict]:
    """semantix's own reading of every item, through the REAL monitor: a pilot
    package is synthesized from the dataset (the definitions' metric view and
    catalog, one execution per item with its session statements), scanned,
    and monitored with `export=True`. Returns item id -> the record's
    unverified observations. An item the monitor produced no record for maps
    to an empty reading (no finding)."""
    import tempfile

    from semantix.adapters.catalog import load_catalog
    from semantix.adapters.sql_dir import load_sql_dir
    from semantix.engine import run_scan
    from semantix.services.history import read_history
    from semantix.services.monitor import build_registry, monitor_history
    from semantix.services.pilot import load_pilot

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
        start = (d.get("applies") or {}).get("from") or "2026-09-01T00:00:00Z"
        ts = start[:10] + "T12:00:00Z"
        rows = [{"statement_id": item["id"], "session_id": item["id"], "start_time": ts,
                 "executed_by": "benchmark",
                 "statement_text": ";\n".join([*(item.get("session") or []), item["sql"]])}
                for _, item in chosen]
        (root / "history.jsonl").write_text("\n".join(json.dumps(r) for r in rows),
                                            encoding="utf-8")
        applies = d.get("applies") or {}
        manifest = {"history": "history.jsonl", "compare_against": "as_of_execution",
                    "catalog": "catalog.yml",
                    "authority": {"system": d["system"], "metric_views": [{
                        "name": d["view_name"], "snapshot": "views/view.yaml",
                        "sha256": hashlib.sha256(snap.read_bytes()).hexdigest(),
                        "applies": {"from": applies.get("from"), "to": applies.get("to")}}]}}
        (root / "pilot.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
        models = Path(tmp) / "models"
        models.mkdir()
        # The scan needs one computed source; this one is not a metric use.
        (models / "bench_mart.sql").write_text(
            f"-- semantix: relation={d['view_name'].rsplit('.', 1)[0]}.bench_mart\n"
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
        iid = record["execution"]["id"]
        if iid in metric_of and record["definition"]["name"].lower() == metric_of[iid]:
            obs = {k: v for k, v in record["unverified_observations"].items() if k != "note"}
            out.setdefault(iid, obs)
    for iid in metric_of:
        out.setdefault(iid, {"finding_kind": None, "reasons": [], "suspected": []})
    return out


# ------------------------------------------------------------------ metrics

def _metrics(rows: list[tuple[str, str, float | None, dict]]) -> dict:
    """rows: (gold advisory, predicted advisory, confidence, assessment extras)."""
    n = len(rows)
    gold_drift = [r for r in rows if r[0] == "suspected_drift"]
    gold_ok = [r for r in rows if r[0] == "likely_permitted"]
    missed = sum(1 for r in gold_drift if r[1] == "likely_permitted")
    alerts = sum(1 for r in gold_ok if r[1] == "suspected_drift")
    abstained = sum(1 for r in rows if r[1] == "insufficient_evidence")
    correct = sum(1 for r in rows if r[0] == r[1])
    confusion = {g: {p: sum(1 for r in rows if r[0] == g and r[1] == p)
                     for p in ADVISORY_LABELS} for g in ADVISORY_LABELS}
    bins = []
    ece = 0.0
    scored = [r for r in rows if r[2] is not None]
    for i in range(10):
        lo, hi = i / 10, (i + 1) / 10
        inside = [r for r in scored if lo <= r[2] < hi or (i == 9 and r[2] == 1.0)]
        if not inside:
            continue
        conf = sum(r[2] for r in inside) / len(inside)
        acc = sum(1 for r in inside if r[0] == r[1]) / len(inside)
        bins.append({"bin": f"[{lo:.1f}, {hi:.1f})", "n": len(inside),
                     "mean_confidence": round(conf, 4), "accuracy": round(acc, 4)})
        ece += len(inside) / len(scored) * abs(conf - acc)
    latencies = sorted(r[3].get("latency_ms") or 0.0 for r in rows)
    return {
        "n": n,
        "missed_drift": {"count": missed, "of": len(gold_drift),
                         "rate": round(missed / len(gold_drift), 4) if gold_drift else None},
        "false_alerts": {"count": alerts, "of": len(gold_ok),
                         "rate": round(alerts / len(gold_ok), 4) if gold_ok else None},
        "abstention": {"count": abstained, "rate": round(abstained / n, 4) if n else None},
        "agreement_with_gold": round(correct / n, 4) if n else None,
        "confusion": confusion,
        "calibration": {"reliability": bins,
                        "ece": round(ece, 4) if scored else None},
        "cost_usd": round(sum(r[3].get("cost_estimate_usd") or 0.0 for r in rows), 6),
        "latency_ms": {"mean": round(sum(latencies) / n, 2) if n else None,
                       "p95": latencies[int(0.95 * (n - 1))] if n else None},
    }


def _by_family(rows_by_family: dict[str, list]) -> dict:
    return {fam: _metrics(rows) for fam, rows in sorted(rows_by_family.items())}


def semantix_alone(observations: dict | None) -> str:
    """The baseline that reads ONLY semantix's monitor outcome."""
    kind = (observations or {}).get("finding_kind")
    if kind == "suspected_difference":
        return "suspected_drift"
    experimental = (observations or {}).get("experimental") or {}
    if kind == "unverifiable" and experimental.get("kind") == "conforming_use":
        return "likely_permitted"
    return "insufficient_evidence"


# ------------------------------------------------------------------ runner

def run_benchmark(provider, *, dataset: dict | None = None, split: str = "test",
                  observations: dict[str, dict] | None = None,
                  criteria: Criteria | None = None) -> dict[str, Any]:
    """Score `provider` on `split` ("dev", "test" or "all"). `observations`
    maps item id -> semantix's unverified observations (from the monitor's
    export); with them the run includes the ablation and the semantix-alone
    baseline."""
    dataset = dataset or load_dataset()
    criteria = criteria or load(1)
    chosen = items(dataset, split)
    arms = {"without_observations": None}
    if observations is not None:
        arms["with_observations"] = None
    report: dict[str, Any] = {
        "dataset": "semantix metric-use drift v1 (synthetic)",
        "split": split, "items": len(chosen),
        "families": sorted({fam for fam, _ in chosen}),
        "gold_classes": dict(Counter(gold_advisory(i["label"]) for _, i in chosen)),
        "provider": provider.name, "model": provider.model,
        "criteria_version": criteria.version,
        "performance_measured": bool(provider.measures_performance),
        "advisory_only": "assessments order triage; they never change a finding, a funnel "
                         "bucket or an exit code",
    }
    if not provider.measures_performance:
        report["note"] = UNMEASURED
    for arm in list(arms):
        records = [build_record(dataset, item, (observations or {}).get(item["id"])
                                if arm == "with_observations" else None)
                   for _, item in chosen]
        if arm == "without_observations":
            records = [without_observations(r) for r in records]
        assessments = {a.record_id: a for a in provider.assess(records, criteria)}
        rows_all, rows_fam = [], {}
        for (fam, item), record in zip(chosen, records):
            a = assessments.get(record["record_id"])
            predicted = a.label if a is not None and not a.error else "insufficient_evidence"
            row = (gold_advisory(item["label"]), predicted,
                   a.confidence if a is not None else None,
                   a.as_dict() if a is not None else {})
            rows_all.append(row)
            rows_fam.setdefault(fam, []).append(row)
        arms[arm] = {"overall": _metrics(rows_all), "by_family": _by_family(rows_fam)}
    report["arms"] = arms
    baselines: dict[str, Any] = {
        "plain_prompt_model": "not run: needs a model backend (credentials this "
                              "environment does not have)"}
    if observations is not None:
        rows = [(gold_advisory(i["label"]), semantix_alone(observations.get(i["id"])), None, {})
                for _, i in chosen]
        baselines["semantix_observations_alone"] = _metrics(rows)
    else:
        baselines["semantix_observations_alone"] = "not run: no observations supplied"
    report["baselines"] = baselines
    return report


def to_json(report: dict) -> str:
    return json.dumps(report, indent=1, sort_keys=False, default=str)
