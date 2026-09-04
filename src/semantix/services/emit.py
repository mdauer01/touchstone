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
* Unity Catalog metric view: measure + view-level filter. A metric-view
  measure has NO fixed grain — the view's fields are optional groupings a
  query may or may not apply — so a grained core is refused; writing the
  grain as `fields` looked right and rescanned as a CONTRADICTION.
* Apache Ossie 0.1.x: a metric expression with no filter and no grain — a
  filtered or grained core is REFUSED rather than folded into a CASE
  expression that would declare a different metric.

Refusals that apply to every format: a core spanning more than one relation
(the single-model formats cannot carry the join graph); post-aggregate,
bound or distinct filters (HAVING / QUALIFY / BOUND / SELECT DISTINCT tags,
which have no WHERE equivalent — the DISTINCT tag passed through and was
emitted AS a WHERE filter); ROLLUP / CUBE / GROUPING SETS grain; subqueries
inside the expression (stripping their qualifiers turned a correlated count
into a cross join); struct paths that look like table qualifiers; and a
model name that cannot be written unquoted.

The last line of defense is the SELF-CHECK: when a registry is available,
the emitted text is loaded back through the target format's own adapter,
canonicalized with `build_claim`, and its core hash compared with the
source core. Transpilation is not identity — Ossie's generic writer wrapped
every division in NULLIF, the Databricks writer inverted EPOCH_MS — and
without the check those declarations were presented as "what the code
computes today". A declaration that does not re-canonicalize to its own
source is refused with that reason. Without a registry the result is
marked unverified, and the memo says so.

Invariant this module is tested against: a declaration emitted from a
fingerprint, scanned back in through its own adapter, CERTIFIES against
that fingerprint — for every format that emits.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import sqlglot
import yaml
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from semantix.extraction.sqlutil import requalify_columns
from semantix.models import SemanticCore

FORMATS = ("metrics", "ossie", "uc-metrics")

# Canonical filter tags with no WHERE-declaration equivalent
# (extraction.fingerprint renders these prefixes).
_TAGGED = ("HAVING ", "QUALIFY ", "BOUND ", "SELECT DISTINCT")
_GRAIN_TAGS = ("ROLLUP(", "CUBE(", "GROUPING SETS(")
_PLAIN_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_RELATION_RE = re.compile(r"^[\w$]+(\.[\w$]+){0,2}$")
_NEVER_UNQUOTE = {"true", "false", "null"}
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class _Unrenderable(Exception):
    """A core element the target text cannot carry faithfully."""


@dataclass
class Emitted:
    format: str
    text: str
    # True: re-canonicalized through the target adapter to the source core.
    # None: no registry was available to check with — say so downstream.
    verified: bool | None = None

    @property
    def suffix(self) -> str:
        return ".json" if self.format == "ossie" else ".yml"


def emit_declaration(name: str, core: SemanticCore, dialect: str, fmt: str,
                     registry=None) -> tuple[Emitted | None, list[str]]:
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
                f"filter `{f[:60]}` is post-aggregate, bound or distinct "
                f"(HAVING / QUALIFY / BOUND / DISTINCT) — declaration formats "
                f"express WHERE-style filters only")
    for g in core.grain:
        if g.upper().startswith(_GRAIN_TAGS):
            reasons.append(f"grain `{g[:60]}` is a grouping-set construct — "
                           f"declaration formats express plain group_by only")
    if fmt == "ossie" and (core.filters or core.grain):
        reasons.append(
            "Ossie 0.1.x metrics carry no filter or grain — folding "
            f"{len(core.filters)} filter(s) and {len(core.grain)} grain term(s) "
            "into the expression would declare a different metric")
    if fmt == "uc-metrics" and core.grain:
        reasons.append(
            "metric-view measures carry no fixed grain (fields are optional "
            "groupings) — a grained core has no faithful metric-view form")
    if fmt != "metrics" and registry is None:
        # Ossie and metric-view emission transpile through the generic and
        # Databricks writers; without the registry there is no self-check,
        # and unverified transpiles have been shown to change meaning
        # (NULLIF wrapping, inverted EPOCH functions). Refuse, do not guess.
        reasons.append(f"emitting {fmt} requires the scan's registry for a "
                       f"round-trip self-check — transpilation is not identity")
    if reasons:
        return None, reasons
    model = nodes[0]
    if not _RELATION_RE.match(model):
        return None, [f"relation {model!r} cannot be written as an unquoted "
                      f"name in a declaration"]
    if registry is not None:
        agg_cols = {c.lower() for c in registry.aggregate_outputs(model)}
        if agg_cols & _columns(core.measure_expr, dialect):
            return None, [f"re-aggregates the aggregate output of {model} — "
                          f"the engine refuses such declarations by design; "
                          f"declare over the model's sources instead"]

    qualifier = model.split(".")[-1] if fmt == "ossie" else None
    # Ossie's expression dialect is ANSI_SQL; the generic writer is tried
    # first, the scan dialect second — the self-check decides.
    writers = {"metrics": [dialect], "uc-metrics": ["databricks"],
               "ossie": [None, dialect]}[fmt]
    last_reason = "no renderer produced a faithful declaration"
    for write in writers:
        try:
            expr = _rewrite(core.measure_expr, dialect, write, qualifier)
            filters = [_rewrite(f, dialect, write, qualifier) for f in core.filters]
            group_by = [_rewrite(g, dialect, write, qualifier) for g in core.grain]
        except _Unrenderable as e:
            return None, [str(e)]
        except Exception as e:  # noqa: BLE001 — canonical text is ours, but stay honest
            last_reason = f"could not re-render the canonical core: {_ANSI.sub('', str(e))}"
            continue
        text = _render(fmt, name, model, qualifier, expr, filters, group_by)
        if registry is None:
            return Emitted(format=fmt, text=text, verified=None), []
        if _self_check(text, fmt, name, core, registry, dialect):
            return Emitted(format=fmt, text=text, verified=True), []
        last_reason = ("emitted declaration does not re-canonicalize to the "
                       "computed core (self-check) — rendering for this format "
                       "changed the meaning")
    return None, [last_reason]


def _render(fmt: str, name: str, model: str, qualifier: str | None,
            expr: str, filters: list[str], group_by: list[str]) -> str:
    if fmt == "metrics":
        entry: dict = {"name": name, "model": model, "expr": expr}
        if filters:
            entry["filters"] = filters
        if group_by:
            entry["group_by"] = group_by
        return yaml.safe_dump({"version": 1, "metrics": [entry]},
                              sort_keys=False, width=100)
    if fmt == "uc-metrics":
        doc: dict = {"version": "1.1", "source": model}
        if filters:
            doc["filter"] = " AND ".join(f"({f})" for f in filters)
        doc["measures"] = [{"name": name, "expr": expr}]
        return yaml.safe_dump(doc, sort_keys=False, width=100)
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
    return json.dumps(doc, indent=2) + "\n"


def _rewrite(text: str, read: str, write: str | None, qualifier: str | None) -> str:
    """Canonical `t0."col"` references -> bare (or dataset-qualified) columns,
    unquoted where the identifier is plain AND not a keyword of the target
    dialect, rendered for the target dialect."""
    node = sqlglot.parse_one(text, read=read)
    if node.find(exp.Query) is not None or node.find(exp.Subquery) is not None:
        raise _Unrenderable(
            "expression contains a subquery — stripping its qualifiers turns a "
            "correlated subquery into an uncorrelated one, a different metric")
    for col in node.find_all(exp.Column):
        if col.args.get("db") or col.args.get("catalog"):
            raise _Unrenderable(
                f"column reference {col.sql()} has more than one qualifier — a "
                f"struct path would be rewritten as a table qualifier")
    requalify_columns(node, qualifier, only_qualified=False)
    dcls = Dialect.get_or_raise(write) if write else Dialect
    keywords = {k.upper() for k in dcls.tokenizer_class.KEYWORDS}
    for col in node.find_all(exp.Column):
        ident = col.this
        if not isinstance(ident, exp.Identifier):
            continue
        name = ident.name
        if (_PLAIN_IDENT.match(name) and name.upper() not in keywords
                and name not in _NEVER_UNQUOTE and not name.startswith("current_")):
            ident.set("quoted", False)
    return node.sql(dialect=write)


def _columns(expr_text: str, dialect: str) -> set[str]:
    try:
        node = sqlglot.parse_one(expr_text, read=dialect)
    except Exception:  # noqa: BLE001
        return set()
    return {c.name.lower() for c in node.find_all(exp.Column)}


def _self_check(text: str, fmt: str, name: str, core: SemanticCore,
                registry, dialect: str) -> bool:
    """Load the emitted text through the target format's OWN adapter, build
    the claim with the scan's registry, and require core-hash equality."""
    from semantix.engine import build_claim

    suffix = ".json" if fmt == "ossie" else ".yml"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"emitted{suffix}"
        path.write_text(text, encoding="utf-8")
        try:
            if fmt == "metrics":
                from semantix.adapters.metrics_yaml import load_metrics
                specs = load_metrics(path)
            elif fmt == "ossie":
                from semantix.adapters.ossie import load_ossie
                specs = load_ossie(path, dialect)[0]
            else:
                from semantix.adapters.uc_metrics import load_uc_metrics
                specs = load_uc_metrics(path, dialect)[0]
        except Exception:  # noqa: BLE001 — an adapter refusal is a failed check
            return False
    specs = [s for s in specs if s.name == name]
    if len(specs) != 1:
        return False
    claim = build_claim(specs[0], registry, dialect)
    return claim.core is not None and claim.core.hash() == core.hash()
