"""Bounded conformance: when an executed query USES a governed metric rather
than redefining it.

A dashboard showing net revenue for the US, by channel, is the governed
metric used correctly — and differs from the governed definition in filters
and grain. Whole-core comparison called that an alternative definition, so
ordinary governed usage was the monitor's largest source of accusations.

"Conforming" is a POSITIVE verdict, held to the doctrine CERTIFIED is: it
comes only from the explicit rules below, and everything outside them is
unverifiable — never "conforming with a warning". Execution testing (the
oracle's conformance section) challenges these rules; it does not establish
them, because agreeing results on test data prove nothing general.

Rule set `monitor-conformance/1`. A query conforms to an authority
definition D when:

* EXACT: its proof-grade core equals D's — the certification standard,
  whatever the query's syntax; or
* SHAPE A (raw table): it is one SELECT over D's base table with no joins,
  CTEs, subqueries, windows, HAVING, QUALIFY, DISTINCT, LIMIT or bind
  parameters, and
    1. its measure expression and physical lineage equal D's,
    2. every one of D's required filters is a top-level AND conjunct, by
       canonical equality (`status = 'completed' OR country = 'US'` does not
       preserve `status = 'completed'`),
    3. every other conjunct is a supported slice predicate — AND/OR/NOT over
       atoms `dim <op> const`, `dim IN (consts)`, `dim BETWEEN c AND c`,
       `dim IS [NOT] NULL` — where `dim` is a SUPPORTED declared dimension
       (its column, or a DATE_TRUNC of a bare-column dimension) and `const`
       is built from literals, intervals and the current date/time,
    4. every grouping term is such a dimension, and D itself is ungrained;
* SHAPE B (metric view): it is one SELECT over an approved, fully
  qualified metric view, built ENTIRELY from an allow-list of constructs
  (anything else — TABLESAMPLE, time travel, joins, windows, subqueries,
  raw aggregates — is unverifiable), whose measure projections are exactly
  `MEASURE(m)` or `AGG(m)`, with filters and groupings over supported
  dimension names, and nothing consumes the result.

Violations are stated only with reasons that are true of the SQL as
written: a changed measure expression, a required filter that is absent,
weakened through OR, broadened or replaced, a slice on a column that is not
a declared dimension (when the dimension list is fully understood), a
different source table, or a governed measure presented under its own name
with a transformation applied. Whether two expressions differ in VALUE on
any particular data is not claimed.

Excluded from the claim (unverifiable when they occur): joins and joined
dimensions, window measures, LIMIT/HAVING/QUALIFY/DISTINCT, CTEs and
derived tables around a variation, unsupported dimension expressions, and
any Databricks runtime behaviour of MEASURE() beyond its declared
definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from semantix.adapters.base import DimensionSpec
from semantix.extraction.sqlutil import (
    flatten_conjuncts,
    render,
    split_group,
    table_fqname,
    truncation_of,
)
from semantix.models import SemanticCore

RULE_SET = "monitor-conformance/1"
EXCLUDED = (
    "joins and joined dimensions",
    "window measures",
    "LIMIT, HAVING, QUALIFY and DISTINCT",
    "TABLESAMPLE and time travel",
    "CTEs, subqueries and derived tables around a variation",
    "unsupported dimension expressions",
    "Databricks runtime behaviour of MEASURE() beyond its declared definition",
)

# Unverifiable reason codes, grouped for the headline breakdown.
CATEGORY = {
    "shape_unsupported": "unsupported shape",
    "predicate_unsupported": "unsupported shape",
    "consumed_downstream": "unsupported shape",
    "context_unresolved": "execution context",
    "no_definition_evidence": "no definition evidence",
    "dimension_unsupported": "unsupported dimension",
    "dimensions_not_modelled": "unsupported dimension",
    "grouping_undeclared": "unsupported dimension",
    "below_proof_grade": "below proof grade",
    "through_model": "below proof grade",
    "implication_undecided": "implication not decided",
    "grained_definition": "unsupported shape",
    "not_a_measure": "unsupported shape",
    "derived_expression": "unsupported shape",
    "unapproved_view": "unsupported shape",
}

_MARKERS = ("HAVING ", "QUALIFY ", "SELECT DISTINCT", "BOUND ")
# Violations that are only true if the measure does not itself apply the
# required filters.
_FILTER_BOUND = frozenset({"measure_changed", "transformed_governed_measure",
                           "required_filter_missing", "required_filter_not_preserved"})
_COMPARE = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ,
            exp.NullSafeNEQ)
_CONST_LEAVES = (exp.Literal, exp.Null, exp.Boolean, exp.CurrentDate,
                 exp.CurrentTimestamp, exp.CurrentDatetime, exp.CurrentTime)
_CONST_OPS = (exp.Neg, exp.Paren, exp.Cast, exp.TryCast, exp.Add, exp.Sub,
              exp.Interval, exp.DateAdd, exp.DateSub, exp.TsOrDsAdd,
              exp.DatetimeAdd, exp.DatetimeSub, exp.TimestampAdd, exp.TimestampSub,
              exp.DataType, exp.Var)


@dataclass
class Authority:
    """One authoritative, proof-grade definition the monitor judges against."""

    concept: str
    system: str
    name: str
    core: SemanticCore
    dimensions: list[DimensionSpec] | None = None
    view: str | None = None          # fully qualified metric view (pilot)
    measure: str | None = None       # measure name within the view
    applies_from: str | None = None
    applies_to: str | None = None
    where: str | None = None
    kind: str = "declared"           # "computed" when an operator names a model system

    @property
    def label(self) -> str:
        if self.view:
            return f"declared definition {self.system}:{self.view}/{self.measure}"
        if self.kind == "computed":
            return f"computed definition at {self.where} ({self.system}, named authoritative)"
        return f"declared definition {self.system}:{self.name}"

    @property
    def base_relation(self) -> str | None:
        nodes = self.core.join_nodes
        return nodes[0] if len(nodes) == 1 and not self.core.join_edges else None

    def applies_at(self, ts: str | None) -> bool:
        if ts is None:
            return False
        return ((self.applies_from is None or self.applies_from <= ts)
                and (self.applies_to is None or ts < self.applies_to))


@dataclass
class Outcome:
    status: str                               # conforming | violation | unverifiable
    reasons: list[dict] = field(default_factory=list)
    variation: dict = field(default_factory=lambda: {"slices": [], "grain": []})
    shape: str | None = None                  # exact | A | B
    # What WOULD have been a violation, had the query been inside the
    # supported-SQL boundary. Context for a human, never a verdict.
    suspected: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {"status": self.status, "shape": self.shape,
             "reasons": list(self.reasons), "variation": self.variation,
             "rule_set": RULE_SET}
        if self.suspected:
            d["suspected"] = list(self.suspected)
        return d

    @property
    def codes(self) -> list[str]:
        return [r["code"] for r in self.reasons]


def _reason(code: str, detail: str) -> dict:
    return {"code": code, "detail": detail}


def unverifiable(code: str, detail: str, shape: str | None = None) -> Outcome:
    return Outcome("unverifiable", [_reason(code, detail)], shape=shape)


# ----------------------------------------------------------------- shape A

def raw_shape_problem(stmt: exp.Expression) -> str | None:
    """Why a statement is outside Shape A's query shape, or None. Judged on
    the statement AS WRITTEN — the optimizer merging a CTE does not make a
    variation inside it a supported shape."""
    if not isinstance(stmt, exp.Select):
        return "not a single SELECT (set operation or other query form)"
    checks = (
        (stmt.args.get("with") is not None, "uses a CTE"),
        (stmt.find(exp.Subquery) is not None
         or any(n is not stmt for n in stmt.find_all(exp.Select)),
         "uses a subquery or derived table"),
        (bool(stmt.args.get("joins")), "joins another relation"),
        (stmt.find(exp.Window) is not None, "uses a window function"),
        (stmt.args.get("having") is not None, "uses HAVING"),
        (stmt.args.get("qualify") is not None, "uses QUALIFY"),
        (any(stmt.args.get(k) is not None for k in ("limit", "offset", "fetch")),
         "uses LIMIT/OFFSET/FETCH"),
        (stmt.args.get("distinct") is not None, "uses DISTINCT"),
        (stmt.find(exp.Placeholder) is not None or stmt.find(exp.Parameter) is not None,
         "uses a bind parameter"),
        (any(stmt.find(t) is not None for t in (exp.Lateral, exp.Unnest, exp.Pivot)),
         "uses LATERAL/UNNEST/PIVOT"),
    )
    for failed, why in checks:
        if failed:
            return why
    sources = [t for t in stmt.find_all(exp.Table)]
    if len(sources) != 1:
        return "does not read exactly one table"
    return None


def check_raw(core: SemanticCore, auth: Authority, dialect: str,
              shape_problem: str | None, models: set[str] = frozenset()) -> Outcome:
    """Shape A: an observed proof-grade core against one authority.

    Judgment order, as on the metric-view path: the supported-SQL BOUNDARY
    (query shape, markers, joins, unreadable or unsupported conditions,
    unsupported or undeclared groupings) is settled before any violation
    may stand. A query outside it is unverifiable, and what would have been
    a violation travels as `suspected` — an unsupported condition such as
    `UPPER(status) = 'COMPLETED'` may stand in for the very required filter
    a violation would call missing."""
    if core.hash() == auth.core.hash():
        return Outcome("conforming", shape="exact")
    base = auth.base_relation
    if base is None:
        return unverifiable("shape_unsupported",
                            "the authority definition reads a join graph — join "
                            "variations are not yet verified")
    boundary: list[dict] = []
    violations: list[dict] = []
    undecided: list[dict] = []
    empty = {"slices": [], "grain": []}
    if shape_problem:
        boundary.append(_reason("shape_unsupported", f"query {shape_problem}"))
    markers = [f for f in (*core.filters, *auth.core.filters) if f.startswith(_MARKERS)]
    if markers and not shape_problem:   # the shape problem already names it
        boundary.append(_reason("shape_unsupported",
                                f"carries {markers[0].split(' ')[0]} semantics, outside "
                                f"this rule set"))
    if auth.core.grain:
        boundary.append(_reason("grained_definition",
                                "the definition is itself grouped — re-grouping is "
                                "not judged in this rule set"))

    if tuple(core.join_nodes) != (base,) or core.join_edges:
        if len(core.join_nodes) == 1 and not core.join_edges:
            rel = core.join_nodes[0]
            if {rel, base} & set(models):
                boundary.append(_reason(
                    "through_model",
                    f"reads {rel}; the definition reads {base} — one of them is a "
                    f"model, and through-model comparison is not made"))
            else:
                violations.append(_reason(
                    "source_changed", f"computed over {rel}; the definition reads {base}"))
        else:
            boundary.append(_reason("shape_unsupported",
                                    "joins another relation — join slices are not yet "
                                    "verified"))
        return _settle(boundary, violations, undecided, empty)

    if core.measure_expr != auth.core.measure_expr \
            or tuple(core.source_columns) != tuple(auth.core.source_columns):
        transformed = (auth.core.measure_expr in core.measure_expr
                       and core.measure_expr != auth.core.measure_expr)
        violations.append(_reason(
            "transformed_governed_measure" if transformed else "measure_changed",
            (f"applies a transformation to the governed expression: "
             f"{core.measure_expr} (definition: {auth.core.measure_expr})")
            if transformed else
            f"measure is {core.measure_expr}; the definition is "
            f"{auth.core.measure_expr}"))
    if boundary:
        return _settle(boundary, violations, undecided, empty)

    parsed: dict[str, exp.Expression] = {}
    for text in (*core.filters, *auth.core.filters):
        if text not in parsed:
            node = _parse(text, dialect)
            if node is None:
                boundary.append(_reason("predicate_unsupported",
                                        f"could not re-read condition {text!r}"))
                return _settle(boundary, violations, undecided, empty)
            parsed[text] = node

    # Contradictory conditions fold to FALSE in canonicalization and take the
    # required conjuncts with them; "the required filter is not applied"
    # would then be a false statement about a query that selects no rows.
    if any(_is_false(parsed[f]) for f in core.filters):
        boundary.append(_reason("predicate_unsupported",
                                "the query's conditions are contradictory (they simplify "
                                "to FALSE) — it selects no rows; not judged"))
        return _settle(boundary, violations, undecided, empty)

    required = list(auth.core.filters)
    observed = list(core.filters)
    extras = [f for f in observed if f not in required]
    required_cols = set().union(*(_columns(parsed[r]) for r in required)) if required else set()
    consumed: set[str] = set()
    for r in (r for r in required if r not in observed):
        rnode = parsed[r]
        rcols = _columns(rnode)
        related = [x for x in extras if _columns(parsed[x]) & rcols]
        if not related:
            violations.append(_reason("required_filter_missing",
                                      f"required condition {r} is not applied"))
            continue
        consumed.update(related)
        decided = _replacement(r, rnode, related, extras, parsed, dialect)
        if decided is not None:
            violations.append(decided)
        else:
            undecided.append(_reason(
                "implication_undecided",
                f"required condition {r} is not a top-level condition; the query "
                f"has {' AND '.join(related)} instead — whether that implies it "
                f"is not decided"))

    resolver = _RawResolver(auth.dimensions)
    slices: list[str] = []
    for x in (x for x in extras if x not in consumed):
        verdict = _classify_predicate(parsed[x], resolver)
        if verdict.kind == "slice":
            slices.append(x)
        elif verdict.kind == "undeclared":
            cols = verdict.columns
            if resolver.fully_understood and not (cols <= required_cols):
                violations.append(_reason(
                    "unpermitted_slice",
                    f"filters {x}, on {', '.join(sorted(cols))} — not a declared "
                    f"dimension of the definition{resolver.hint(cols)}"))
            elif cols <= required_cols:
                undecided.append(_reason(
                    "implication_undecided",
                    f"condition {x} constrains column(s) the required filters use "
                    f"— whether it changes the definition is not decided"))
            else:
                boundary.append(_reason(resolver.unknown_code(), resolver.unknown_detail(x)))
        else:
            boundary.append(_reason(verdict.code, verdict.detail.format(x=x)))

    grain: list[str] = []
    for g in core.grain:
        node = _parse(g, dialect)
        if node is None or isinstance(node, (exp.Rollup, exp.Cube, exp.GroupingSets)) \
                or g.upper().startswith(("ROLLUP", "CUBE", "GROUPING SETS")):
            boundary.append(_reason("shape_unsupported",
                                    f"groups by {g} — ROLLUP/CUBE/GROUPING SETS are "
                                    f"not judged in this rule set"))
            continue
        verdict = _classify_term(node, resolver)
        if verdict == "dimension":
            grain.append(g)
        elif verdict == "unsupported":
            boundary.append(_reason("dimension_unsupported",
                                    f"groups by {g}, a dimension this rule set does "
                                    f"not model{resolver.hint(_columns(node))}"))
        elif resolver.dims is None or not resolver.fully_understood:
            boundary.append(_reason(resolver.unknown_code(), resolver.unknown_detail(g)))
        else:
            boundary.append(_reason("grouping_undeclared",
                                    f"groups by {g}, which is not a declared dimension "
                                    f"— grouping outside the governed model is not "
                                    f"judged in this rule set"))

    # A measure that itself conditions on a required filter's columns —
    # `SUM(amount) FILTER (WHERE status = 'completed')`, a CASE on status —
    # may carry the required filter inside the aggregate. Then neither "the
    # measure changed" nor "the filter is not applied" is a statement this
    # rule set can make truthfully; only unrelated violations survive.
    moved = (_columns(_parse(core.measure_expr, dialect)) & required_cols) \
        - _columns(_parse(auth.core.measure_expr, dialect))
    if moved:
        filter_bound = [v for v in violations if v["code"] in _FILTER_BOUND]
        if filter_bound:
            violations = [v for v in violations if v["code"] not in _FILTER_BOUND]
            undecided.append(_reason(
                "implication_undecided",
                f"the measure conditions on {', '.join(sorted(moved))}, a column of "
                f"the required filters — whether it carries them is not decided"))

    return _settle(boundary, violations, undecided, {"slices": slices, "grain": grain})


def _settle(boundary: list[dict], violations: list[dict], undecided: list[dict],
            variation: dict) -> Outcome:
    """Boundary first, then violations, then undecided implications."""
    if boundary:
        return Outcome("unverifiable", boundary + undecided, variation, "A",
                       suspected=[v["detail"] for v in violations])
    if violations:
        return Outcome("violation", violations, variation, "A")
    if undecided:
        return Outcome("unverifiable", undecided, variation, "A")
    return Outcome("conforming", [], variation, "A")


def _replacement(r: str, rnode: exp.Expression, related: list[str], extras: list[str],
                 parsed: dict[str, exp.Expression], dialect: str) -> dict | None:
    """A provable change to a required condition, or None (not decided)."""
    for x in related:
        node = _unparen(parsed[x])
        if isinstance(node, exp.Or):
            disjuncts = list(node.flatten())
            if r in {render(_unparen(d), dialect) for d in disjuncts}:
                others = set().union(*(_columns(d) for d in disjuncts
                                       if render(_unparen(d), dialect) != r))
                constrained = any(_columns(parsed[y]) & others
                                  for y in extras if y != x)
                if not constrained:
                    return _reason("required_filter_not_preserved",
                                   f"required condition {r} is weakened through OR: {x}")
    eq = _column_equals_literal(rnode)
    if eq is not None and len(related) == 1:
        col, lit = eq
        node = _unparen(parsed[related[0]])
        if isinstance(node, exp.In) and not node.args.get("query") \
                and _same_column(node.this, col) \
                and all(_is_literal(e) for e in node.expressions):
            values = {e.sql() for e in node.expressions}
            if lit.sql() in values and len(values) > 1:
                return _reason("required_filter_not_preserved",
                               f"required condition {r} is broadened: {related[0]} "
                               f"admits values the definition excludes")
        other = _column_equals_literal(node)
        if other is not None and _same_column(other[0], col) and other[1].sql() != lit.sql():
            return _reason("required_filter_not_preserved",
                           f"required condition {r} is replaced: {related[0]} selects "
                           f"rows the definition excludes")
    return None


# ----------------------------------------------------------------- shape B

# Databricks spells a metric-view measure reference MEASURE(m) or, from
# Runtime 18.1, AGG(m). Recognizing only one made the other vanish from the
# measure denominator: coverage depended on which spelling a client emitted.
MEASURE_FUNCTIONS = frozenset({"MEASURE", "AGG"})


# sqlglot 30.12 parses both as anonymous calls; 30.24 gives AGG a dedicated
# node (exp.Agg, argument in `this`). Both shapes are read, as split_group
# reads both GROUP BY shapes — a parser upgrade must not make a spelling
# vanish again.
_MEASURE_NODES = tuple(c for c in (getattr(exp, "Agg", None), getattr(exp, "Measure", None))
                       if isinstance(c, type))


def _is_measure_ref(node: exp.Expression) -> bool:
    if isinstance(node, exp.Anonymous):
        return str(node.this).upper() in MEASURE_FUNCTIONS
    return bool(_MEASURE_NODES) and isinstance(node, _MEASURE_NODES)


def measure_calls(node: exp.Expression) -> list[exp.Expression]:
    return [n for n in node.walk() if _is_measure_ref(n)]


def outside_measure_refs(node: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None:
        if _is_measure_ref(parent):
            return False
        parent = parent.parent
    return True


def measure_ref(call: exp.Expression) -> tuple[str, exp.Expression | None]:
    """(function name, its single argument or None) in either AST shape."""
    if isinstance(call, exp.Anonymous):
        args = list(call.expressions)
        return str(call.this).upper(), (args[0] if len(args) == 1 else None)
    args = [a for a in (call.this, *call.expressions) if a is not None]
    return type(call).__name__.upper(), (args[0] if len(args) == 1 else None)


# Shape B grants conformity only to a statement built ENTIRELY from these
# constructs (measure projections are judged separately). An allow-list, not
# a list of known-bad shapes: TABLESAMPLE and time travel passed the old
# deny-list as conforming because nobody had thought to name them.
_VIEW_NODES = (exp.Select, exp.From, exp.Table, exp.TableAlias, exp.Identifier,
               exp.Column, exp.Alias, exp.Where, exp.Group, exp.Order, exp.Ordered,
               exp.And, exp.Or, exp.Not, exp.Paren, exp.In, exp.Between, exp.Is,
               exp.DateTrunc, exp.TimestampTrunc, exp.DatetimeTrunc)
_VIEW_ARGS = {
    exp.Select: frozenset({"expressions", "from", "from_", "where", "group", "order"}),
    exp.Table: frozenset({"this", "db", "catalog", "alias"}),
    exp.Group: frozenset({"expressions", "all"}),
}
_ARG_NAMES = {"sample": "TABLESAMPLE", "version": "time travel (VERSION/TIMESTAMP AS OF)",
              "joins": "a join", "laterals": "LATERAL", "pivots": "PIVOT",
              "limit": "LIMIT", "offset": "OFFSET", "distinct": "DISTINCT",
              "having": "HAVING", "qualify": "QUALIFY", "with": "a CTE",
              "rollup": "ROLLUP", "cube": "CUBE", "grouping_sets": "GROUPING SETS"}
_NODE_NAMES = {"TableSample": "TABLESAMPLE", "Version": "time travel", "Agg": "AGG()",
               "Window": "a window function", "Subquery": "a subquery",
               "Placeholder": "a bind parameter", "Parameter": "a bind parameter"}


def _view_construct_problem(node: exp.Expression, skip: set[int]) -> str | None:
    """The first construct outside Shape B's allow-list, or None."""
    if id(node) in skip:
        return None
    if not isinstance(node, (*_VIEW_NODES, *_COMPARE, *_CONST_LEAVES, *_CONST_OPS)):
        if isinstance(node, exp.Anonymous):
            return f"uses {node.name}()"
        if isinstance(node, exp.AggFunc):
            return f"mixes a raw aggregate ({node.sql()}) with governed measures"
        name = type(node).__name__
        return f"uses {_NODE_NAMES.get(name, name)}"
    allowed = _VIEW_ARGS.get(type(node))
    if allowed is not None:
        extra = sorted(k for k, v in node.args.items()
                       if v not in (None, [], False) and k not in allowed)
        if extra:
            return "uses " + ", ".join(_ARG_NAMES.get(k, k) for k in extra)
    for child in node.iter_expressions():
        problem = _view_construct_problem(child, skip)
        if problem:
            return problem
    return None


@dataclass
class ViewProjection:
    measure: str            # the governed measure referenced
    output: str | None      # the name the query presents it under
    outcome: Outcome
    governed_reference: bool = True
    expression: str | None = None   # the projection as written


def metric_view_relation(stmt: exp.Expression) -> str | None:
    """The relation the (first) MEASURE() call reads from, lower-cased."""
    calls = measure_calls(stmt)
    select = calls[0].find_ancestor(exp.Select) if calls else None
    if select is None:
        return None
    from_ = select.args.get("from") or select.args.get("from_")
    table = from_.this if from_ is not None else None
    if not isinstance(table, exp.Table):
        return None
    return table_fqname(table).replace("`", "").lower()


def check_metric_view(stmt: exp.Expression,
                      approved: dict[str, dict[str, Authority | None]],
                      dialect: str) -> list[ViewProjection]:
    """Shape B, for a statement containing MEASURE(). `approved` maps a
    fully qualified metric view to {measure name: authority}, the authority
    None when the approved definition could not be read at proof grade."""
    calls = measure_calls(stmt)
    out: list[ViewProjection] = []
    top = stmt if isinstance(stmt, exp.Select) else None
    view_relation = metric_view_relation(stmt)
    view = approved.get(view_relation or "")
    # Each call with the name its projection presents it under, so every
    # early return still carries the claimed identity.
    refs: dict[tuple, str | None] = {}
    for call in calls:
        _, arg = measure_ref(call)
        m = arg.name.lower() if isinstance(arg, exp.Column) else "?"
        alias = call.find_ancestor(exp.Alias)
        output = alias.alias.lower() if alias is not None and alias.alias else None
        refs.setdefault((m, output), (alias.this if alias is not None else call).sql(dialect))

    def every(outcome_fn, governed: bool | None = None) -> list[ViewProjection]:
        return [ViewProjection(m, output, outcome_fn(m),
                               view is not None if governed is None else governed,
                               expression=written)
                for (m, output), written in refs.items()]

    if top is None or any(c.find_ancestor(exp.Select) is not top for c in calls) \
            or top.args.get("with") is not None or top.args.get("joins"):
        return every(lambda m: unverifiable(
            "consumed_downstream",
            "the governed calculation is consumed by an outer query, CTE or "
            "join — subsequent transformations are not verified", shape="B"))
    if view is None and view_relation and len(view_relation.split(".")) < 3:
        return every(lambda m: unverifiable(
            "context_unresolved",
            f"a measure reference over {view_relation}, which is not fully qualified "
            f"and no session context is evidenced — which metric view it reads is "
            f"unknown", shape="B"), governed=False)
    if view is None:
        return every(lambda m: unverifiable(
            "unapproved_view",
            f"a measure reference over {view_relation or 'an unnamed relation'}, which "
            f"is not an approved metric view (approved views are named by a pilot "
            f"package)", shape="B"), governed=False)
    measure_projections = {id(p) for p in top.expressions if measure_calls(p)}
    problem = _view_construct_problem(top, measure_projections)
    if problem:
        return every(lambda m: unverifiable("shape_unsupported", f"query {problem}",
                                            shape="B"))

    readable = [a for a in view.values() if a is not None]
    dims = readable[0].dimensions if readable else None
    resolver = _ViewResolver(dims)
    measure_names = set(view)

    # Non-measure projections, filters and groupings: the shared variation.
    shared: list[dict] = []
    slices: list[str] = []
    grain: list[str] = []
    projections = list(top.expressions)
    plain: list[exp.Expression] = []
    for proj in projections:
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        if measure_calls(inner):
            continue
        plain.append(inner)
        if _classify_term(inner, resolver) != "dimension":
            shared.append(_reason("dimension_unsupported",
                                  f"projects {inner.sql(dialect)}, not a supported "
                                  f"dimension of {view_relation}"))
    where = top.args.get("where")
    for conj in flatten_conjuncts(where.this if where else None):
        text = conj.sql(dialect)
        if measure_calls(conj):
            shared.append(_reason("predicate_unsupported",
                                  f"filters on a measure ({text})"))
            continue
        verdict = _classify_predicate(conj, resolver)
        if verdict.kind == "slice":
            slices.append(text)
        elif verdict.kind == "undeclared":
            shared.append(_reason("dimension_unsupported",
                                  f"filters {text}, on "
                                  f"{', '.join(sorted(verdict.columns))} — not a "
                                  f"supported dimension of {view_relation}"))
        else:
            shared.append(_reason(verdict.code, verdict.detail.format(x=text)))
    group = top.args.get("group")
    if group is not None:
        if group.args.get("all"):
            terms = plain
        else:
            plain_terms, special = split_group(group)
            if special:
                shared.append(_reason("shape_unsupported",
                                      "uses ROLLUP/CUBE/GROUPING SETS"))
            terms = []
            for t in plain_terms:
                if isinstance(t, exp.Literal) and t.is_int:
                    idx = int(t.name) - 1
                    if 0 <= idx < len(projections):
                        p = projections[idx]
                        t = p.this if isinstance(p, exp.Alias) else p
                terms.append(t)
        for t in terms:
            if measure_calls(t):
                shared.append(_reason("shape_unsupported", "groups by a measure"))
            elif _classify_term(t, resolver) == "dimension":
                grain.append(t.sql(dialect))
            else:
                shared.append(_reason("dimension_unsupported",
                                      f"groups by {t.sql(dialect)}, not a supported "
                                      f"dimension of {view_relation}"))
    variation = {"slices": slices, "grain": grain}

    for proj in projections:
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        calls_here = measure_calls(inner)
        if not calls_here:
            continue
        alias = proj.alias if isinstance(proj, exp.Alias) and proj.alias else None
        output = alias.lower() if alias else None
        written = inner.sql(dialect)
        bare = (inner is calls_here[0] and len(calls_here) == 1
                and isinstance(measure_ref(inner)[1], exp.Column))

        def emit(m: str, outcome: Outcome) -> None:
            out.append(ViewProjection(m, output, outcome, expression=written))

        for call in calls_here:
            fn, arg = measure_ref(call)
            m = arg.name.lower() if isinstance(arg, exp.Column) else None
            if m is None or m not in measure_names:
                emit(m or "?", unverifiable(
                    "not_a_measure", f"{fn}({arg.sql(dialect) if arg else ''}) does "
                    f"not name a measure of {view_relation}", shape="B"))
                continue
            if view[m] is None:
                emit(m, unverifiable(
                    "below_proof_grade", f"the approved definition of "
                    f"{view_relation}/{m} could not be read at proof grade", shape="B"))
                continue
            presented_as_governed = output is None or output == m
            transformation = None if bare else (
                f"presented as {m} but computed as {written} — a transformation of "
                f"the governed measure" if presented_as_governed else
                f"{output} = {written} is derived from the governed measure {m}; no "
                f"authority defines {output}")
            # The shared boundary (filters, groupings, projected dimensions)
            # is settled BEFORE the projection's formula is judged: a
            # transformed projection returned early and never saw it, so an
            # unsupported filter became a violation.
            if shared:
                emit(m, Outcome("unverifiable", list(shared), variation, "B",
                                suspected=[transformation] if transformation else []))
                continue
            if transformation and presented_as_governed:
                emit(m, Outcome("violation", [_reason(
                    "transformed_governed_measure", transformation)], variation, "B"))
            elif transformation:
                emit(m, unverifiable("derived_expression", transformation, shape="B"))
            else:
                emit(m, Outcome("conforming", [], variation, "B"))
    return out


# --------------------------------------------------------------- resolvers

class _Resolver:
    def __init__(self, dims: list[DimensionSpec] | None):
        self.dims = dims
        self.fully_understood = dims is not None and all(d.supported for d in dims)

    def unknown_code(self) -> str:
        return "dimensions_not_modelled"

    def unknown_detail(self, text: str) -> str:
        if self.dims is None:
            return (f"{text}: the authority's dimensions are not modelled for "
                    f"this source, so no slice or grouping can be judged")
        bad = [d.name for d in self.dims if not d.supported]
        return (f"{text}: the dimension list is not fully modelled (unsupported: "
                f"{', '.join(bad)}), so a column outside it cannot be called "
                f"unpermitted")

    def hint(self, cols: set[str]) -> str:
        return ""


class _RawResolver(_Resolver):
    """Dimensions by the base-table column they read."""

    def term(self, node: exp.Expression) -> str | None:
        """'dimension' | 'unsupported' | None (not a dimension)."""
        if self.dims is None:
            return None
        trunc = truncation_of(node)
        if trunc is not None:
            unit, col = trunc
            name = col.name.lower()
            matches = [d for d in self.dims if d.column == name]
            if any(d.supported and (d.truncation in (None, unit)) for d in matches):
                return "dimension"
            return "unsupported" if matches or self._unsupported_reads(name) else None
        if isinstance(node, exp.Column):
            name = node.name.lower()
            matches = [d for d in self.dims if d.column == name and d.truncation is None]
            if any(d.supported for d in matches):
                return "dimension"
            return "unsupported" if matches or self._unsupported_reads(name) else None
        return None

    def _unsupported_reads(self, column: str) -> bool:
        return any(not d.supported and d.expr and column in d.expr.lower()
                   for d in self.dims or [])

    def hint(self, cols: set[str]) -> str:
        named = [d for d in self.dims or [] if d.name in cols and d.column and d.column not in cols]
        if named:
            d = named[0]
            return f" (dimension {d.name} reads column {d.column})"
        truncated = [d for d in self.dims or [] if d.column in cols and d.truncation]
        if truncated:
            d = truncated[0]
            return (f" ({d.column} is declared only as {d.name} = "
                    f"DATE_TRUNC('{d.truncation}', {d.column}))")
        return ""


class _ViewResolver(_Resolver):
    """Dimensions by NAME — how a query over the metric view refers to them."""

    def term(self, node: exp.Expression) -> str | None:
        if self.dims is None:
            return None
        trunc = truncation_of(node)
        col = trunc[1] if trunc is not None else node
        if not isinstance(col, exp.Column):
            return None
        name = col.name.lower()
        matches = [d for d in self.dims if d.name == name]
        if any(d.supported for d in matches):
            return "dimension"
        return "unsupported" if matches else None


@dataclass
class _Predicate:
    kind: str                  # slice | undeclared | unsupported
    columns: set[str] = field(default_factory=set)
    code: str = ""
    detail: str = ""


def _classify_term(node: exp.Expression | None, resolver: _Resolver) -> str | None:
    if node is None:
        return None
    return resolver.term(_unparen(node))


def _classify_predicate(node: exp.Expression, resolver: _Resolver) -> _Predicate:
    cols = _columns(node)
    undeclared = set()
    unsupported = set()
    for col in node.find_all(exp.Column):
        verdict = resolver.term(col)
        if verdict is None:
            # A column read only inside a DATE_TRUNC that IS a dimension is fine.
            parent = col.parent
            if parent is not None and truncation_of(parent) is not None \
                    and resolver.term(parent) == "dimension":
                continue
            undeclared.add(col.name.lower())
        elif verdict == "unsupported":
            unsupported.add(col.name.lower())
    if undeclared:
        return _Predicate("undeclared", undeclared)
    if unsupported:
        return _Predicate("unsupported", cols, "dimension_unsupported",
                          f"{{x}} reads {', '.join(sorted(unsupported))}, a dimension "
                          f"this rule set does not model")
    if not _supported_shape(_unparen(node), resolver):
        return _Predicate("unsupported", cols, "predicate_unsupported",
                          "{x}: predicate shape not supported in this rule set")
    return _Predicate("slice", cols)


def _supported_shape(node: exp.Expression, resolver: _Resolver) -> bool:
    node = _unparen(node)
    if isinstance(node, (exp.And, exp.Or)):
        return all(_supported_shape(c, resolver) for c in (node.left, node.right))
    if isinstance(node, exp.Not):
        return _supported_shape(node.this, resolver)
    is_dim = lambda n: resolver.term(_unparen(n)) == "dimension"  # noqa: E731
    if isinstance(node, _COMPARE):
        left, right = node.left, node.right
        return (is_dim(left) and _is_const(right)) or (is_dim(right) and _is_const(left))
    if isinstance(node, exp.In):
        return (is_dim(node.this) and not node.args.get("query")
                and not node.args.get("unnest") and bool(node.expressions)
                and all(_is_const(e) for e in node.expressions))
    if isinstance(node, exp.Between):
        return (is_dim(node.this) and _is_const(node.args.get("low"))
                and _is_const(node.args.get("high")))
    if isinstance(node, exp.Is):
        return is_dim(node.this) and isinstance(node.expression, exp.Null)
    return False


# ----------------------------------------------------------------- helpers

def _parse(text: str, dialect: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(text, read=dialect)
    except Exception:  # noqa: BLE001 — a rendered condition should re-read
        return None


def _is_false(node: exp.Expression) -> bool:
    node = _unparen(node)
    return (isinstance(node, exp.Boolean) and not node.this) or isinstance(node, exp.Null)


def _unparen(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _columns(node: exp.Expression | None) -> set[str]:
    if node is None:
        return set()
    return {c.name.lower() for c in node.find_all(exp.Column)}


def _is_literal(node: exp.Expression | None) -> bool:
    node = _unparen(node) if node is not None else None
    if isinstance(node, (exp.Literal, exp.Null, exp.Boolean)):
        return True
    if isinstance(node, (exp.Neg, exp.Cast, exp.TryCast)):
        return _is_literal(node.this)
    return False


def _is_const(node: exp.Expression | None) -> bool:
    """Built only from literals, intervals and the current date/time: the same
    value for every row of one execution."""
    if node is None:
        return False
    for n in node.walk():
        if isinstance(n, (exp.Column, exp.Subquery, exp.Select, exp.AggFunc,
                          exp.Window, exp.Placeholder, exp.Parameter, exp.Anonymous)):
            return False
        if not isinstance(n, (*_CONST_LEAVES, *_CONST_OPS, exp.Identifier)):
            return False
    return True


def _column_equals_literal(node: exp.Expression) -> tuple[exp.Column, exp.Expression] | None:
    node = _unparen(node)
    if isinstance(node, exp.EQ):
        if isinstance(node.left, exp.Column) and _is_literal(node.right):
            return node.left, node.right
        if isinstance(node.right, exp.Column) and _is_literal(node.left):
            return node.right, node.left
    return None


def _same_column(a: exp.Expression, b: exp.Column) -> bool:
    return isinstance(a, exp.Column) and a.name.lower() == b.name.lower()
