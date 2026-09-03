"""Apache Ossie adapter (§2): osi documents as declared claims.

Reads Ossie (formerly Open Semantic Interchange) semantic-model documents —
the vendor-neutral interchange format incubating at the ASF, emitted by dbt
Core v1.12+ as `target/osi_document.json` and authored by hand under `osi/`.
Spec versions 0.1.0 and 0.1.1 (the released line, the same set dbt accepts)
are read; anything else is refused by name — the spec is incubating, and a
guessed read of a future shape is a fabricated claim.

Three properties matter here:

* MULTI-DIALECT METRICS BECOME SIBLING CLAIMS. An Ossie metric may carry one
  expression per dialect (ANSI_SQL, SNOWFLAKE, ...). Each SQL variant is
  parsed under ITS OWN dialect, rendered in the scan's dialect, and emitted
  as a separate DeclaredSpec sharing the metric's name — so concept
  resolution puts the variants in one concept, and reconciliation's existing
  declared-vs-declared comparison decides whether the vendor variants agree.
  Cross-dialect drift (the Snowflake text quietly diverging from the ANSI
  text) surfaces as a CONTRADICTION with no new verdict machinery.

* NON-SQL VARIANTS BLOCK, NEVER VANISH. MDX / TABLEAU / MAQL expressions are
  declarations we cannot read. Each becomes an inexpressible claim naming
  itself, which qualifies the concept — certifying "all declared variants
  agree" while one sat unread would be a false certification.

* DENY BY DEFAULT. A key this adapter does not model — on the document, the
  semantic model, a dataset, a field, or a metric — taints every metric it
  encloses, because an unread key at any of those levels can change what the
  metric MEANS (the rule extraction.completeness applies to SQL args, applied
  to intake). `custom_extensions` are spec-defined VENDOR METADATA, so they
  block only where they sit on the definition itself (the metric, or a field
  or dataset the metric reads); on outer levels they are disclosed
  informationally. Everything else unread is disclosed too: relationships,
  key declarations, datasets-only models.

Reading source documents directly also restores what dbt's own intake drops:
constructs dbt skips with warning I078 still appear here, as refusals with
reasons instead of log lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from pathlib import Path

import sqlglot
from sqlglot import exp

from semantix.adapters.base import DeclaredSpec, informational_skip as _info
from semantix.adapters.loadutil import load_json, load_yaml
from semantix.extraction.sqlutil import requalify_columns

SUPPORTED_VERSIONS = ("0.1.0", "0.1.1")

# Ossie dialect enum -> sqlglot read dialect. None means sqlglot's
# dialect-neutral parser (the spec's ANSI_SQL baseline). A dialect in
# neither map is refused by name: the enum will grow, and a new SQL dialect
# read under the wrong grammar is a silently different expression.
_SQL_DIALECTS: dict[str, str | None] = {
    "ANSI_SQL": None,
    "SNOWFLAKE": "snowflake",
    "DATABRICKS": "databricks",
    "BIGQUERY": "bigquery",
}
_NON_SQL_DIALECTS = frozenset({"MDX", "TABLEAU", "MAQL"})

# Deny-by-default key vocabulary (spec 0.1.x object shapes).
_DOC_KEYS = frozenset({"version", "semantic_model"})
_MODEL_KEYS = frozenset({"name", "description", "ai_context", "datasets",
                         "relationships", "metrics", "custom_extensions"})
_DATASET_KEYS = frozenset({"name", "source", "primary_key", "unique_keys",
                           "description", "ai_context", "fields",
                           "custom_extensions"})
_FIELD_KEYS = frozenset({"name", "expression", "dimension", "label",
                         "description", "datatype", "ai_context",
                         "custom_extensions"})
_METRIC_KEYS = frozenset({"name", "expression", "description", "datatype",
                          "ai_context", "custom_extensions"})
_EXPRESSION_KEYS = frozenset({"dialects"})
_VARIANT_KEYS = frozenset({"dialect", "expression"})


@dataclass
class _Field:
    variants: dict[str, str] = dc_field(default_factory=dict)
    taints: list[str] = dc_field(default_factory=list)


@dataclass
class _Dataset:
    name: str
    source: str
    tail: str
    fields: dict[str, _Field] = dc_field(default_factory=dict)
    taints: list[str] = dc_field(default_factory=list)


def load_ossie(path: str | Path, dialect: str = "duckdb"
               ) -> tuple[list[DeclaredSpec], list[dict]]:
    """(specs, skipped) for a single Ossie document, a dbt
    `target/osi_document.json`, or a directory tree of documents (`osi/`).

    Every entry in `skipped` is informational — content that WAS read but is
    not verified by this adapter (relationships, key declarations, vendor
    extensions on outer levels). engine.run_scan relies on that: these
    entries must never represent unread estate.
    """
    root = Path(path)
    if root.is_dir():
        files = sorted(p for p in root.rglob("*") if p.is_file()
                       and p.suffix.lower() in (".json", ".yml", ".yaml"))
        if not files:
            raise ValueError(
                f"{root}: no .json/.yaml files found — is this an osi/ "
                f"directory or a dbt target/ directory?")
        strict = False
    elif root.is_file():
        files = [root]
        strict = True
    else:
        raise ValueError(f"{root}: not a file or directory")

    specs: list[DeclaredSpec] = []
    skipped: list[dict] = []
    documents = 0
    for f in files:
        suffix = f.suffix.lower()
        if suffix == ".json":
            data = load_json(f, expect=object)
        elif suffix in (".yml", ".yaml"):
            data = load_yaml(f, expect=object)
        else:
            raise ValueError(f"{f}: expected a .json or .yaml Ossie document")
        docs = _unwrap(f, data, strict, skipped)
        for doc in docs:
            documents += 1
            _read_document(f, doc, dialect, specs, skipped)
    if not documents:
        raise ValueError(
            f"{root}: no Ossie documents found — {len(files)} file(s) "
            f"inspected, none carried `version` + `semantic_model`")
    return specs, skipped


def _unwrap(f: Path, data: object, strict: bool, skipped: list[dict]) -> list[dict]:
    """Ossie documents inside one file: the document itself, or dbt's
    `osi_document.json` wrapper carrying several under `documents`."""
    if isinstance(data, dict):
        if "documents" in data:
            inner = data["documents"]
            if isinstance(inner, dict):
                entries = [inner[k] for k in sorted(inner)]
            elif isinstance(inner, list):
                entries = inner
            else:
                raise ValueError(
                    f"{f}: `documents` must be a list or mapping of Ossie "
                    f"documents, found {type(inner).__name__}")
            for i, entry in enumerate(entries):
                if not isinstance(entry, dict) or "semantic_model" not in entry:
                    raise ValueError(
                        f"{f}: documents[{i}] is not an Ossie document "
                        f"(no `semantic_model`)")
            # The wrapper is dbt transport, not spec content; its other keys
            # (metadata, versions) do not change the documents, but they are
            # named rather than silently passed over.
            extra = sorted(set(data) - {"documents"})
            if extra:
                skipped.append(_info(f.name, str(f),
                                     "wrapper keys not read: " + ", ".join(extra)))
            return entries
        if "semantic_model" in data:
            return [data]
        if strict:
            raise ValueError(
                f"{f}: not an Ossie document — top-level keys "
                f"{sorted(data)[:8]} carry no `semantic_model`")
        skipped.append(_info(f.name, str(f),
                             "not an Ossie document (no `semantic_model`) — not read"))
        return []
    if isinstance(data, list) and data and all(
            isinstance(d, dict) and "semantic_model" in d for d in data):
        return data
    if strict:
        raise ValueError(
            f"{f}: not an Ossie document — expected an object with "
            f"`version` and `semantic_model`")
    skipped.append(_info(f.name, str(f),
                         "not an Ossie document (no `semantic_model`) — not read"))
    return []


def _read_document(f: Path, doc: dict, dialect: str,
                   specs: list[DeclaredSpec], skipped: list[dict]) -> None:
    version = doc.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(
            f"{f}: Ossie version {version!r} is not supported (supported: "
            f"{', '.join(SUPPORTED_VERSIONS)}) — the spec is incubating and "
            f"this adapter will not guess at a shape it does not know")

    doc_taints = [f"document carries unmodelled key(s) "
                  f"{', '.join(sorted(set(doc) - _DOC_KEYS))} — cannot rule "
                  f"out semantic impact"] if set(doc) - _DOC_KEYS else []

    models = doc.get("semantic_model")
    if not isinstance(models, list):
        raise ValueError(f"{f}: `semantic_model` must be a list, found "
                         f"{type(models).__name__}")
    for i, model in enumerate(models):
        if not isinstance(model, dict):
            raise ValueError(f"{f}: semantic_model[{i}] is not an object")
        name = model.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"{f}: semantic_model[{i}] has no name — "
                             f"cannot attribute its declarations")
        _read_model(f, name, model, doc_taints, dialect, specs, skipped)


def _read_model(f: Path, model_name: str, model: dict, doc_taints: list[str],
                dialect: str, specs: list[DeclaredSpec],
                skipped: list[dict]) -> None:
    taints = list(doc_taints)
    unknown = sorted(set(model) - _MODEL_KEYS)
    if unknown:
        taints.append(f"semantic model carries unmodelled key(s) "
                      f"{', '.join(unknown)} — cannot rule out semantic impact")

    ds_by_name: dict[str, _Dataset] = {}
    ds_by_tail: dict[str, list[_Dataset]] = {}
    key_decls = 0
    for j, raw in enumerate(model.get("datasets") or []):
        if not isinstance(raw, dict):
            raise ValueError(f"{f}: {model_name} datasets[{j}] is not an object")
        ds = _read_dataset(f, model_name, j, raw)
        ds_by_name[ds.name.lower()] = ds
        ds_by_tail.setdefault(ds.tail.lower(), []).append(ds)
        if raw.get("primary_key") or raw.get("unique_keys"):
            key_decls += 1

    metrics = model.get("metrics") or []
    for k, metric in enumerate(metrics):
        if not isinstance(metric, dict):
            raise ValueError(f"{f}: {model_name} metrics[{k}] is not an object")
        m_name = metric.get("name")
        if not isinstance(m_name, str) or not m_name:
            raise ValueError(f"{f}: {model_name} metrics[{k}] has no name — "
                             f"cannot attribute the claim")
        specs.extend(_metric_specs(f, model_name, m_name, metric,
                                   ds_by_name, ds_by_tail, taints, dialect))

    # Declared content this adapter reads but does not verify — disclosed,
    # never silently passed over.
    notes = []
    rels = model.get("relationships") or []
    if rels:
        notes.append(f"{len(rels)} relationship(s) (verify via mine/verify-relationships)")
    if key_decls:
        notes.append(f"key declarations on {key_decls} dataset(s)")
    vendors = sorted({str(e.get("vendor_name"))
                      for e in (model.get("custom_extensions") or [])
                      if isinstance(e, dict)})
    if vendors:
        notes.append(f"model-level custom extensions from {', '.join(vendors)}")
    if notes:
        skipped.append(_info(model_name, str(f),
                             "declared but not verified by the ossie adapter: "
                             + "; ".join(notes)))
    if not metrics:
        skipped.append(_info(model_name, str(f),
                             "semantic model declares no metrics (datasets only)"))


def _read_dataset(f: Path, model_name: str, idx: int, raw: dict) -> _Dataset:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{f}: {model_name} datasets[{idx}] has no name")
    source = raw.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ValueError(f"{f}: dataset {name!r} in {model_name} has no "
                         f"source — its metrics would bind to nothing")
    source = source.replace('"', "").replace("`", "").strip()
    ds = _Dataset(name=name, source=source, tail=source.split(".")[-1])

    unknown = sorted(set(raw) - _DATASET_KEYS)
    if unknown:
        ds.taints.append(f"dataset {name} carries unmodelled key(s) "
                         f"{', '.join(unknown)} — cannot rule out semantic impact")
    vendors = sorted({str(e.get("vendor_name"))
                      for e in (raw.get("custom_extensions") or [])
                      if isinstance(e, dict)})
    if vendors:
        ds.taints.append(f"dataset {name} carries custom extensions from "
                         f"{', '.join(vendors)} — content not readable")

    for fi, fraw in enumerate(raw.get("fields") or []):
        if not isinstance(fraw, dict):
            raise ValueError(f"{f}: dataset {name} fields[{fi}] is not an object")
        f_name = fraw.get("name")
        if not isinstance(f_name, str) or not f_name:
            raise ValueError(f"{f}: dataset {name} fields[{fi}] has no name")
        fld = _Field()
        unknown = sorted(set(fraw) - _FIELD_KEYS)
        if unknown:
            fld.taints.append(f"field {f_name} carries unmodelled key(s) "
                              f"{', '.join(unknown)}")
        vendors = sorted({str(e.get("vendor_name"))
                          for e in (fraw.get("custom_extensions") or [])
                          if isinstance(e, dict)})
        if vendors:
            fld.taints.append(f"field {f_name} carries custom extensions from "
                              f"{', '.join(vendors)} — content not readable")
        fld.variants = _variant_map(fraw.get("expression"), fld.taints,
                                    f"field {f_name}")
        ds.fields[f_name.lower()] = fld
    return ds


def _variant_map(expression: object, taints: list[str], where: str) -> dict[str, str]:
    """dialect name -> expression text, appending taints for anything the
    0.1.x Expression shape does not allow."""
    if not isinstance(expression, dict):
        taints.append(f"{where} declares no expression object")
        return {}
    unknown = sorted(set(expression) - _EXPRESSION_KEYS)
    if unknown:
        taints.append(f"{where} expression carries unmodelled key(s) "
                      f"{', '.join(unknown)}")
    out: dict[str, str] = {}
    entries = expression.get("dialects")
    if not isinstance(entries, list) or not entries:
        taints.append(f"{where} expression carries no dialect variants")
        return out
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            taints.append(f"{where} dialects[{i}] is not an object")
            continue
        unknown = sorted(set(entry) - _VARIANT_KEYS)
        if unknown:
            taints.append(f"{where} dialects[{i}] carries unmodelled key(s) "
                          f"{', '.join(unknown)}")
        d, text = entry.get("dialect"), entry.get("expression")
        if not isinstance(d, str) or not isinstance(text, str) or not text.strip():
            taints.append(f"{where} dialects[{i}] is missing dialect or expression")
            continue
        if d in out and out[d] != text:
            taints.append(f"{where} declares dialect {d} twice with "
                          f"different expressions")
            continue
        out[d] = text
    return out


def _metric_specs(f: Path, model_name: str, m_name: str, metric: dict,
                  ds_by_name: dict[str, _Dataset],
                  ds_by_tail: dict[str, list[_Dataset]],
                  base_taints: list[str], dialect: str) -> list[DeclaredSpec]:
    where = f"{f}::{model_name}/{m_name}"
    description = metric.get("description") if isinstance(
        metric.get("description"), str) else None

    taints = list(base_taints)
    unknown = sorted(set(metric) - _METRIC_KEYS)
    if unknown:
        taints.append(f"metric carries unmodelled key(s) {', '.join(unknown)} "
                      f"— cannot rule out semantic impact")
    vendors = sorted({str(e.get("vendor_name"))
                      for e in (metric.get("custom_extensions") or [])
                      if isinstance(e, dict)})
    if vendors:
        # This is where a vendor round-trip stashes what the spec cannot say
        # (dbt: the MetricFlow config it would otherwise drop with I078) —
        # exactly the content that makes the spec'd expression an incomplete
        # statement of the metric's meaning.
        taints.append(f"metric carries custom extensions from "
                      f"{', '.join(vendors)} — content not readable, and it "
                      f"may qualify the metric's meaning")
    variants = _variant_map(metric.get("expression"), taints, "metric")

    if taints:
        return [DeclaredSpec(system="ossie", name=m_name,
                             description=description,
                             untranslatable=taints, path=where)]

    out: list[DeclaredSpec] = []
    merged: dict[tuple[str | None, str], DeclaredSpec] = {}
    for d_name, text in variants.items():
        if d_name in _NON_SQL_DIALECTS:
            out.append(DeclaredSpec(
                system="ossie", name=m_name, description=description,
                untranslatable=[f"{d_name} expression is not SQL and cannot "
                                f"be verified against the estate: {text[:120]}"],
                path=f"{where}@{d_name}"))
            continue
        if d_name not in _SQL_DIALECTS:
            out.append(DeclaredSpec(
                system="ossie", name=m_name, description=description,
                untranslatable=[f"unknown expression dialect {d_name!r} — "
                                f"reading it under a guessed grammar would "
                                f"verify a different expression"],
                path=f"{where}@{d_name}"))
            continue
        expr, source, reasons = _translate(
            text, _SQL_DIALECTS[d_name], d_name, ds_by_name, ds_by_tail, dialect)
        if reasons:
            out.append(DeclaredSpec(
                system="ossie", name=m_name, description=description,
                model=source,
                untranslatable=[f"{d_name}: {r}" for r in reasons],
                path=f"{where}@{d_name}"))
            continue
        key = (source, expr)
        if key in merged:
            # Two dialects rendering to the same scan-dialect expression are
            # provably one claim; one spec carries both names.
            merged[key].path += f"+{d_name}"
            continue
        spec = DeclaredSpec(system="ossie", name=m_name,
                            description=description, model=source, expr=expr,
                            path=f"{where}@{d_name}")
        merged[key] = spec
        out.append(spec)
    return out


def _translate(text: str, read: str | None, d_name: str,
               ds_by_name: dict[str, _Dataset],
               ds_by_tail: dict[str, list[_Dataset]],
               dialect: str) -> tuple[str | None, str | None, list[str]]:
    """(scan-dialect expression, source relation, refusal reasons).

    Faithful mechanics only: parse under the variant's own grammar, resolve
    dataset references against the document's declarations, substitute
    declared field expressions, and render for the scan dialect. Anything
    short of certain — an unknown qualifier, a metric spanning datasets, a
    field with no readable expression — is a refusal with the reason, never
    a guess.
    """
    try:
        node = sqlglot.parse_one(text, read=read)
    except Exception as e:  # noqa: BLE001 — declared text is arbitrary
        return None, None, [f"could not parse expression: {e}"]
    if node is None:
        return None, None, ["could not parse expression"]
    if isinstance(node, exp.Query):
        return None, None, ["expression is a full query — expected a scalar "
                            "metric expression"]

    columns = list(node.find_all(exp.Column))
    resolved: dict[int, _Dataset] = {}
    unqualified: list[exp.Column] = []
    reasons: list[str] = []
    for col in columns:
        if col.args.get("catalog") or col.args.get("db"):
            fq = ".".join(p for p in (
                col.args.get("catalog") and col.args["catalog"].name,
                col.args.get("db") and col.args["db"].name,
                col.table) if p).lower()
            match = [ds for ds in ds_by_name.values() if ds.source.lower() == fq]
            if len(match) == 1:
                resolved[id(col)] = match[0]
            else:
                reasons.append(f"references {fq}.{col.name}, which is not a "
                               f"declared dataset's source")
        elif col.table:
            t = col.table.lower()
            ds = ds_by_name.get(t)
            if ds is None:
                tails = ds_by_tail.get(t) or []
                if len(tails) == 1:
                    ds = tails[0]
                elif len(tails) > 1:
                    reasons.append(f"table name {col.table} is the source of "
                                   f"{len(tails)} datasets — ambiguous")
                    continue
            if ds is None:
                reasons.append(f"references {col.table}.{col.name}, and "
                               f"{col.table} is neither a declared dataset "
                               f"nor a dataset's table name")
                continue
            resolved[id(col)] = ds
        else:
            unqualified.append(col)
    if reasons:
        return None, None, sorted(set(reasons))

    targets = {ds.name: ds for ds in resolved.values()}
    if len(targets) > 1:
        return None, None, [
            f"metric spans datasets {', '.join(sorted(targets))} — "
            f"cross-dataset metrics need the declared join graph, which this "
            f"adapter does not yet apply"]
    if not targets:
        if len(ds_by_name) == 1:
            targets = {ds.name: ds for ds in ds_by_name.values()}
        else:
            what = (f"unqualified column {unqualified[0].name}" if unqualified
                    else "no column references")
            return None, None, [
                f"{what} with {len(ds_by_name)} datasets declared — cannot "
                f"attribute the metric to one dataset"]
    ds = next(iter(targets.values()))
    if ds.taints:
        return None, ds.source, list(ds.taints)

    for col in columns:
        fld = ds.fields.get(col.name.lower())
        if fld is None:
            continue  # a physical column of the source, not a declared field
        if fld.taints:
            return None, ds.source, list(fld.taints)
        if d_name in fld.variants:
            f_text, f_read = fld.variants[d_name], read
        elif "ANSI_SQL" in fld.variants:
            f_text, f_read = fld.variants["ANSI_SQL"], None
        else:
            return None, ds.source, [
                f"field {col.name} has no {d_name} or ANSI_SQL expression "
                f"(declared: {', '.join(sorted(fld.variants)) or 'none'})"]
        if f_text.strip().lower() == col.name.lower():
            continue  # identity field: the column is its own expression
        try:
            sub = sqlglot.parse_one(f_text, read=f_read)
        except Exception as e:  # noqa: BLE001
            return None, ds.source, [
                f"field {col.name} expression could not be parsed: {e}"]
        replacement = sub if isinstance(sub, exp.Column) else exp.Paren(this=sub)
        if col is node:
            node = replacement
        else:
            col.replace(replacement)

    # Qualifiers now provably mean the one dataset; rewrite them to the name
    # the synthesized claim's FROM clause will carry, so qualification binds.
    requalify_columns(node, ds.tail)
    return node.sql(dialect=dialect), ds.source, []
