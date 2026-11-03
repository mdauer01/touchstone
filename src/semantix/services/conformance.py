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

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from semantix.adapters.base import DimensionSpec
from semantix.extraction.sqlutil import (
    annotate_truncations,
    flatten_conjuncts,
    literal_kind,
    misread_literal,
    render,
    split_group,
    table_fqname,
    truncation_of,
    unaccepted_truncation,
)
from semantix.models import SemanticCore

RULE_SET = "monitor-conformance/1"
EXCLUDED = (
    "joins and joined dimensions",
    "window measures",
    "LIMIT, HAVING, QUALIFY and DISTINCT",
    "TABLESAMPLE and time travel",
    "CTEs, subqueries and derived tables outside the supported grammar (select-"
    "project-filter bodies inside it are inlined and judged)",
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
    "toolchain_unqualified": "unqualified parser",
    "transformation_undecided": "implication not decided",
    "unrecognized_function": "unrecognized function",
    "positive_suspended": "experimental match (positive verdicts suspended)",
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
    # Time groupings the operator APPROVED for this metric beyond its declared
    # dimensions: {dimension name: {DATE_TRUNC unit}} (pilot.yml
    # approved_time_groupings). A declared date dimension alone authorizes
    # grouping by the dimension as declared — never by every truncation of it.
    approved_groupings: dict = field(default_factory=dict)

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

    def current_at(self, now: str) -> bool:
        """Still applicable at `now`: no recorded end, or an end still ahead.
        A definition whose window closes next month was reported as one that
        "has ended" (sweep 2, #45)."""
        return self.applies_to is None or self.applies_to > now


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
        (_with(stmt) is not None, "uses a CTE"),
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
              shape_problem: str | None, models: set[str] = frozenset(),
              hypothesis: str | None = None,
              literal_undecided: frozenset = frozenset(),
              column_types: dict | None = None, *, refusal: str | None = None) -> Outcome:
    """Shape A: an observed proof-grade core against one authority.

    Two passes, in order, and the first never returns a violation:

    1. BOUNDARY — query shape and markers, the join graph, every condition's
       syntax (including conditions on required-filter columns, which the
       required-filter logic later reasons about), dimensions of the
       remaining conditions, and every grouping term. Complete before
       anything is judged.
    2. JUDGMENT — source, measure, required filters, undeclared slices.

    Outside the boundary the use is unverifiable and pass 2's violations
    travel as `suspected`: an unsupported condition such as
    `UPPER(status) = 'COMPLETED'` may stand in for the very required filter
    a violation would call missing, and a filter such as `refunds = 0` can
    make a changed measure equal the definition on its rows. Round twelve
    still returned from two branches (a changed source; conditions on
    required-filter columns) before the boundary was complete.

    `hypothesis` (the grammar gap) marks a core that is the general
    optimizer's reading, not proof: it is a boundary of its own, so the
    result is always unverifiable, and what pass 2 finds is suspected
    context worded as the hypothesis it is.

    `refusal` says why the statement AS WRITTEN cannot be compared (a
    truncation unit the engine does not accept): a boundary of its own that
    no exact match bypasses — the core re-renders the parser's reading."""
    if hypothesis is not None:
        outcome = _check_raw(core, auth, dialect, shape_problem, models, hypothesis,
                             literal_undecided, column_types, refusal)
        outcome.suspected = [f"under the optimizer's reading (a hypothesis): {s}"
                             for s in outcome.suspected]
        return outcome
    return _check_raw(core, auth, dialect, shape_problem, models, None, literal_undecided,
                      column_types, refusal)


def _check_raw(core: SemanticCore, auth: Authority, dialect: str,
               shape_problem: str | None, models: set[str],
               hypothesis: str | None,
               literal_undecided: frozenset = frozenset(),
               column_types: dict | None = None, refusal: str | None = None) -> Outcome:
    if refusal is not None:
        # Unverifiable whatever the core says. What the comparison finds
        # travels as context, as outside any boundary; the variation does
        # not — it would name a grain the engine does not compute.
        inner = _check_raw(core, auth, dialect, shape_problem, models, hypothesis,
                           literal_undecided, column_types)
        found = [r["detail"] for r in inner.reasons] if inner.status == "violation" else []
        return Outcome("unverifiable", [_reason("shape_unsupported", f"query {refusal}")],
                       shape="A", suspected=found + inner.suspected)
    if hypothesis is None and core.hash() == auth.core.hash():
        return Outcome("conforming", shape="exact")
    base = auth.base_relation
    if base is None:
        return unverifiable("shape_unsupported",
                            "the authority definition reads a join graph — join "
                            "variations are not yet verified")
    boundary: list[dict] = []
    violations: list[dict] = []
    undecided: list[dict] = []

    # ---- pass 1: the boundary --------------------------------------------
    if hypothesis is not None:
        # It names the construct the grammar refused; the as-written shape
        # problem would only repeat it.
        boundary.append(_reason("shape_unsupported", hypothesis))
    elif shape_problem:
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
    source_changed = None
    if tuple(core.join_nodes) != (base,) or core.join_edges:
        if len(core.join_nodes) == 1 and not core.join_edges:
            rel = core.join_nodes[0]
            if {rel, base} & set(models):
                boundary.append(_reason(
                    "through_model",
                    f"reads {rel}; the definition reads {base} — one of them is a "
                    f"model, and through-model comparison is not made"))
            else:
                source_changed = _reason(
                    "source_changed", f"computed over {rel}; the definition reads {base}")
        else:
            boundary.append(_reason("shape_unsupported",
                                    "joins another relation — join slices are not yet "
                                    "verified"))

    parsed: dict[str, exp.Expression] = {}
    for text in dict.fromkeys((*core.filters, *auth.core.filters)):
        if text.startswith(_MARKERS):
            continue
        node = _parse(text, dialect)
        if node is None:
            boundary.append(_reason("predicate_unsupported",
                                    f"could not re-read condition {text!r}"))
        else:
            parsed[text] = node
    # Contradictory conditions fold to FALSE in canonicalization and take the
    # required conjuncts with them; "the required filter is not applied"
    # would then be a false statement about a query that selects no rows.
    if any(f in parsed and _is_false(parsed[f]) for f in core.filters):
        boundary.append(_reason("predicate_unsupported",
                                "the query's conditions are contradictory (they simplify "
                                "to FALSE) — it selects no rows; not judged"))

    required = [r for r in auth.core.filters if r in parsed]
    extras = [f for f in core.filters if f in parsed and f not in auth.core.filters
              and not _is_false(parsed[f])]
    # Syntax of EVERY extra condition, whatever columns it touches.
    for x in extras:
        if not _supported_syntax(_unparen(parsed[x])):
            boundary.append(_reason("predicate_unsupported",
                                    f"{x}: predicate shape not supported in this rule set"))
    required_cols = set().union(*(_columns(parsed[r]) for r in required)) if required else set()
    missing = [r for r in required if r not in core.filters]
    # Conditions the required-filter judgment will reason about (structural:
    # they touch a missing required filter's columns).
    related = {r: [x for x in extras if _columns(parsed[x]) & _columns(parsed[r])]
               for r in missing}
    consumed = {x for xs in related.values() for x in xs}

    resolver = _RawResolver(auth.dimensions, auth.approved_groupings)
    slices: list[str] = []
    undeclared: list[tuple[str, set[str]]] = []
    for x in (x for x in extras if x not in consumed):
        verdict = _classify_predicate(parsed[x], resolver)
        if verdict.kind == "slice":
            slices.append(x)
        elif verdict.kind == "undeclared":
            cols = verdict.columns
            if resolver.fully_understood or cols <= required_cols:
                undeclared.append((x, cols))      # judged in pass 2
            else:
                boundary.append(_reason(resolver.unknown_code(), resolver.unknown_detail(x)))
        elif verdict.code != "predicate_unsupported":   # syntax already reported
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
        verdict = _classify_term(node, resolver, grouping=True)
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
                                    f"groups by {g}, which is not a declared dimension"
                                    f"{resolver.grouping_hint(_unparen(node))} "
                                    f"— grouping outside the governed model is not "
                                    f"judged in this rule set"))
    variation = {"slices": slices, "grain": grain}

    # ---- pass 2: judgment --------------------------------------------------
    notes: list[str] = []      # observations for review, never verdicts
    measure_differs = core.measure_expr != auth.core.measure_expr \
        or tuple(core.source_columns) != tuple(auth.core.source_columns)
    observed_node = _parse(core.measure_expr, dialect) if measure_differs else None
    governed_node = _parse(auth.core.measure_expr, dialect) if measure_differs else None
    if observed_node is not None:
        # A narrowing cast is kept as an observation for review — on EVERY
        # path where the measure differs, the source change included: it
        # CAN change values; whether it changed this result is not decided.
        # Notes never change a verdict.
        notes += narrowing_casts(observed_node, column_types or {}, dialect,
                                 governed=governed_node)
    if source_changed is not None:
        # Measure and filters are compared against a different table's
        # definition only through the source change itself.
        return _settle(boundary, [source_changed], undecided, variation, notes)
    if measure_differs:
        transformed = (auth.core.measure_expr in core.measure_expr
                       and core.measure_expr != auth.core.measure_expr)
        def governed(n):
            return n == governed_node
        wrapper = (transformed and observed_node is not None and governed_node is not None
                   and wrapper_only(observed_node, governed))
        column_wrapped = (not wrapper and observed_node is not None
                          and governed_node is not None
                          and tuple(core.source_columns) == tuple(auth.core.source_columns)
                          and strip_column_wrappers(observed_node) == governed_node)
        if column_wrapped:
            # `SUM(CAST(amount AS DOUBLE))` — often a staging CTE's no-op
            # cast — may compute exactly the definition: not decided (round
            # 17 found it called a changed measure).
            undecided.append(_reason(
                "transformation_undecided",
                f"measure is {core.measure_expr}, the definition "
                f"{auth.core.measure_expr} with casts, rounding or neutral arithmetic "
                f"around its columns or aggregates — whether that preserves the value "
                f"is not decided"))
        elif wrapper and not definite_change(observed_node, governed):
            # CAST(… AS DOUBLE), ROUND, × 1: may preserve the value. Refused
            # honestly; the transformation is context (sweep 2, #41 #57).
            undecided.append(_reason(
                "transformation_undecided",
                f"applies {core.measure_expr} to the governed expression "
                f"{auth.core.measure_expr} — whether it preserves the value is "
                f"not decided"))
        else:
            violations.append(_reason(
                "transformed_governed_measure" if wrapper else "measure_changed",
                (f"applies a transformation to the governed expression: "
                 f"{core.measure_expr} (definition: {auth.core.measure_expr})")
                if wrapper else
                f"measure is {core.measure_expr}; the definition is "
                f"{auth.core.measure_expr}"))
    for r in missing:
        if not related[r]:
            violations.append(_reason("required_filter_missing",
                                      f"required condition {r} is not applied"))
            continue
        decided = _replacement(r, parsed[r], related[r], extras, parsed, dialect,
                               literal_undecided)
        if decided is not None:
            violations.append(decided)
        else:
            undecided.append(_reason(
                "implication_undecided",
                f"required condition {r} is not a top-level condition; the query "
                f"has {' AND '.join(related[r])} instead — whether that implies it "
                f"is not decided"))
    for x, cols in undeclared:
        if cols <= required_cols:
            undecided.append(_reason(
                "implication_undecided",
                f"condition {x} constrains column(s) the required filters use "
                f"— whether it changes the definition is not decided"))
        else:
            violations.append(_reason(
                "unpermitted_slice",
                f"filters {x}, on {', '.join(sorted(cols))} — not a declared "
                f"dimension of the definition{resolver.hint(cols)}"))

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

    return _settle(boundary, violations, undecided, variation, notes)


def _settle(boundary: list[dict], violations: list[dict], undecided: list[dict],
            variation: dict, notes: list[str] | None = None) -> Outcome:
    """Boundary first, then violations, then undecided implications. `notes`
    are observations kept for review (a narrowing cast) — never verdicts."""
    notes = list(notes or [])
    if boundary:
        return Outcome("unverifiable", boundary + undecided, variation, "A",
                       suspected=[v["detail"] for v in violations] + notes)
    if violations:
        return Outcome("violation", violations, variation, "A", suspected=notes)
    if undecided:
        return Outcome("unverifiable", undecided, variation, "A", suspected=notes)
    return Outcome("conforming", [], variation, "A", suspected=notes)


def _replacement(r: str, rnode: exp.Expression, related: list[str], extras: list[str],
                 parsed: dict[str, exp.Expression], dialect: str,
                 literal_undecided: frozenset = frozenset()) -> dict | None:
    """A provable change to a required condition, or None (not decided).

    Decided by VALUE, never by literal text: `discount = 0.0`, `0E0` and
    `CAST(0 AS DOUBLE)` select the rows `discount = 0` selects, and
    `IN ('completed', NULL)` admits nothing more than 'completed' — all were
    called replaced or broadened. A comparison whose literal kinds differ
    (a string against a number) depends on coercion and is not decided."""
    rcols = _columns(rnode)
    for x in related:
        node = _unparen(parsed[x])
        if isinstance(node, exp.Or):
            disjuncts = list(node.flatten())
            if r in {render(_unparen(d), dialect) for d in disjuncts}:
                others = [d for d in disjuncts if render(_unparen(d), dialect) != r]
                if not others:
                    # `r OR r` admits exactly what `r` admits; it is not a
                    # top-level `r` either, so it is not decided (round 17).
                    continue
                other_cols = set().union(*(_columns(d) for d in others))
                # Weakened only if another branch can admit rows the required
                # condition excludes: a branch on the required column itself
                # (`status = 'completed' AND …`) may imply it, and a branch
                # that compares with NULL admits nothing.
                independent = (not (other_cols & rcols)
                               and not any(d.find(exp.Null) for d in others))
                constrained = any(_columns(parsed[y]) & other_cols
                                  for y in extras if y != x)
                # Another top-level condition on the required column itself
                # (`… AND status BETWEEN 'completed' AND 'completed'`) may
                # restore exactly what the OR weakened.
                restored = any(_columns(parsed[y]) & rcols for y in extras if y != x)
                if independent and not constrained and not restored:
                    return _reason("required_filter_not_preserved",
                                   f"required condition {r} is weakened through OR: {x}")
    eq = _column_equals_literal(rnode)
    if eq is not None and eq[0].name.lower() in literal_undecided:
        eq = None   # a collated / CHAR column: literal equality is engine-specific
    if eq is not None and len(related) == 1:
        col, lit = eq
        want = _literal_value(lit)
        node = _unparen(parsed[related[0]])
        if want is not None and isinstance(node, exp.In) and not node.args.get("query") \
                and _same_column(node.this, col) \
                and all(_is_literal(e) for e in node.expressions):
            values = [_literal_value(e) for e in node.expressions
                      if not isinstance(_unparen(e), exp.Null)]
            if values and all(v is not None and v[0] == want[0] for v in values) \
                    and want in values and any(v != want and _distinct_everywhere(v, want)
                                               for v in values):
                return _reason("required_filter_not_preserved",
                               f"required condition {r} is broadened: {related[0]} "
                               f"admits values the definition excludes")
        other = _column_equals_literal(node)
        if want is not None and other is not None and _same_column(other[0], col):
            got = _literal_value(other[1])
            if got is not None and got[0] == want[0] and got != want \
                    and _distinct_everywhere(got, want):
                return _reason("required_filter_not_preserved",
                               f"required condition {r} is replaced: {related[0]} selects "
                               f"rows the definition excludes")
    return None


# A string the column's type could coerce: a number, a date or time, a
# boolean. '2026-1-1' and '2026-01-01' are one DATE; as text they differ.
_COERCIBLE = re.compile(r"[\s\d.:+\-/TZeE]*|(?i:true|false|yes|no|on|off|t|f|y|n)")


def _literal_value(node: exp.Expression) -> tuple[str, object] | None:
    """("num", Decimal) or ("str", text) for a literal whose value does not
    depend on the column's type, else None (not decided).

    Round fifteen decided by value through casts and typed literals, and sweep
    2 showed the coercion still decides: DATE'2026-1-1' is DATE'2026-01-01',
    TIMESTAMP'2026-01-01 00:00:00' equals that DATE, 0.10000000000000001 is
    0.1 as a DOUBLE, CAST(-0.4 AS INT) is 0 — each was called a replaced
    required filter. Now only uncast integers (below 2^53, exact in every
    numeric type), decimals that stay distinct as DOUBLE too, and plain
    strings nothing could coerce are compared."""
    import decimal

    node = _unparen(node)
    negate = False
    while isinstance(node, (exp.Neg, exp.Paren)):
        if isinstance(node, exp.Neg):
            negate = not negate
        node = node.this
    if not isinstance(node, exp.Literal):
        return None   # casts, typed literals (DATE '…'), anything computed
    if node.is_string:
        # A backslash is an escape to Spark/Databricks ('complet\\ed' is
        # 'completed' there) but kept by the parser: not decided (round 17).
        if negate or _COERCIBLE.fullmatch(node.name) or "\\" in node.name:
            return None
        return ("str", node.name)
    try:
        value = decimal.Decimal(node.name)
    except decimal.InvalidOperation:
        return None
    if not value.is_finite():
        return None
    value = -value if negate else value
    if value == value.to_integral_value():
        return ("num", value) if abs(value) < 2 ** 53 else None
    return ("dec", value)


def _distinct_everywhere(a: tuple[str, object], b: tuple[str, object]) -> bool:
    """Two literal values no numeric column type makes equal: decimals that
    are also different DOUBLEs (0.1 and 0.10000000000000001 are not)."""
    if a[0] == "dec" or b[0] == "dec":
        return float(a[1]) != float(b[1]) and a[1] != b[1]
    return a != b


# A wrapper around the governed measure is a definite change only when it
# changes every nonzero value: ± c (c ≠ 0), × or ÷ c (c ≠ 1), negation. A cast,
# a rounding, × 1 or -(-x) may preserve the value — not decided.
def wrapper_only(expr: exp.Expression, governed) -> bool:
    """Whether `expr` is the governed expression under constants, casts and
    functions alone — nothing else it reads. `SUM(amount) - SUM(refund)`
    over a governed `SUM(amount)` reads another column: a changed measure,
    not a wrapper."""
    target = next((n for n in expr.walk() if governed(n)), None)
    if target is None:
        return False
    rest = [n for n in expr.walk() if not _within(n, target)]
    return not any(isinstance(n, (exp.Column, exp.AggFunc, exp.Anonymous)) for n in rest)


def _within(node: exp.Expression, target: exp.Expression) -> bool:
    while node is not None:
        if node is target:
            return True
        node = node.parent
    return False


_INTEGRAL_BYTES = {"TINYINT": 1, "BYTE": 1, "INT1": 1, "SMALLINT": 2, "SHORT": 2, "INT2": 2,
                   "INT": 4, "INTEGER": 4, "INT4": 4, "BIGINT": 8, "LONG": 8, "INT8": 8,
                   "HUGEINT": 16, "INT128": 16}
# Decimal digits an integer type holds: EVERY value with `full` digits fits;
# SOME values with `full + 1` (the type's maximum width) do.
_INT_FULL_DIGITS = {1: 2, 2: 4, 4: 9, 8: 18, 16: 38}
_INT_BITS = {1: 7, 2: 15, 4: 31, 8: 63, 16: 127}
_FLOATS = {"FLOAT": 24, "REAL": 24, "FLOAT4": 24, "DOUBLE": 53, "FLOAT8": 53,
           "DOUBLE PRECISION": 53}
# A bare DECIMAL means different things per engine.
_BARE_DECIMAL = {"databricks": (10, 0), "spark": (10, 0), "duckdb": (18, 3),
                 "snowflake": (38, 0), "bigquery": (38, 9)}
# Type names whose width is the DIALECT's: Snowflake's integers are all
# NUMBER(38,0) and its floats all double; BigQuery's integers are INT64.
_DIALECT_TYPES = {
    "snowflake": {**{t: ("decimal", 38, 0) for t in ("INT", "INTEGER", "BIGINT", "SMALLINT",
                                                      "TINYINT", "BYTEINT")},
                  **{t: ("float", 53) for t in ("FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "REAL",
                                                "DOUBLE PRECISION")}},
    "bigquery": {**{t: ("int", 8) for t in ("INT64", "INT", "INTEGER", "BIGINT", "SMALLINT",
                                             "TINYINT", "BYTEINT")},
                 "FLOAT64": ("float", 53)}}


def _numeric_type(text: str | None, dialect: str = "databricks"):
    """('int', bytes) | ('float', mantissa bits) | ('decimal', p, s) | None.
    None also for a bare DECIMAL whose default precision the dialect does not
    fix (Postgres: unconstrained)."""
    if not text:
        return None
    t = re.sub(r"\s+", " ", str(text).strip().upper())
    if t in _DIALECT_TYPES.get(dialect, {}):
        return _DIALECT_TYPES[dialect][t]
    m = re.fullmatch(r"(DECIMAL|NUMERIC|DEC|NUMBER)\s*(?:\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\))?",
                     t)
    if m:
        if m.group(2) is None:
            default = _BARE_DECIMAL.get(dialect)
            return ("decimal", *default) if default else None
        return ("decimal", int(m.group(2)), int(m.group(3) or 0))
    base = t.split("(")[0].strip()
    if base in _INTEGRAL_BYTES:
        return ("int", _INTEGRAL_BYTES[base])
    if base in _FLOATS:
        return ("float", _FLOATS[base])
    return None


def _narrows(source, target) -> bool | None:
    """Whether a cast from `source` to `target` (_numeric_type) can change a
    value: True, False, or None (not decided)."""
    if source is None or target is None:
        return None
    if source[0] == "float":
        if target[0] == "float":
            return target[1] < source[1]
        return True                           # to an integer or a decimal: rounds
    if source[0] == "decimal":
        p, s_ = source[1], source[2]
        if target[0] == "int":
            return s_ > 0 or (p - s_) > _INT_FULL_DIGITS[target[1]]
        if target[0] == "decimal":
            return target[2] < s_ or (target[1] - target[2]) < (p - s_)
        # A binary float holds every integer up to 2**mantissa exactly, and
        # no decimal fraction such as 0.10.
        return s_ > 0 or 10 ** p - 1 > 2 ** target[1]
    if source[0] == "int":
        if target[0] == "int":
            return target[1] < source[1]
        if target[0] == "decimal":
            return (target[1] - target[2]) < _INT_FULL_DIGITS[source[1]] + 1
        return _INT_BITS[source[1]] > target[1]   # beyond the float's exact integers
    return None


def _render_type(t) -> str:
    if t is None:
        return "unknown"
    if t[0] == "int":
        return {1: "TINYINT", 2: "SMALLINT", 4: "INT", 8: "BIGINT", 16: "HUGEINT"}[t[1]]
    if t[0] == "float":
        return "DOUBLE" if t[1] == 53 else "FLOAT"
    return f"DECIMAL({t[1]},{t[2]})"


def _value_type(node: exp.Expression, column_types: dict, dialect: str,
                known: dict | None = None):
    """The numeric type of a value, as far as it can be derived: a declared
    column, a cast's target, a numeric literal, through ROUND / NEUTRAL
    arithmetic (`× 1`, `+ 0`) / negation, and the aggregates' result types
    (SUM of an integer is BIGINT — HUGEINT on DuckDB — of a DECIMAL(p,s)
    DECIMAL(min(38,p+10),s); COUNT is BIGINT; AVG is DOUBLE or a DECIMAL).
    `known` gives the type of nodes this function cannot see into (a
    MEASURE() reference), by their rendering. None when it cannot be."""
    node = _unparen(node)
    if known and node.sql(dialect) in known:
        return known[node.sql(dialect)]

    def rec(n):
        return _value_type(n, column_types, dialect, known)
    if isinstance(node, exp.Column):
        return _numeric_type(_column_type(node, column_types), dialect)
    if isinstance(node, (exp.Cast, exp.TryCast)):
        return _numeric_type(node.to.sql(dialect) if node.to is not None else None, dialect)
    if isinstance(node, exp.Literal):
        return _literal_type(node)
    if isinstance(node, exp.Neg):
        return rec(node.this)
    if isinstance(node, exp.Round):
        return _round_type(rec(node.this), node.args.get("decimals"), dialect)
    if isinstance(node, exp.Count):
        return ("int", 8)
    if isinstance(node, (exp.Sum, exp.Avg, exp.Min, exp.Max)):
        inner = rec(node.this)
        if inner is None or isinstance(node, (exp.Min, exp.Max)):
            return inner
        if isinstance(node, exp.Avg):
            # DuckDB's AVG of a DECIMAL is a DOUBLE; Spark's a wider DECIMAL.
            return ("float", 53) if inner[0] != "decimal" or dialect == "duckdb" else \
                ("decimal", min(38, inner[1] + 4), min(38, inner[2] + 4))
        if inner[0] == "int":
            return ("int", 16) if dialect == "duckdb" else ("int", max(8, inner[1]))
        if inner[0] == "decimal":
            return ("decimal", min(38, inner[1] + 10), inner[2])
        return ("float", 53)
    if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        left, right = _unparen(node.this), _unparen(node.expression)
        neutral = "1" if isinstance(node, (exp.Mul, exp.Div)) else "0"
        if isinstance(node, exp.Div):
            # `/` on integers is a DOUBLE (Spark, DuckDB); on decimals a
            # rescaled DECIMAL this rule set does not derive.
            a = rec(left) if _is_number(right, neutral) else None
            return ("float", 53) if a is not None and a[0] == "int" else \
                a if a is not None and a[0] == "float" else None
        # Neutral arithmetic keeps the other operand's type. Otherwise both
        # operands are typed — a literal too: INT × 0.5 is a DECIMAL, not
        # an INT — and only the combinations below are derived.
        if _is_number(right, neutral):
            return rec(left)
        if _is_number(left, neutral) and not isinstance(node, exp.Sub):
            return rec(right)
        a, b = rec(left), rec(right)
        if a is None or b is None:
            return None
        if dialect == "duckdb":
            # DuckDB fits a literal to the other operand: an integer literal
            # takes the smallest integer type that holds it, a numeric
            # literal beside a FLOAT is a FLOAT.
            for lit, other, typed in ((left, right, b), (right, left, a)):
                value = _numeric_value(lit)
                if value is None or isinstance(_cast_literal(other), exp.Literal):
                    continue
                if typed[0] == "float":
                    return typed
                if typed[0] == "int" and value == value.to_integral_value():
                    return ("int", max(typed[1], _smallest_int(value)))
        if a[0] == b[0] == "int":
            return ("int", max(a[1], b[1]))
        if a[0] == b[0] == "float":
            return ("float", max(a[1], b[1]))
        if {a[0], b[0]} == {"float", "int"}:
            return a if a[0] == "float" else b       # FLOAT + INT is a FLOAT
        if "float" in (a[0], b[0]):
            return ("float", 53)                     # with a DECIMAL: DOUBLE
        return None
    return None


def _round_type(inner, decimals: exp.Expression | None, dialect: str):
    """ROUND keeps an integer or a float; on a DECIMAL(p,s) to d < s places
    it is a DECIMAL with scale max(d, 0) — DuckDB keeps p, Spark gives
    p - s + d + 1 (one digit for the carry)."""
    if inner is None or inner[0] != "decimal":
        return inner
    d = 0
    if decimals is not None:
        lit = _unparen(decimals)
        if isinstance(lit, exp.Neg):
            lit, sign = _unparen(lit.this), -1
        else:
            sign = 1
        if not (isinstance(lit, exp.Literal) and not lit.is_string and lit.name.isdigit()):
            return None
        d = sign * int(lit.name)
    p, s_ = inner[1], inner[2]
    if d >= s_:
        return inner
    scale = max(d, 0)
    return ("decimal", p, scale) if dialect == "duckdb" else \
        ("decimal", min(38, p - s_ + scale + 1), scale)


def _column_type(node: exp.Column, column_types: dict) -> str | None:
    """A column's declared type: by its qualified name first (the monitor
    keys `relation.column`), then by its bare name — which the monitor keeps
    only where every relation read agrees."""
    parts = [p.lower() for p in (node.catalog, node.db, node.table, node.name) if p]
    for i in range(len(parts) - 1):
        hit = column_types.get(".".join(parts[i:]))
        if hit is not None:
            return hit
    return column_types.get(node.name.lower())


def _numeric_value(node: exp.Expression):
    """The value of a numeric literal, a negated one included; else None."""
    import decimal

    node = _unparen(node)
    sign = 1
    if isinstance(node, exp.Neg):
        node, sign = _unparen(node.this), -1
    if not (isinstance(node, exp.Literal) and not node.is_string):
        return None
    try:
        return sign * decimal.Decimal(node.name)
    except decimal.InvalidOperation:
        return None


def _smallest_int(value) -> int:
    for width in (1, 2, 4, 8, 16):
        if -(2 ** _INT_BITS[width]) <= value < 2 ** _INT_BITS[width]:
            return width
    return 16


def _cast_literal(node: exp.Expression) -> exp.Expression:
    """The literal under casts to number types (`1.0D` parses as CAST(1.0 AS
    DOUBLE)); `node` itself otherwise."""
    node = _unparen(node)
    while isinstance(node, (exp.Cast, exp.TryCast)) and node.to is not None \
            and _numeric_type(node.to.sql()) is not None:
        node = _unparen(node.this)
    return node


def _is_number(node: exp.Expression, value: str) -> bool:
    import decimal

    node = _cast_literal(node)
    if not (isinstance(node, exp.Literal) and not node.is_string):
        return False
    try:
        return decimal.Decimal(node.name) == decimal.Decimal(value)
    except decimal.InvalidOperation:
        return False


def _literal_type(node: exp.Literal):
    """A numeric literal's type: an integer is INT (BIGINT beyond it), a
    plain decimal `1.50` is DECIMAL(3,2), an exponent makes it DOUBLE. A
    string literal has no numeric type."""
    if node.is_string:
        return None
    text = node.name.lstrip("+-")
    if re.fullmatch(r"\d+", text):
        return ("int", 4) if int(text) < 2 ** 31 else ("int", 8) if int(text) < 2 ** 63 else None
    m = re.fullmatch(r"(\d*)\.(\d*)", text)
    if m:
        digits = (m.group(1).lstrip("0") or "") + m.group(2)
        scale = len(m.group(2))
        return ("decimal", max(len(digits), scale, 1), scale)
    if re.fullmatch(r"[\d.]+[eE][+-]?\d+", text):
        return ("float", 53)
    return None


def _written_type(node: exp.Expression, column_types: dict, dialect: str) -> str | None:
    """The type the SQL or the catalog STATES for a value, as text, when it
    is not a number type this rule set models (`amount` declared STRING;
    `CAST(x AS STRING)`; a string literal)."""
    if isinstance(node, exp.Column):
        return _column_type(node, column_types)
    if isinstance(node, (exp.Cast, exp.TryCast)) and node.to is not None:
        return node.to.sql(dialect)
    if isinstance(node, exp.Literal) and node.is_string:
        return "a string literal"
    return None


# Cast targets that are not numbers — casting to them is not narrowing a
# number, and this rule set says nothing about them.
_NON_NUMERIC = frozenset({"STRING", "VARCHAR", "CHAR", "TEXT", "DATE", "TIMESTAMP",
                          "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMPTZ", "TIMESTAMP_TZ",
                          "DATETIME", "TIME", "BOOLEAN", "BINARY", "INTERVAL", "JSON",
                          "VARIANT", "ARRAY", "MAP", "STRUCT"})


def _constant_preserved(value: exp.Literal, target, negative: bool = False) -> bool | None:
    """Whether casting the numeric constant `value` (negated when `negative`)
    to `target` keeps it; None when that is not decided here. Decimal
    arithmetic runs at a precision no literal exceeds — a DECIMAL(38,18)
    constant raised InvalidOperation and aborted the whole monitor run. A
    float keeps a constant whose shortest form reads back the same (0.1 is
    0.1 as a DOUBLE literal too)."""
    import decimal
    import struct

    try:
        with decimal.localcontext() as ctx:
            ctx.prec = 400
            v = decimal.Decimal(value.name)
            if negative:
                v = -v
            if not v.is_finite():
                return None
            if target[0] == "int":
                bound = 2 ** _INT_BITS[target[1]]
                return v == v.to_integral_value() and -bound <= v < bound
            if target[0] == "decimal":
                p, s_ = target[1], target[2]
                return v == v.quantize(decimal.Decimal(1).scaleb(-s_)) and \
                    abs(v) < decimal.Decimal(10) ** (p - s_)
            f = float(v)
            if f in (float("inf"), float("-inf")):
                return False                          # beyond a DOUBLE
            if target[1] == 24:
                try:
                    f32 = struct.unpack("f", struct.pack("f", f))[0]
                except OverflowError:
                    return False                      # beyond a FLOAT
                return decimal.Decimal(f"{f32:.7g}") == v
            return decimal.Decimal(repr(f)) == v
    except (decimal.InvalidOperation, OverflowError, ValueError, struct.error):
        return None


def _unqualified(node: exp.Expression, dialect: str) -> str:
    """`node` rendered with its columns unqualified: the optimizer's reading
    qualifies the columns of a cast the definition writes bare."""
    copy = node.copy()
    for col in copy.find_all(exp.Column):
        for part in ("table", "db", "catalog"):
            col.set(part, None)
    return copy.sql(dialect)


def narrowing_casts(expr: exp.Expression, column_types: dict, dialect: str,
                    governed: exp.Expression | None = None,
                    known: dict | None = None) -> list[str]:
    """Observations for the casts in `expr` that can change the values they
    convert, kept for review, each once. A cast to a narrower type CAN change
    a value; whether it changed THIS result is not decided, and nothing here
    says it did. No observation for a cast that cannot change a value — a
    widening, NULL, a boolean, a constant it keeps exactly — nor for casts
    the `governed` definition itself writes: they are the definition, not
    something the query did to it. `known` as _value_type."""
    from collections import Counter

    own = Counter(_unqualified(c, dialect) for c in governed.find_all(exp.Cast, exp.TryCast)) \
        if governed is not None else Counter()
    out: list[str] = []

    def note(text: str) -> None:
        if text not in out:
            out.append(text)
    for cast in expr.find_all(exp.Cast, exp.TryCast):
        try:
            _note_cast(cast, column_types, dialect, known, own, note)
        except Exception:  # noqa: BLE001 — an observation never breaks a run
            continue
    return out


def _note_cast(cast, column_types: dict, dialect: str, known, own, note) -> None:
    """One cast's observation, if any (narrowing_casts)."""
    if cast.to is None:
        return
    key = _unqualified(cast, dialect)
    if own[key] > 0:
        own[key] -= 1
        return
    written = cast.to.sql(dialect)
    target = _numeric_type(written, dialect)
    inner = _unparen(cast.this)
    if isinstance(inner, (exp.Null, exp.Boolean)):
        return
    if target is None:
        base = re.sub(r"\s*\(.*$", "", written.strip().upper())
        if base not in _NON_NUMERIC:
            note(f"{cast.sql(dialect)}: {written} is a type this rule set does not model "
                 f"— whether the cast changes values is not decided")
        return
    constant = _unparen(inner.this) if isinstance(inner, exp.Neg) else inner
    if isinstance(constant, exp.Literal) and not constant.is_string:
        kept = _constant_preserved(constant, target, negative=isinstance(inner, exp.Neg))
        if kept is False:
            note(f"{cast.sql(dialect)} changes the constant {inner.sql(dialect)} — "
                 f"whether that changes this result is not decided")
        if kept is not None:
            return
    source = _value_type(inner, column_types, dialect, known)
    verdict = _narrows(source, target)
    what = (f"{inner.name} ({_column_type(inner, column_types)})"
            if isinstance(inner, exp.Column) else
            f"the value it casts ({_render_type(source)})")
    if verdict:
        note(f"{cast.sql(dialect)} narrows {what} to {written} — it can change "
             f"values; whether it changes this result is not decided")
    elif verdict is None:
        stated = _written_type(inner, column_types, dialect)
        if stated and _numeric_type(stated, dialect) is None:
            subject = (f"{inner.name} is declared {stated}" if isinstance(inner, exp.Column)
                       else f"the value it casts is {stated}")
            note(f"{cast.sql(dialect)}: {subject}, not a numeric type this rule "
                 f"set models — whether the cast changes values is not decided")
        else:
            note(f"{cast.sql(dialect)}: the type of the value it casts is not "
                 f"known, so whether the cast narrows it is not decided")


def strip_column_wrappers(expr: exp.Expression) -> exp.Expression:
    """`expr` with every wrapper that MAY preserve a value removed: a cast,
    ROUND, `× 1`, `÷ 1`, `+ 0`, `− 0` (neutral as a DOUBLE too) and double
    negation around a column or an aggregate (`CAST(SUM(amount) AS DOUBLE)`).
    Equality after stripping decides nothing; it only says the difference
    may be no difference."""
    import decimal

    def neutral(lit: exp.Expression, value: int) -> bool:
        # `1.0D` is CAST(1.0 AS DOUBLE) to the parser: a cast of the neutral
        # value to a number type is still the neutral value.
        lit = _cast_literal(lit)
        if not (isinstance(lit, exp.Literal) and not lit.is_string):
            return False
        try:
            return float(decimal.Decimal(lit.name)) == float(value)
        except (decimal.InvalidOperation, ValueError):
            return False

    def operand(n: exp.Expression) -> bool:
        return isinstance(n, (exp.Column, exp.AggFunc))

    def strip(node: exp.Expression) -> exp.Expression:
        node = _unparen(node)
        if isinstance(node, (exp.Cast, exp.TryCast, exp.Round)):
            inner = strip(node.this)
            if operand(inner):
                return inner
        if isinstance(node, exp.Neg) and isinstance(_unparen(node.this), exp.Neg):
            inner = strip(_unparen(node.this).this)
            if operand(inner):
                return inner
        if isinstance(node, (exp.Mul, exp.Div, exp.Add, exp.Sub)):
            unit = 1 if isinstance(node, (exp.Mul, exp.Div)) else 0
            left, right = strip(node.this), strip(node.expression)
            if operand(left) and neutral(_unparen(node.expression), unit):
                return left
            if isinstance(node, (exp.Mul, exp.Add)) and operand(right) \
                    and neutral(_unparen(node.this), unit):
                return right
        return node

    def walk(node: exp.Expression) -> exp.Expression:
        node = strip(node)
        for key, value in list(node.args.items()):
            if isinstance(value, exp.Expression):
                node.set(key, walk(value))
            elif isinstance(value, list):
                node.set(key, [walk(v) if isinstance(v, exp.Expression) else v
                               for v in value])
        return node

    return walk(expr.copy())


def definite_change(expr: exp.Expression, governed) -> bool:
    import decimal

    node = _unparen(expr)
    if isinstance(node, exp.Neg):
        return governed(_unparen(node.this))
    if not isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div)):
        return False
    left, right = _unparen(node.this), _unparen(node.expression)
    sides = [(left, right)] if isinstance(node, (exp.Sub, exp.Div)) else \
        [(left, right), (right, left)]
    for measure, lit in sides:
        if not governed(measure):
            continue
        if not (isinstance(lit, exp.Literal) and not lit.is_string):
            continue
        try:
            c = decimal.Decimal(lit.name)
        except decimal.InvalidOperation:
            continue
        neutral = 0 if isinstance(node, (exp.Add, exp.Sub)) else 1
        # A constant that rounds to the neutral element as a DOUBLE
        # (`* 1.00000000000000000001`, `+ 1e-400`) changes nothing there.
        return c.is_finite() and c != neutral and float(c) != float(neutral)
    return False


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
    # `AS v(country, …, net_revenue)` renames the view's columns positionally
    # — it can swap two measures' names. Only the bare alias is allowed.
    exp.TableAlias: frozenset({"this"}),
}
_ARG_NAMES = {"sample": "TABLESAMPLE", "version": "time travel (VERSION/TIMESTAMP AS OF)",
              "joins": "a join", "laterals": "LATERAL", "pivots": "PIVOT",
              "limit": "LIMIT", "offset": "OFFSET", "distinct": "DISTINCT",
              "having": "HAVING", "qualify": "QUALIFY", "with": "a CTE", "with_": "a CTE",
              "rollup": "ROLLUP", "cube": "CUBE", "grouping_sets": "GROUPING SETS",
              "columns": "a column alias list on the metric view"}
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


def _with(select: exp.Expression):
    """The WITH clause, under either argument name sqlglot has used. Reading
    only `with` saw no CTE at all on 30.x, which stores it as `with_`."""
    return select.args.get("with_") or select.args.get("with")


def check_metric_view(stmt: exp.Expression,
                      approved: dict[str, dict[str, Authority | None]],
                      dialect: str, column_types=None) -> list[ViewProjection]:
    """Shape B, for a statement containing MEASURE(). `approved` maps a
    fully qualified metric view to {measure name: authority}, the authority
    None when the approved definition could not be read at proof grade.
    `column_types(relation)` gives a source's declared column types, for the
    narrowing-cast observations."""
    calls = measure_calls(stmt)
    out: list[ViewProjection] = []
    top = stmt if isinstance(stmt, exp.Select) else None
    view_relation = metric_view_relation(stmt)
    view = approved.get(view_relation or "")
    # One entry per PROJECTION, with the name it presents its value under, so
    # every early return still carries the claimed identity — and a
    # projection combining two measures stays one use (sweep 2, #46).
    refs: dict[int, tuple[str, str | None, str]] = {}
    for call in calls:
        _, arg = measure_ref(call)
        m = arg.name.lower() if isinstance(arg, exp.Column) else "?"
        alias = call.find_ancestor(exp.Alias)
        output = alias.alias.lower() if alias is not None and alias.alias else None
        node = call
        while node.parent is not None and not isinstance(node.parent, exp.Select):
            node = node.parent
        refs.setdefault(id(node), (m, output,
                                   (alias.this if alias is not None else call).sql(dialect)))

    def every(outcome_fn, governed: bool | None = None) -> list[ViewProjection]:
        return [ViewProjection(m, output, outcome_fn(m),
                               view is not None if governed is None else governed,
                               expression=written)
                for m, output, written in refs.values()]

    ctes = {c.alias_or_name.lower() for c in top.find_all(exp.CTE)} if top is not None else set()
    if view_relation and view_relation in ctes:
        # MEASURE() over a CTE: which view, and through what, is not this
        # rule set's to read — and no session context would change that.
        return every(lambda m: unverifiable(
            "shape_unsupported",
            f"a measure reference over {view_relation}, a CTE — Shape B judges "
            f"MEASURE() only where it reads an approved metric view directly",
            shape="B"), governed=False)
    if top is None or any(c.find_ancestor(exp.Select) is not top for c in calls) \
            or _with(top) is not None or top.args.get("joins"):
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
    # As in Shape A, a truncation the engine does not accept as written is no
    # grain at all — and its rendering would name one ('W' as WEEK).
    problem = unaccepted_truncation(top, dialect) or \
        misread_literal(stmt.meta.get("source_text") or "", dialect) or \
        _view_construct_problem(top, measure_projections)
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
    # Groupings that truncate a dimension to a grain of its own: permitted
    # per MEASURE, only when approved for that metric.
    time_groupings: list[tuple[str, tuple[str, ...] | None, str]] = []
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
                tg = resolver.time_grouping(_unparen(t))
                if tg is not None:
                    time_groupings.append((t.sql(dialect), *tg))
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
        core_expr = _unparen(inner)   # `(MEASURE(m))` is still a bare reference
        bare = (core_expr is calls_here[0] and len(calls_here) == 1
                and isinstance(measure_ref(core_expr)[1], exp.Column))
        if len(calls_here) > 1:
            # One projection is one measure use, however many governed
            # measures it combines; a derived expression is never judged as
            # any one of them.
            fn, arg = measure_ref(calls_here[0])
            first = arg.name.lower() if isinstance(arg, exp.Column) else "?"
            out.append(ViewProjection(first, output, Outcome(
                "unverifiable", [_reason(
                    "derived_expression",
                    f"{output or written} = {written} combines governed measures; whether it "
                    f"equals any one definition is not decided")], shape="B",
                suspected=_measure_cast_notes(inner, calls_here, view, column_types,
                                              dialect)),
                expression=written))
            continue

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
            notes = [] if bare else _measure_cast_notes(inner, [call], view, column_types,
                                                        dialect)
            approved = view[m].approved_groupings or {}
            own = [_reason("grouping_undeclared",
                           f"groups by {sql}: grouping by {unit} is not a declared dimension "
                           f"of {view_relation}"
                           + (" — it truncates a dimension that is itself a truncation, "
                              "which no approval makes a declared grain" if dims is None else
                              f" and is not approved for {m} — approve it on "
                              f"{' or '.join(dims)} in pilot.yml approved_time_groupings if "
                              f"it is a governed grain"
                              if dims else " (no plain date dimension to approve it on)"))
                   for sql, dims, unit in time_groupings
                   if dims is None or not any(unit in approved.get(d, ()) for d in dims)]
            if shared or own:
                emit(m, Outcome("unverifiable", [*shared, *own], variation, "B",
                                suspected=([transformation] if transformation else [])
                                + notes))
                continue
            if transformation and presented_as_governed \
                    and wrapper_only(inner, lambda n: n is call) \
                    and not definite_change(inner, lambda n: n is call):
                emit(m, Outcome("unverifiable", [_reason(
                    "transformation_undecided",
                    f"presented as {m} but computed as {written} — whether the "
                    f"transformation preserves the value is not decided")],
                    variation, "B", suspected=[transformation, *notes]))
            elif transformation and presented_as_governed:
                emit(m, Outcome("violation", [_reason(
                    "transformed_governed_measure", transformation)], variation, "B",
                    suspected=notes))
            elif transformation:
                emit(m, Outcome("unverifiable", [_reason("derived_expression", transformation)],
                                shape="B", suspected=notes))
            else:
                emit(m, Outcome("conforming", [], variation, "B"))
    return out


def _measure_cast_notes(inner: exp.Expression, calls: list[exp.Expression], view: dict,
                        column_types, dialect: str) -> list[str]:
    """Narrowing-cast observations for a projection over MEASURE() calls:
    each reference has the type of its governed expression, over the view's
    source columns."""
    if inner.find(exp.Cast, exp.TryCast) is None:
        return []
    known: dict = {}
    types: dict = {}
    for call in calls:
        _, arg = measure_ref(call)
        auth = view.get(arg.name.lower()) if isinstance(arg, exp.Column) else None
        if auth is None:
            continue
        source = auth.base_relation
        types = ({c.lower(): t for c, t in column_types(source).items()}
                 if column_types is not None and source else {})
        governed = _parse(auth.core.measure_expr, dialect)
        if governed is not None:
            known[call.sql(dialect)] = _value_type(governed, types, dialect)
    return narrowing_casts(inner, types, dialect, known=known)


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
    """Dimensions by the base-table column they read.

    A truncation of a declared date column means two different things. In a
    FILTER it is a time window on that column (`DATE_TRUNC('WEEK', d) =
    DATE '…'` selects a contiguous range of d), which the declared dimension
    permits. In a GROUPING it is a new grain: permitted only when that
    truncation IS a declared dimension, or the operator approved it for this
    metric (`approved`). Round seventeen's benchmark found every unit of a
    bare declared date column accepted as a grouping."""

    def __init__(self, dims: list[DimensionSpec] | None, approved: dict | None = None):
        super().__init__(dims)
        self.approved = approved or {}

    def term(self, node: exp.Expression, grouping: bool = False) -> str | None:
        """'dimension' | 'unsupported' | None (not a dimension)."""
        if self.dims is None:
            return None
        trunc = truncation_of(node)
        if trunc is not None:
            unit, col = trunc
            name = col.name.lower()
            matches = [d for d in self.dims if d.column == name]
            if grouping:
                if any(d.supported and d.truncation == unit for d in matches):
                    return "dimension"
                if any(d.supported and d.truncation is None
                       and unit in self.approved.get(d.name.lower(), ()) for d in matches):
                    return "dimension"
                if any(d.supported for d in matches):
                    return None    # a declared column, an undeclared grain
                return "unsupported" if matches or self._unsupported_reads(name) else None
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

    def grouping_hint(self, node: exp.Expression) -> str:
        trunc = truncation_of(node)
        if trunc is None:
            return ""
        unit, col = trunc
        bare = [d for d in self.dims or []
                if d.column == col.name.lower() and d.truncation is None and d.supported]
        if not bare:
            return ""
        names = " or ".join(d.name for d in bare)
        return (f" ({names} is a declared dimension, but grouping it by {unit} is "
                f"neither a declared dimension nor approved for this metric — approve it "
                f"in pilot.yml approved_time_groupings if it is a governed grain)")

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
    """Dimensions by NAME — how a query over the metric view refers to them.
    In a grouping, a truncation of a dimension is a new grain unless it is the
    dimension's own truncation (a no-op) — whether it is approved depends on
    the metric, so `time_grouping` reports it for a per-measure decision."""

    def term(self, node: exp.Expression, grouping: bool = False) -> str | None:
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

    def time_grouping(self, node: exp.Expression) -> tuple[tuple[str, ...] | None, str] | None:
        """(the plain dimensions an approval may name, unit) when `node`
        truncates a supported dimension to a grain NO declared dimension has;
        None when it is declared (its own truncation, or another dimension
        declared as exactly that truncation of the same column — `month` =
        DATE_TRUNC('MONTH', order_date)). An approval on ANY plain declared
        dimension of the column approves the grain; none when the column has
        no plain declared dimension to approve it on."""
        trunc = truncation_of(node)
        if trunc is None or self.dims is None:
            return None
        unit, col = trunc
        named = next((d for d in self.dims if d.name == col.name.lower() and d.supported),
                     None)
        if named is None:
            return None
        if named.truncation == unit:
            return None                      # its own truncation again: a no-op
        if named.truncation is not None:
            # A grain of a grain: MONTH of a WEEK dimension credits a week
            # that starts in September to September — not the declared month,
            # and nothing an approval on the base column describes.
            return None, unit
        if any(d.supported and d.column == named.column and d.truncation == unit
               for d in self.dims):
            return None
        plain = tuple(d.name.lower() for d in self.dims if d.supported
                      and d.column == named.column and d.truncation is None)
        return plain, unit


@dataclass
class _Predicate:
    kind: str                  # slice | undeclared | unsupported
    columns: set[str] = field(default_factory=set)
    code: str = ""
    detail: str = ""


def _classify_term(node: exp.Expression | None, resolver: _Resolver,
                   grouping: bool = False) -> str | None:
    if node is None:
        return None
    return resolver.term(_unparen(node), grouping=grouping)


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
    return _shape(node, lambda n: resolver.term(_unparen(n)) == "dimension")


def _supported_syntax(node: exp.Expression) -> bool:
    """The predicate grammar alone, before asking which columns are
    dimensions: atoms over a bare column or a DATE_TRUNC of one."""
    def is_term(n: exp.Expression) -> bool:
        n = _unparen(n)
        return isinstance(n, exp.Column) or truncation_of(n) is not None
    return _shape(node, is_term)


def _shape(node: exp.Expression, is_dim) -> bool:
    node = _unparen(node)
    if isinstance(node, (exp.And, exp.Or)):
        return all(_shape(c, is_dim) for c in (node.left, node.right))
    if isinstance(node, exp.Not):
        return _shape(node.this, is_dim)
    if isinstance(node, _COMPARE):
        left, right = node.left, node.right
        return (is_dim(left) and _is_const(right)) or (is_dim(right) and _is_const(left))
    if isinstance(node, exp.In):
        # Items of one kind only, as for BETWEEN: the engines bind the list to
        # one type (`country IN ('US', 5)`).
        return (is_dim(node.this) and not node.args.get("query")
                and not node.args.get("unnest") and bool(node.expressions)
                and all(_is_const(e) for e in node.expressions)
                and len({literal_kind(e) for e in node.expressions}) == 1)
    if isinstance(node, exp.Between):
        # Bounds of one kind only — the policy's boundary, and the proof
        # grammar's: Shape B read `d BETWEEN DATE '…' AND '…'` as a slice.
        low, high = node.args.get("low"), node.args.get("high")
        return (is_dim(node.this) and _is_const(low) and _is_const(high)
                and literal_kind(low) == literal_kind(high))
    if isinstance(node, exp.Is):
        return is_dim(node.this) and isinstance(node.expression, exp.Null)
    return False


# ----------------------------------------------------------------- helpers

def _parse(text: str, dialect: str) -> exp.Expression | None:
    try:
        node = sqlglot.parse_one(text, read=dialect)
    except Exception:  # noqa: BLE001 — a rendered condition should re-read
        return None
    annotate_truncations(node, text, dialect)
    return node


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
