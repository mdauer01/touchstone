"""Declarations from canonical cores: the shared emitter.

Every resolution path that ends in "write the definition down" — a
resolution PR that updates a declaration to match the code, promoting a
shadow metric into the ontology, an Ossie export of certified definitions —
needs the same thing: a declaration, in some format, rendered from a
SemanticCore. This module is that one renderer.

The core is canonical and name-free (`SUM(t0."amount")`, filters and grain
as canonical strings, the join graph as relations). Rendering it back into a
declaration is mechanical only for what the target format can EXPRESS, and
the formats differ:

* metrics YAML (semantix native): expr + WHERE-style filters + group_by over
  one model.
* Unity Catalog metric view: measure + view-level filter + grain as fields.
* Apache Ossie 0.1.x: a metric expression with no filter and no grain — so
  a filtered or grained core is REFUSED rather than folded into a CASE
  expression that would declare a different metric.

Two refusals apply to every format: a core spanning more than one relation
(the single-model formats cannot carry the join graph), and post-aggregate
or bound filters (HAVING / QUALIFY / DISTINCT tags), which have no WHERE
equivalent. Refusals carry reasons; nothing is approximated.

Invariant this module is tested against: a declaration emitted from a
fingerprint, scanned back in, CERTIFIES against that fingerprint.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

import sqlglot
import yaml
from sqlglot import exp

from semantix.extraction.sqlutil import requalify_columns
from semantix.models import SemanticCore

FORMATS = ("metrics", "ossie", "uc-metrics")

# Canonical filter tags with no WHERE-declaration equivalent
# (extraction.fingerprint renders these prefixes).
_TAGGED = ("HAVING ", "QUALIFY ", "DISTINCT")
_PLAIN_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass
class Emitted:
    format: str
    text: str

    @property
    def suffix(self) -> str:
        return ".json" if self.format == "ossie" else ".yml"


def emit_declaration(name: str, core: SemanticCore, dialect: str, fmt: str
                     ) -> tuple[Emitted | None, list[str]]:
    """(declaration, refusal reasons). Exactly one of the two is populated."""
    if fmt not in FORMATS:
        raise ValueError(f"unknown declaration format {fmt!r} "
                         f"(choose from {', '.join(FORMATS)})")
    reasons: list[str] = []
    nodes = list(core.join_nodes)
    if len(nodes) != 1 or nodes[0].startswith("cte:") or core.join_edges:
        reasons.append(
            f"reads {len(nodes)} relation(s) ({', '.join(nodes) or 'none'}) — "
            f"single-model declaration formats cannot express the join graph")
    for f in core.filters:
        if f.startswith(_TAGGED):
            reasons.append(
                f"filter `{f[:60]}` is post-aggregate or bound (HAVING / "
                f"QUALIFY / DISTINCT) — declaration formats express WHERE-"
                f"style filters only")
    if reasons:
        return None, reasons

    model = nodes[0]
    write = {"metrics": dialect, "ossie": None, "uc-metrics": "databricks"}[fmt]
    qualifier = model.split(".")[-1] if fmt == "ossie" else None
    try:
        expr = _rewrite(core.measure_expr, dialect, write, qualifier)
        filters = [_rewrite(f, dialect, write, qualifier) for f in core.filters]
        group_by = [_rewrite(g, dialect, write, qualifier) for g in core.grain]
    except Exception as e:  # noqa: BLE001 — canonical text is ours, but stay honest
        return None, [f"could not re-render the canonical core: {e}"]

    if fmt == "metrics":
        entry: dict = {"name": name, "model": model, "expr": expr}
        if filters:
            entry["filters"] = filters
        if group_by:
            entry["group_by"] = group_by
        text = yaml.safe_dump({"version": 1, "metrics": [entry]},
                              sort_keys=False, width=100)
    elif fmt == "uc-metrics":
        doc: dict = {"version": "1.1", "source": model}
        if filters:
            doc["filter"] = " AND ".join(f"({f})" for f in filters)
        if group_by:
            doc["fields"] = [{"name": _field_name(g, i), "expr": g}
                             for i, g in enumerate(group_by, 1)]
        doc["measures"] = [{"name": name, "expr": expr}]
        text = yaml.safe_dump(doc, sort_keys=False, width=100)
    else:  # ossie
        if filters or group_by:
            return None, [
                "Ossie 0.1.x metrics carry no filter or grain — folding "
                f"{len(filters)} filter(s) and {len(group_by)} grain term(s) "
                "into the expression would declare a different metric"]
        doc = {
            "version": "0.1.1",
            "semantic_model": [{
                "name": f"{qualifier}_model",
                "datasets": [{"name": qualifier, "source": model}],
                "metrics": [{
                    "name": name,
                    "expression": {"dialects": [
                        {"dialect": "ANSI_SQL", "expression": expr}]},
                }],
            }],
        }
        text = json.dumps(doc, indent=2) + "\n"
    return Emitted(format=fmt, text=text), []


def _rewrite(text: str, read: str, write: str | None, qualifier: str | None) -> str:
    """Canonical `t0."col"` references -> bare (or dataset-qualified) columns,
    unquoted where the identifier is plain, rendered for the target dialect."""
    node = sqlglot.parse_one(text, read=read)
    requalify_columns(node, qualifier, only_qualified=False)
    for col in node.find_all(exp.Column):
        ident = col.this
        if isinstance(ident, exp.Identifier) and _PLAIN_IDENT.match(ident.name):
            ident.set("quoted", False)
    return node.sql(dialect=write)


def _field_name(expr_text: str, index: int) -> str:
    from semantix.services.resolution import normalize_name

    return normalize_name(expr_text)[:48] or f"grain_{index}"
