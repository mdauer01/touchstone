"""dbt adapter: manifest.json + compiled SQL (§2, v0 adapter set).

Accepts a project directory containing `manifest.json` or `target/manifest.json`.
Compiled SQL is resolved per node in order of fidelity: compiled file on disk,
`compiled_code` embedded in the manifest, then `raw_code` only if it contains
no unrendered Jinja.
"""

from __future__ import annotations

from pathlib import Path

from touchstone.adapters.loadutil import load_json
from touchstone.adapters.base import ComputedArtifact, DeclaredSpec, topo_sort


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
    data = load_json(manifest_path)

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
        # `config` can be present and null in a real manifest, which made
        # `.get("config", {}).get(...)` raise and take the whole scan down.
        config = node.get("config") or {}
        if kind == "model" and config.get("materialized") != "ephemeral":
            continue
        if kind in ("test", "analysis", "operation", "sql_operation", "unit_test"):
            continue
        if kind == "model":
            continue  # ephemeral models are handled with the rest below
        # Every OTHER resource_type is disclosed, not dropped. The chain of
        # `if kind == ...` fell through for anything unrecognized — a missing
        # resource_type, `function`, and whatever dbt adds next — so the
        # docstring's "DISCLOSED rather than dropped" failed open exactly as
        # dbt's node vocabulary grows.
        skipped.append({
            "object_id": uid,
            "relation": _relation(node),
            "path": node.get("original_file_path"),
            "reason": (f"{kind}s are not read by this adapter" if kind
                       else "node has no resource_type and was not read"),
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
                           if ("{{" in raw or "{%" in raw)
                           else "no compiled SQL in manifest or target/"),
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
    data = load_json(_find_manifest(Path(project_dir)))
    semantic_models = data.get("semantic_models") or {}

    # A metric's `type_params.measure` is a REFERENCE — usually just a name.
    # The aggregation and expression live on the measure definition inside the
    # owning semantic model, so resolving them off the reference produced a
    # claim that was never expressible.
    measures: dict[str, tuple[str, str | None, str]] = {}
    model_relation: dict[str, str] = {}
    for sm in semantic_models.values():
        sm_name = str(sm.get("name") or "")
        node_relation = sm.get("node_relation") or {}
        # A bare `alias` is not a relation. Falling back to it while
        # `schema_name` and `database` sat right beside it produced a
        # proof-grade claim over a relation that does not exist, whose lineage
        # terminals could never match the computed model — a false
        # ORPHAN_CLAIM or CONTRADICTION every time.
        relation = node_relation.get("relation_name") or ".".join(
            str(p) for p in (node_relation.get("database"),
                             node_relation.get("schema_name"),
                             node_relation.get("alias")) if p
        )
        if sm_name and relation:
            model_relation[sm_name] = str(relation).replace('"', "").replace("`", "")
        for measure in sm.get("measures") or []:
            m_name = measure.get("name")
            if m_name:
                measures[str(m_name)] = (
                    str(measure.get("agg") or "").lower(),
                    measure.get("expr"),
                    sm_name,
                    measure,
                )

    specs: list[DeclaredSpec] = []
    for metric in (data.get("metrics") or {}).values():
        name = metric.get("name")
        if not name:
            continue
        expr, model, untranslatable = _metric_expression(metric, model_relation, measures)
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


def _where_filters(container: dict) -> list[str]:
    """Raw `where_sql_template` strings from a MetricFlow filter block."""
    filt = container.get("filter") or {}
    return [
        str(w.get("where_sql_template"))
        for w in (filt.get("where_filters") or [])
        if w.get("where_sql_template")
    ]


def _metric_expression(metric: dict, model_relation: dict[str, str],
                       measures: dict[str, tuple[str, str | None, str]]):
    """(expr, model, untranslatable) for one MetricFlow metric.

    Only `simple` metrics map cleanly onto a single measure over one table.
    Ratio, derived, cumulative and conversion metrics compose other metrics or
    carry window semantics the SemanticCore does not represent, and metric- or
    measure-level `filter` blocks are Jinja templates (`Dimension(...)`) we
    cannot render faithfully — all are reported rather than guessed, because a
    claim missing its filter is a different claim, not a weaker one.
    """
    metric_type = str(metric.get("type") or "").lower()
    type_params = metric.get("type_params") or {}
    measure_ref = type_params.get("measure") or {}
    measure_name = measure_ref.get("name") if isinstance(measure_ref, dict) else measure_ref

    if metric_type != "simple" or not measure_name:
        return None, None, [f"{metric_type or 'unknown'} metric is not a single measure"]

    untranslatable = [f"metric filter not translatable: {w}"
                      for w in _where_filters(metric)]
    if isinstance(measure_ref, dict):
        untranslatable += [f"measure filter not translatable: {w}"
                           for w in _where_filters(measure_ref)]

    agg, expr, owning_model, measure = measures.get(
        str(measure_name), ("", None, "", {}))
    model = model_relation.get(owning_model)
    if not agg:
        untranslatable.append(
            f"measure {measure_name} could not be resolved to an aggregation"
        )
        return None, model, untranslatable

    # Qualifiers that change what the measure MEANS. Dropping them produced a
    # proof-grade `SUM(x)` claim for a measure that is explicitly not additive
    # — the same failure `_where_filters` exists to prevent: a claim missing a
    # semantic qualifier is a different claim, not a weaker one.
    # (`agg_time_dimension` names a time column and does not change the
    # aggregation for a simple metric; cumulative metrics — where it would —
    # are refused as non-simple above.)
    if measure.get("non_additive_dimension"):
        untranslatable.append(
            f"measure {measure_name} declares non_additive_dimension "
            f"(semi-additive measure): {measure['non_additive_dimension']}")
        return None, model, untranslatable
    if isinstance(measure_ref, dict):
        if measure_ref.get("fill_nulls_with") is not None:
            untranslatable.append(
                f"metric declares fill_nulls_with={measure_ref['fill_nulls_with']}, "
                f"which changes the measure's value for missing periods")
            return None, model, untranslatable
        # dbt serializes join_to_timespine on the MEASURE REFERENCE
        # (`type_params.measure.join_to_timespine`), not on type_params —
        # the first version guarded a location that does not exist in dbt's
        # MetricInputMeasure schema, so the refusal never fired on real
        # manifests and a timespine-joined metric reached proof grade
        # missing the qualifier that changes its row universe.
        if measure_ref.get("join_to_timespine"):
            untranslatable.append(
                "metric declares join_to_timespine, which changes the row universe")
            return None, model, untranslatable
    if isinstance(type_params, dict) and type_params.get("join_to_timespine"):
        untranslatable.append(
            "metric declares join_to_timespine, which changes the row universe")
        return None, model, untranslatable

    rendered = _render_agg(agg, expr or measure_name, measure)
    if rendered is None:
        untranslatable.append(
            f"measure {measure_name} uses agg `{agg}`, which this adapter does "
            f"not know how to render as SQL")
        return None, model, untranslatable
    return rendered, model, untranslatable


# MetricFlow's `agg` vocabulary. Emitting the raw value as `agg(expr)` made
# `count_distinct(x)` and `average(x)` — MetricFlow's spellings — unparseable,
# so every distinct-count and average metric in a dbt Semantic Layer project
# was unverifiable, AND `spec.untranslatable` stayed empty so the honest-
# refusal channel was bypassed: the failure surfaced as a canonicalization
# error blaming the declaration's shape.
_AGG_SQL = {
    "sum": "SUM({expr})",
    "min": "MIN({expr})",
    "max": "MAX({expr})",
    "count": "COUNT({expr})",
    "average": "AVG({expr})",
    "avg": "AVG({expr})",
    "count_distinct": "COUNT(DISTINCT {expr})",
    "sum_boolean": "SUM(CASE WHEN {expr} THEN 1 ELSE 0 END)",
}


def _render_agg(agg: str, expr: str, measure: dict) -> str | None:
    """SQL for a MetricFlow aggregation, or None if we do not model it.

    `percentile` is deliberately absent: its value lives in
    `agg_params.percentile` with a `use_discrete_percentile` flag, and the
    SQL differs per warehouse. Refusing is honest; guessing is not.
    """
    template = _AGG_SQL.get(agg)
    if template is None:
        return None
    return template.format(expr=expr)


def _find_manifest(root: Path) -> Path:
    """The manifest dbt most recently wrote.

    When BOTH exist, prefer the NEWER by mtime: `dbt compile` writes only
    `target/manifest.json`, so a committed copy at the project root (docs/CI
    artifact) plus a fresh local compile meant the adapter verified the
    previous run's SQL while the current manifest sat unread in target/.
    """
    candidates = [p for p in (root / "manifest.json",
                              root / "target" / "manifest.json") if p.is_file()]
    if not candidates:
        raise FileNotFoundError(f"no manifest.json under {root}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _relation(node: dict) -> str:
    """The relation identity a real manifest uses.

    Compiled SQL, `relation_name`, and `semantic_models[*].node_relation` all
    refer to models three-part as `database.schema.alias`. Composing a
    two-part `schema.alias` here meant registry keys, duplicate detection and
    lineage terminals never matched the names appearing in the SQL itself —
    every dependency edge silently missed on any real project.
    """
    declared = node.get("relation_name")
    if declared:
        return str(declared).replace('"', "").replace("`", "")
    name = node.get("alias") or node.get("name")
    parts = [node.get("database"), node.get("schema") or "public", name]
    return ".".join(str(p) for p in parts if p)


def _compiled_sql(root: Path, node: dict) -> tuple[str | None, str | None]:
    """The SQL this model actually runs, and where we read it from.

    `compiled_code` in the manifest OUTRANKS `target/compiled/*.sql` on disk.
    The other order verified the PREVIOUS run's SQL whenever a CI-artifact
    manifest landed in a tree with a leftover `target/` — producing a
    CONTRADICTION against code that matches its declaration exactly, with
    nothing in `skipped` to reveal it.
    """
    compiled_code = node.get("compiled_code")
    if compiled_code:
        return compiled_code, node.get("original_file_path")
    compiled_path = node.get("compiled_path")
    if compiled_path:
        # `compiled_path` comes from a third-party manifest, so it is
        # untrusted input: an absolute path or one containing `..` would
        # otherwise make the adapter read arbitrary files off the host.
        base = root.resolve()
        p = (root / compiled_path).resolve()
        if p.is_file() and p.is_relative_to(base):
            try:
                # utf-8-sig, strict: a BOM killed whole models, and
                # errors="replace" silently turned a latin-1 byte inside a
                # predicate into U+FFFD — fingerprinting a DIFFERENT filter
                # than the warehouse runs, at proof grade.
                return p.read_text(encoding="utf-8-sig", errors="strict"), str(p)
            except (OSError, UnicodeDecodeError):
                pass
    raw_code = node.get("raw_code")
    # `{%` too: a raw_code carrying only Jinja STATEMENTS (`{% if ... %}`)
    # passed the `{{`-only check, became an artifact of template text, then
    # failed to parse — misclassified as unparseable SQL instead of the
    # actionable "run `dbt compile`", with the template recorded as the
    # model's definition.
    if raw_code and "{{" not in raw_code and "{%" not in raw_code:
        return raw_code, node.get("original_file_path")
    return None, None


