"""Unity Catalog metric views (§2): governed measure declarations as claims.

Reads Databricks metric-view YAML (spec 1.x, GA April 2026) — the definition
`CREATE VIEW ... WITH METRICS LANGUAGE YAML` carries, and what dbt-databricks
v1.12 materializes. Each `measures[]` entry becomes one DeclaredSpec named by
the measure, over the view's `source`, carrying the view-level `filter`; the
expressions are parsed as Databricks SQL and rendered for the scan dialect.

What is refused rather than approximated — a claim missing a semantic
qualifier is a different claim:

* WINDOW MEASURES. `window:` (order / range / semiadditive / offset) makes a
  measure a windowed computation; certifying its bare aggregate would verify
  a metric nobody declared.
* JOIN REFERENCES. A measure or filter reaching through a join alias depends
  on the declared join graph, which this adapter does not yet apply. Joins
  themselves are disclosed, not dropped — they are lookup joins applied
  on-demand, so base-table measures stay verifiable while they sit unused.
* PARAMETERS. A `:param` in an expression makes the metric a family of
  metrics; there is no single claim to verify.
* FIELD INDIRECTION. A measure referencing a declared field whose expression
  is anything but the identity column is ambiguous between the field and a
  physical column; resolution order is engine-internal, so we refuse instead
  of guessing.
* UNKNOWN KEYS, anywhere above or on a measure (deny-by-default, the same
  rule extraction.completeness applies to SQL args). Display metadata
  (comment / display_name / format / synonyms) is modelled and ignored;
  `materialization` is serving configuration and is disclosed.

Intake: one YAML file (the view's name is the file stem — the YAML itself
does not carry it) or a directory of them.
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlglot
from sqlglot import exp

from semantix.adapters.base import DeclaredSpec
from semantix.adapters.loadutil import load_yaml

SUPPORTED_VERSIONS = ("1", "1.0", "1.1")

_TOP_KEYS = frozenset({"version", "source", "comment", "parameters", "filter",
                       "joins", "fields", "dimensions", "measures",
                       "materialization"})
_JOIN_KEYS = frozenset({"name", "source", "on", "using", "cardinality",
                        "joins", "rely"})
_FIELD_KEYS = frozenset({"name", "expr", "comment", "display_name", "format",
                         "synonyms"})
_MEASURE_KEYS = frozenset({"name", "expr", "comment", "display_name",
                           "format", "synonyms", "window"})
_PARAM_KEYS = frozenset({"name", "data_type", "default"})

# A source we can bind a claim to: a 1–3 part identifier chain (backticks
# stripped). Anything else is an inline SQL query — a real feature of metric
# views, but not a nameable relation a claim can attach to.
_RELATION_RE = re.compile(r"^[\w$]+(\.[\w$]+){0,2}$")


def load_uc_metrics(path: str | Path, dialect: str = "duckdb"
                    ) -> tuple[list[DeclaredSpec], list[dict]]:
    """(specs, skipped) for one metric-view YAML or a directory of them.

    Every `skipped` entry is informational — content read but not verified.
    engine.run_scan relies on that (mirrors the ossie adapter's contract).
    """
    root = Path(path)
    if root.is_dir():
        files = sorted(p for p in root.rglob("*") if p.is_file()
                       and p.suffix.lower() in (".yml", ".yaml"))
        if not files:
            raise ValueError(f"{root}: no .yml/.yaml metric view definitions found")
        strict = False
    elif root.is_file():
        files = [root]
        strict = True
    else:
        raise ValueError(f"{root}: not a file or directory")

    specs: list[DeclaredSpec] = []
    skipped: list[dict] = []
    views = 0
    for f in files:
        data = load_yaml(f, expect=object)
        if not isinstance(data, dict) or "source" not in data:
            if strict:
                raise ValueError(
                    f"{f}: not a metric view definition — no `source` key")
            skipped.append(_info(f.stem, str(f),
                                 "not a metric view definition (no `source`) "
                                 "— not read"))
            continue
        views += 1
        _read_view(f, data, dialect, specs, skipped)
    if not views:
        raise ValueError(
            f"{root}: no metric view definitions found — {len(files)} "
            f"file(s) inspected, none carried `source`")
    return specs, skipped


def _info(object_id: str, path: str, reason: str) -> dict:
    return {"object_id": object_id, "relation": None, "path": path,
            "reason": reason, "informational": True}


def _read_view(f: Path, view: dict, dialect: str,
               specs: list[DeclaredSpec], skipped: list[dict]) -> None:
    view_name = f.stem
    version = view.get("version")
    if str(version) not in SUPPORTED_VERSIONS:
        raise ValueError(
            f"{f}: metric view spec version {version!r} is not supported "
            f"(supported: {', '.join(SUPPORTED_VERSIONS)}) — refusing to "
            f"guess at a shape this adapter does not know")

    taints: list[str] = []
    unknown = sorted(set(view) - _TOP_KEYS)
    if unknown:
        taints.append(f"metric view carries unmodelled key(s) "
                      f"{', '.join(unknown)} — cannot rule out semantic impact")

    raw_source = str(view.get("source") or "")
    source = raw_source.replace("`", "").strip()
    if not _RELATION_RE.match(source):
        taints.append("source is an inline SQL query, not a nameable "
                      "relation — claims need a relation to bind to")
    tail = source.split(".")[-1] if source else ""

    join_names: set[str] = set()
    for j, join in enumerate(view.get("joins") or []):
        if not isinstance(join, dict) or not join.get("name"):
            raise ValueError(f"{f}: joins[{j}] has no name")
        join_names.add(str(join["name"]).lower())
        unknown = sorted(set(join) - _JOIN_KEYS)
        if unknown:
            taints.append(f"join {join['name']} carries unmodelled key(s) "
                          f"{', '.join(unknown)} — cannot rule out impact on "
                          f"the view's row universe")
        for nested in join.get("joins") or []:
            if isinstance(nested, dict) and nested.get("name"):
                join_names.add(str(nested["name"]).lower())

    params: set[str] = set()
    for p, param in enumerate(view.get("parameters") or []):
        if not isinstance(param, dict) or not param.get("name"):
            raise ValueError(f"{f}: parameters[{p}] has no name")
        params.add(str(param["name"]))
        unknown = sorted(set(param) - _PARAM_KEYS)
        if unknown:
            taints.append(f"parameter {param['name']} carries unmodelled "
                          f"key(s) {', '.join(unknown)}")

    # Fields matter to claims only as an ambiguity source: a measure naming a
    # declared non-identity field could mean the field OR a physical column.
    risky_fields: set[str] = set()
    field_entries = list(view.get("fields") or []) + list(view.get("dimensions") or [])
    for entry in field_entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{f}: field entry is not an object")
        name, expr = entry.get("name"), entry.get("expr")
        if name is None:
            continue  # wildcard field — expands columns, declares no alias
        unknown = sorted(set(entry) - _FIELD_KEYS)
        identity = (isinstance(expr, str)
                    and _strip_ident(expr) == _strip_ident(str(name)))
        if unknown or not identity:
            risky_fields.add(_strip_ident(str(name)))

    filter_sql = view.get("filter")
    filter_rendered: str | None = None
    filter_reasons: list[str] = []
    if filter_sql is not None:
        filter_rendered, filter_reasons = _translate(
            str(filter_sql), tail, join_names, params, risky_fields, dialect)
        filter_reasons = [f"view filter: {r}" for r in filter_reasons]

    measures = view.get("measures") or []
    for m, measure in enumerate(measures):
        if not isinstance(measure, dict):
            raise ValueError(f"{f}: measures[{m}] is not an object")
        name = measure.get("name")
        expr = measure.get("expr")
        if name is None:
            skipped.append(_info(view_name, str(f),
                                 f"wildcard measure entry ({str(expr)[:60]}) "
                                 f"— expands columns, declares no metric"))
            continue
        where = f"{f}::{view_name}/{name}"
        m_taints = list(taints) + filter_reasons
        unknown = sorted(set(measure) - _MEASURE_KEYS)
        if unknown:
            m_taints.append(f"measure carries unmodelled key(s) "
                            f"{', '.join(unknown)}")
        if measure.get("window"):
            m_taints.append(
                "window measure (order/range/semiadditive semantics) — its "
                "bare aggregate is a different metric than the one declared")
        if not isinstance(expr, str) or not expr.strip():
            m_taints.append("measure declares no expression")
            rendered = None
        else:
            rendered, reasons = _translate(
                expr, tail, join_names, params, risky_fields, dialect)
            m_taints.extend(reasons)
        description = measure.get("comment") if isinstance(
            measure.get("comment"), str) else None
        if m_taints:
            specs.append(DeclaredSpec(
                system="uc_metrics", name=str(name), description=description,
                model=source or None, untranslatable=m_taints, path=where))
        else:
            specs.append(DeclaredSpec(
                system="uc_metrics", name=str(name), description=description,
                model=source, expr=rendered,
                filters=[filter_rendered] if filter_rendered else [],
                path=where))

    notes = []
    if view.get("joins"):
        notes.append(f"{len(view['joins'])} join(s) (with cardinality) — the "
                     f"join graph is not verified")
    if params:
        notes.append(f"{len(params)} parameter(s)")
    if view.get("materialization"):
        notes.append("materialization schedule (serving config, not meaning)")
    if notes:
        skipped.append(_info(view_name, str(f),
                             "declared but not verified by the uc_metrics "
                             "adapter: " + "; ".join(notes)))
    if not measures:
        skipped.append(_info(view_name, str(f),
                             "metric view declares no measures (fields only)"))


def _strip_ident(text: str) -> str:
    return text.strip().strip("`").strip().lower()


def _translate(text: str, tail: str, join_names: set[str], params: set[str],
               risky_fields: set[str], dialect: str
               ) -> tuple[str | None, list[str]]:
    """Render one metric-view expression for the scan dialect.

    Parsed as Databricks SQL — that is the language the YAML is written in
    regardless of what dialect the surrounding scan uses. `source.` (the
    YAML's name for the base table) is rewritten to the table's own name so
    the synthesized claim's FROM clause binds it.
    """
    used_params = {p for p in params if re.search(rf"(?<!\w):{re.escape(p)}\b", text)}
    if used_params:
        return None, [f"expression is parameterized (:"
                      f"{', :'.join(sorted(used_params))}) — a parameter "
                      f"makes this a family of metrics, not one claim"]
    try:
        node = sqlglot.parse_one(text, read="databricks")
    except Exception as e:  # noqa: BLE001 — declared text is arbitrary
        return None, [f"could not parse expression: {e}"]
    if node is None or isinstance(node, exp.Query):
        return None, ["expression is a full query — expected a scalar expression"]

    reasons: list[str] = []
    for col in node.find_all(exp.Column):
        qualifier = col.table.lower() if col.table else ""
        if not qualifier:
            name = col.name.lower()
            if name in risky_fields:
                reasons.append(
                    f"references declared field {col.name}, whose expression "
                    f"is not the identity column — field-vs-column resolution "
                    f"order is engine-internal, refusing to guess")
            continue
        if qualifier in join_names:
            reasons.append(f"references joined table {col.table} — declared "
                           f"join application is not yet supported")
        elif qualifier not in ("source", tail.lower()):
            reasons.append(f"references {col.table}.{col.name}, which is "
                           f"neither `source` nor the source table")
    if reasons:
        return None, sorted(set(reasons))

    for col in node.find_all(exp.Column):
        if col.table:
            col.set("catalog", None)
            col.set("db", None)
            col.set("table", exp.to_identifier(tail))
    return node.sql(dialect=dialect), []
