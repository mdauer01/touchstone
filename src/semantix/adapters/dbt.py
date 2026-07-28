"""dbt adapter: manifest.json + compiled SQL (§2, v0 adapter set).

Accepts a project directory containing `manifest.json` or `target/manifest.json`.
Compiled SQL is resolved per node in order of fidelity: compiled file on disk,
`compiled_code` embedded in the manifest, then `raw_code` only if it contains
no unrendered Jinja.
"""

from __future__ import annotations

import json
from pathlib import Path

from semantix.adapters.base import ComputedArtifact, DeclaredSpec, topo_sort


def load_dbt(project_dir: str | Path) -> list[ComputedArtifact]:
    """Artifacts only. Prefer load_dbt_detailed where the caller can report
    what was skipped — a dropped model is a dropped definition."""
    return load_dbt_detailed(project_dir)[0]


def load_dbt_detailed(
    project_dir: str | Path,
) -> tuple[list[ComputedArtifact], list[dict]]:
    """(artifacts, skipped). A model with no compiled SQL cannot be
    fingerprinted, but it must never vanish silently: a scan that quietly
    ignores the one model contradicting a declared metric reports CERTIFIED,
    which is exactly backwards. Callers surface `skipped` so coverage numbers
    describe the whole estate rather than only the parts we could read.
    """
    root = Path(project_dir)
    manifest_path = _find_manifest(root)
    data = json.loads(manifest_path.read_text())

    all_nodes = data.get("nodes") or {}
    nodes = {
        uid: node for uid, node in all_nodes.items()
        if node.get("resource_type") == "model"
    }
    relation_of = {uid: _relation(node) for uid, node in nodes.items()}

    artifacts: list[ComputedArtifact] = []
    skipped: list[dict] = []

    # Node types we do not read are DISCLOSED rather than dropped. Snapshots
    # and ephemeral models define real transformations; an ephemeral model in
    # particular is inlined into its consumers, so ignoring it silently made
    # its logic vanish from lineage while appearing nowhere in coverage.
    for uid, node in all_nodes.items():
        kind = node.get("resource_type")
        if kind == "model" and node.get("config", {}).get("materialized") != "ephemeral":
            continue
        if kind in ("test", "analysis", "operation", "sql_operation"):
            continue
        if kind == "model":
            continue  # ephemeral models are handled with the rest below
        if kind in ("snapshot", "seed"):
            skipped.append({
                "object_id": uid,
                "relation": _relation(node),
                "path": node.get("original_file_path"),
                "reason": f"{kind}s are not read by this adapter",
            })
    for uid, node in nodes.items():
        sql, path = _compiled_sql(root, node)
        if sql is None:
            raw = node.get("raw_code") or ""
            skipped.append({
                "object_id": uid,
                "relation": relation_of[uid],
                "path": node.get("original_file_path"),
                "reason": ("unrendered Jinja and no compiled SQL — run `dbt compile`"
                           if "{{" in raw else "no compiled SQL in manifest or target/"),
            })
            continue
        depends = sorted(
            relation_of[dep]
            for dep in (node.get("depends_on") or {}).get("nodes", [])
            if dep in relation_of
        )
        artifacts.append(
            ComputedArtifact(
                system="dbt",
                object_id=uid,
                relation=relation_of[uid],
                sql=sql,
                path=path,
                depends_on=depends,
            )
        )
    return topo_sort(artifacts), skipped


def load_dbt_metrics(project_dir: str | Path) -> list[DeclaredSpec]:
    """Declared metrics from the SAME manifest the models come from.

    A dbt project using the Semantic Layer carries `metrics` and
    `semantic_models` alongside `nodes`, so both sides of the reconciliation
    are already in the file we open — and reading only one of them reported
    every declared metric as a SHADOW_METRIC, which is precisely backwards.
    """
    data = json.loads(_find_manifest(Path(project_dir)).read_text())
    semantic_models = data.get("semantic_models") or {}
    # semantic model name -> the relation it is defined over
    model_relation: dict[str, str] = {}
    for sm in semantic_models.values():
        name = sm.get("name")
        node_relation = sm.get("node_relation") or {}
        relation = node_relation.get("relation_name") or node_relation.get("alias")
        if name and relation:
            model_relation[str(name)] = str(relation).replace('"', "")

    specs: list[DeclaredSpec] = []
    for metric in (data.get("metrics") or {}).values():
        name = metric.get("name")
        if not name:
            continue
        expr, model, untranslatable = _metric_expression(metric, model_relation)
        specs.append(DeclaredSpec(
            system="dbt_metrics",
            name=str(name),
            description=metric.get("description"),
            model=model,
            expr=expr,
            untranslatable=untranslatable,
            path=metric.get("original_file_path"),
        ))
    return specs


def _metric_expression(metric: dict, model_relation: dict[str, str]):
    """(expr, model, untranslatable) for one MetricFlow metric.

    Only `simple` metrics map cleanly onto a single measure over one table.
    Ratio, derived, cumulative and conversion metrics compose other metrics
    or carry window semantics the SemanticCore does not represent — those are
    reported as untranslatable rather than guessed into a wrong claim.
    """
    metric_type = str(metric.get("type") or "").lower()
    type_params = metric.get("type_params") or {}
    measure = type_params.get("measure") or {}
    measure_name = measure.get("name") if isinstance(measure, dict) else measure

    if metric_type != "simple" or not measure_name:
        return None, None, [f"{metric_type or 'unknown'} metric is not a single measure"]

    agg = str(measure.get("agg") or "").lower() if isinstance(measure, dict) else ""
    expr = measure.get("expr") if isinstance(measure, dict) else None
    column = expr or measure_name
    model = model_relation.get(str(measure.get("semantic_model") or "")) if isinstance(measure, dict) else None
    if not agg:
        return None, model, [f"measure {measure_name} declares no aggregation"]
    return f"{agg}({column})", model, []


def _find_manifest(root: Path) -> Path:
    for candidate in (root / "manifest.json", root / "target" / "manifest.json"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no manifest.json under {root}")


def _relation(node: dict) -> str:
    schema = node.get("schema") or "public"
    name = node.get("alias") or node.get("name")
    return f"{schema}.{name}"


def _compiled_sql(root: Path, node: dict) -> tuple[str | None, str | None]:
    compiled_path = node.get("compiled_path")
    if compiled_path:
        p = root / compiled_path
        if p.is_file():
            return p.read_text(), str(p)
    compiled_code = node.get("compiled_code")
    if compiled_code:
        return compiled_code, node.get("original_file_path")
    raw_code = node.get("raw_code")
    if raw_code and "{{" not in raw_code:
        return raw_code, node.get("original_file_path")
    return None, None


