"""The proof path: a small, positively defined certification grammar.

sqlglot's general optimizer rewrites SQL for EXECUTION, not for proof. A
review of 6e831d0 reproduced many rewrites that do not preserve meaning —
predicate pushdown below an ordered LIMIT 1 (NULL became 900), merges on the
null-supplying side of outer joins, join elimination, subquery unnesting
under NOT IN and empty scalar lookups, boolean simplification that is wrong
under NULLs, exact-decimal folding of DOUBLE arithmetic. An optimizer
completing successfully does not establish that its output preserves the
query's meaning, so it no longer grants proof grade. It survives only as a
source of HYPOTHESES (canonicalize.canonicalize_sql), which can never
certify, alias, or conform.

This module grants proof grade to SQL inside one grammar, through
transformations that are each stated with their preconditions (RULES) and
rest on stated assumptions (ASSUMPTIONS). Anything else is refused with the
reason. The policy is versioned: a positive verdict records the policy and
the rules it used, and a policy change requires recertification.

Grammar (policy semantix-proof/2):

* one SELECT over exactly ONE source — a table, or a chain of
  select-project-filter CTEs / derived tables over one table;
* no joins, set operations, recursion, LIMIT/OFFSET, DISTINCT, QUALIFY,
  window functions, subqueries in expressions, sampling, time travel,
  LATERAL/UNNEST/PIVOT or bind parameters;
* expressions built from columns of that table, literals, arithmetic,
  comparisons, AND/OR/NOT, IN over literals, BETWEEN, IS [NOT] NULL, LIKE,
  CASE, and an allow-list of scalar functions;
* aggregates SUM, COUNT (incl. COUNT(*) and DISTINCT), AVG, MIN, MAX, with
  an optional FILTER (WHERE …), and GROUP BY / HAVING over the above;
* every column resolves to a known column of the source.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from semantix.extraction.sqlutil import flatten_conjuncts, table_fqname

POLICY_ID = "semantix-proof/2"

# Every transformation the proof path applies. Adding one is a claim that it
# preserves the query's meaning under ASSUMPTIONS, and it must arrive with a
# regression case the execution oracle runs (original vs transformed SQL).
RULES = {
    "identifiers": "unquoted identifiers are folded by the dialect's own case rule",
    "inline": "a select-project-filter CTE or derived table (one source; no aggregate in "
              "any clause, "
              "window, DISTINCT, GROUP BY, HAVING, LIMIT/OFFSET, set operation or "
              "subquery) is substituted into its single consumer: its projections "
              "replace the consumer's references and its WHERE is conjoined",
    "star": "SELECT * is expanded from the source's known columns",
    "unused_cte": "a CTE that nothing reads is removed",
    "order": "ORDER BY without LIMIT/OFFSET is removed, unless it holds the only aggregate "
             "(which would make the query an aggregate one)",
    "group_ordinal": "a GROUP BY ordinal is replaced by the projection it names",
    "output_alias": "a GROUP BY or HAVING name that is a projection alias and NOT a "
                    "source column is replaced by that projection",
    "group_all": "GROUP BY ALL is replaced by the non-aggregate, non-constant projections",
    "conjuncts": "WHERE is read as the set of its top-level AND conjuncts",
    "commutative": "AND/OR chains are flattened and ordered; the two operands of one "
                   "+, *, =, <> are ordered (never re-associated)",
    "not_equality": "NOT (a = b) is a <> b and NOT (a <> b) is a = b",
    "table_alias": "the source's alias is renamed t0",
    "between": "x BETWEEN a AND b (not SYMMETRIC) is x >= a AND x <= b",
    "parens": "parentheses are re-derived from the parse tree: exactly those operator "
              "precedence needs for the text to parse back to the same tree "
              "(checked by re-parsing)",
}
ASSUMPTIONS = (
    "column types are those the catalog or registered model declares",
    "expressions evaluate without raising errors, so the order in which AND/OR "
    "operands are evaluated does not change a result",
    "ORDER BY without a bound changes no value: the grammar has no order-sensitive "
    "function",
    "relation identity is the name the SQL wrote, completed only by the "
    "operator-supplied execution context",
    "identifier case folding follows the dialect",
    "CURRENT_DATE and CURRENT_TIMESTAMP are one value per execution, so two "
    "queries using them are compared as executed at the same time",
)
POLICY = {"id": POLICY_ID, "rules": RULES, "assumptions": list(ASSUMPTIONS)}
GRAMMAR_GAP = f"outside the supported grammar ({POLICY_ID})"
IDENTITY_GAP = f"relation identity not established ({POLICY_ID})"


def is_grammar_gap(gap: str) -> bool:
    """The gap canonicalize() records when the form is the optimizer's
    HYPOTHESIS, not proof — the only gap that still lets a reading serve as
    context (`suspected`) rather than nothing."""
    return gap.startswith(GRAMMAR_GAP)


def is_policy_gap(gap: str) -> bool:
    """A refusal the policy STATES (grammar or identity) — scope, not a blind
    spot."""
    return gap.startswith((GRAMMAR_GAP, IDENTITY_GAP))

_AGGREGATES = (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)
_SCALAR_FUNCTIONS = (
    exp.Upper, exp.Lower, exp.Trim, exp.Length, exp.Abs, exp.Round, exp.Coalesce,
    exp.Nullif, exp.If, exp.Case, exp.Cast, exp.TryCast, exp.DateTrunc, exp.TimestampTrunc,
    exp.DatetimeTrunc, exp.Extract, exp.Year, exp.Month, exp.Day, exp.Quarter,
    exp.Concat, exp.Substring, exp.Floor, exp.Ceil, exp.Sign, exp.Sqrt, exp.Ln,
    exp.Exp, exp.Pow, exp.Greatest, exp.Least, exp.CurrentDate, exp.CurrentTimestamp,
)
_EXPRESSIONS = (
    exp.Column, exp.Identifier, exp.Literal, exp.Null, exp.Boolean, exp.Star,
    exp.Neg, exp.Paren, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv,
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ,
    exp.And, exp.Or, exp.Not, exp.In, exp.Between, exp.Is, exp.Like, exp.ILike,
    exp.DataType, exp.DataTypeParam, exp.Var, exp.Interval, exp.Alias, exp.Tuple,
    exp.Ordered, exp.Distinct, exp.Filter, exp.Where,
    *_AGGREGATES, *_SCALAR_FUNCTIONS,
)
_STRUCTURE = (exp.Select, exp.From, exp.Table, exp.TableAlias, exp.Group, exp.Having,
              exp.Order, exp.With, exp.CTE, exp.Subquery)
_SELECT_ARGS = frozenset({"expressions", "from", "from_", "where", "group", "having",
                          "order", "with", "with_"})
_TABLE_ARGS = frozenset({"this", "db", "catalog", "alias"})
# sqlglot renamed Select's FROM argument across versions.
_FROM_KEY = "from_" if "from_" in exp.Select.arg_types else "from"
_GROUP_ARGS = frozenset({"expressions", "all"})
_ARG_NAMES = {"joins": "a join", "limit": "LIMIT", "offset": "OFFSET", "distinct": "DISTINCT",
              "qualify": "QUALIFY", "laterals": "LATERAL", "pivots": "PIVOT",
              "sample": "TABLESAMPLE", "version": "time travel", "kind": "SELECT AS STRUCT",
              "rollup": "ROLLUP", "cube": "CUBE", "grouping_sets": "GROUPING SETS",
              "recursive": "a recursive CTE", "windows": "a named WINDOW", "fetch": "FETCH",
              "connect": "CONNECT BY", "prewhere": "PREWHERE", "settings": "SETTINGS",
              "format": "FORMAT", "locks": "a locking clause", "match": "MATCH_RECOGNIZE",
              "into": "SELECT INTO", "operation_modifiers": "an operation modifier",
              "hint": "an optimizer hint", "columns": "a column alias list",
              "materialized": "a MATERIALIZED hint", "scalar": "a scalar CTE",
              "joins_table": "a join", "totals": "WITH TOTALS"}

_NODE_NAMES = {"Rollup": "ROLLUP", "Cube": "CUBE", "GroupingSets": "GROUPING SETS",
               "Join": "a join", "Union": "a set operation", "Intersect": "a set operation",
               "Except": "a set operation", "Lateral": "LATERAL", "Unnest": "UNNEST",
               "Pivot": "PIVOT", "Placeholder": "a bind parameter",
               "Parameter": "a bind parameter", "TableSample": "TABLESAMPLE",
               "Qualify": "QUALIFY", "Limit": "LIMIT", "Offset": "OFFSET",
               "Exists": "EXISTS", "Array": "ARRAY"}

# Binding strength, for parenthesizing a substituted expression.
_PRECEDENCE = [
    (exp.Or,), (exp.And,), (exp.Not,),
    (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ, exp.NullSafeNEQ,
     exp.In, exp.Between, exp.Is, exp.Like, exp.ILike),
    (exp.Add, exp.Sub), (exp.Mul, exp.Div, exp.Mod, exp.IntDiv), (exp.Neg,),
]


def _rank(node: exp.Expression) -> int | None:
    for i, group in enumerate(_PRECEDENCE):
        if isinstance(node, group):
            return i
    return None


class Refusal(Exception):
    """Not provable; the message is the reason. `kind` is "grammar" (the SQL
    is outside the grammar: the optimizer's reading may still serve as a
    hypothesis) or "identity" (the SQL is inside it, but which relation or
    column it reads is not established — no reading of it is trustworthy)."""

    def __init__(self, message: str, kind: str = "grammar"):
        super().__init__(message)
        self.kind = kind


@dataclass
class ProofResult:
    ast: exp.Select | None
    rules: tuple[str, ...]
    reason: str | None
    kind: str = "grammar"          # of the refusal, when there is one

    @property
    def supported(self) -> bool:
        return self.ast is not None


def certify(sql: str, registry, dialect: str) -> ProofResult:
    """Proof normal form of `sql`, or the reason it is outside the grammar.
    `registry` (extraction.registry.SchemaRegistry) is the only source of
    relation identity and columns."""
    rules: list[str] = []
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception as e:  # noqa: BLE001 — arbitrary input
        return ProofResult(None, (), f"could not parse: {e}")
    try:
        if not isinstance(parsed, exp.Select):
            raise Refusal("not a single SELECT (set operation or other statement)")
        normalized = normalize_identifiers(parsed.copy(), dialect=dialect)
        rules.append("identifiers")
        _check_grammar(normalized)
        ast = _Builder(registry, dialect, rules).build(normalized)
        ast = _expand_between(ast, rules)
    except Refusal as r:
        return ProofResult(None, tuple(dict.fromkeys(rules)), str(r), r.kind)
    return ProofResult(ast, tuple(dict.fromkeys(rules)), None)


# ------------------------------------------------------------------ grammar

def _check_grammar(node: exp.Expression) -> None:
    """Every node and argument in the statement is on the allow-list."""
    if isinstance(node, exp.Anonymous):
        raise Refusal(f"function {node.name}() is not in the supported grammar")
    if isinstance(node, exp.Window):
        raise Refusal("a window function is not in the supported grammar")
    if not isinstance(node, (*_EXPRESSIONS, *_STRUCTURE)):
        # Named as the SQL construct, whichever way a sqlglot version parses
        # it (30.24 makes ROLLUP a node where 30.12 had an argument).
        named = _NODE_NAMES.get(type(node).__name__)
        raise Refusal(f"uses {named}" if named else
                      f"{type(node).__name__} is not in the supported grammar")
    allowed = {exp.Select: _SELECT_ARGS, exp.Table: _TABLE_ARGS,
               exp.Group: _GROUP_ARGS}.get(type(node))
    if allowed is not None:
        extra = sorted(k for k, v in node.args.items()
                       if v not in (None, [], False) and k not in allowed)
        if extra:
            raise Refusal("uses " + ", ".join(_ARG_NAMES.get(k, k) for k in extra))
    if isinstance(node, exp.TableAlias) and node.args.get("columns"):
        raise Refusal("uses a column alias list")
    if isinstance(node, exp.With) and node.args.get("recursive"):
        raise Refusal("uses a recursive CTE")
    if isinstance(node, exp.In) and (node.args.get("query") or node.args.get("unnest")):
        raise Refusal("uses IN over a subquery")
    if isinstance(node, exp.Subquery) and not isinstance(node.parent, exp.From):
        raise Refusal("uses a subquery outside FROM")
    if isinstance(node, exp.Select):
        order = node.args.get("order")
        if order is not None and any(node.args.get(k) is not None
                                     for k in ("limit", "offset", "fetch")):
            raise Refusal("uses LIMIT/OFFSET")
    if isinstance(node, exp.Distinct) and not isinstance(node.parent, _AGGREGATES):
        raise Refusal("uses DISTINCT")
    for child in node.iter_expressions():
        _check_grammar(child)


def _has_aggregate(node: exp.Expression) -> bool:
    return node.find(*_AGGREGATES) is not None


def _is_constant(node: exp.Expression) -> bool:
    return node.find(exp.Column) is None and not _has_aggregate(node)


def _aggregates_outside_from(select: exp.Select) -> bool:
    """An aggregate in any clause of `select` itself (projections, WHERE,
    GROUP BY, HAVING, ORDER BY) — not in the sources it reads."""
    return any(_has_aggregate(v) for k, v in select.args.items()
               if k not in ("from", "from_", "with", "with_", "joins")
               and isinstance(v, exp.Expression)) or any(
        _has_aggregate(p) for p in select.expressions)


# ------------------------------------------------------------------ builder

@dataclass
class _Source:
    table: exp.Table               # the base table, aliased t0
    relation: str                  # its identity
    columns: dict[str, exp.Expression]   # visible name -> expression over t0
    order: list[str]               # visible names in projection order
    predicates: list[exp.Expression]     # conjuncts over t0 from inlined bodies


class _Builder:
    def __init__(self, registry, dialect: str, rules: list[str]):
        self.registry = registry
        self.dialect = dialect
        self.rules = rules

    def build(self, select: exp.Select) -> exp.Select:
        ctes = self._ctes(select)
        source = self._source(select, ctes, depth=0)
        used = set()
        self._mark_used(select, ctes, used)
        if set(ctes) - used:
            self.rules.append("unused_cte")
        return self._final(select, source)

    # ---- sources

    def _ctes(self, select: exp.Select) -> dict[str, exp.Select]:
        out: dict[str, exp.Select] = {}
        with_ = select.args.get("with") or select.args.get("with_")
        for cte in (with_.expressions if with_ is not None else []):
            name = cte.alias_or_name.lower()
            if name in out:
                raise Refusal(f"CTE {name} is defined twice")
            if not isinstance(cte.this, exp.Select):
                raise Refusal(f"CTE {name} is not a single SELECT")
            if cte.this.args.get("with") or cte.this.args.get("with_"):
                raise Refusal(f"CTE {name} has its own WITH")
            out[name] = cte.this
        return out

    def _mark_used(self, select: exp.Select, ctes: dict, used: set) -> None:
        from_ = select.args.get("from") or select.args.get("from_")
        src = from_.this if from_ is not None else None
        if isinstance(src, exp.Table) and not src.args.get("db") \
                and src.name.lower() in ctes and src.name.lower() not in used:
            used.add(src.name.lower())
            self._mark_used(ctes[src.name.lower()], ctes, used)
        elif isinstance(src, exp.Subquery) and isinstance(src.this, exp.Select):
            self._mark_used(src.this, ctes, used)

    def _source(self, select: exp.Select, ctes: dict, depth: int) -> _Source:
        if depth > 16:
            raise Refusal("source chain too deep")
        from_ = select.args.get("from") or select.args.get("from_")
        if from_ is None:
            raise Refusal("no FROM source")
        src = from_.this
        if isinstance(src, exp.Table) and not src.args.get("db") and src.name.lower() in ctes:
            body = ctes[src.name.lower()]
            self.rules.append("inline")
            inner = self._inline_body(body, ctes, depth)
            return self._aliased(inner, src.alias_or_name)
        if isinstance(src, exp.Subquery):
            if not isinstance(src.this, exp.Select):
                raise Refusal("derived table is not a single SELECT")
            self.rules.append("inline")
            inner = self._inline_body(src.this, ctes, depth)
            return self._aliased(inner, src.alias_or_name)
        if not isinstance(src, exp.Table):
            raise Refusal(f"unsupported source {type(src).__name__}")
        return self._base(src)

    def _aliased(self, source: _Source, alias: str) -> _Source:
        source.alias = alias.lower() if alias else None  # type: ignore[attr-defined]
        return source

    def _base(self, table: exp.Table) -> _Source:
        name = table_fqname(table)
        relation = self.registry.canonical_relation(name)
        columns = self.registry.columns(relation)
        if not columns:
            if self.registry.under_qualified(name):
                raise Refusal(
                    f"relation identity depends on the session: {name} is not fully "
                    f"qualified for this estate ({self.registry.estate_depth()}-part names) "
                    f"and no default database/schema was supplied", "identity")
            shorter = self.registry.shorter_spellings(relation)
            if shorter:
                # Borrowing a short entry's columns would assume it IS this
                # table — the suffix match round seven removed from identity.
                raise Refusal(
                    f"relation {name} is not in the catalog; it lists only "
                    f"{shorter[0]}, whose columns are not assumed to be this "
                    f"table's — describe the relation at the estate's depth, or "
                    f"supply the execution context", "identity")
            raise Refusal(f"relation {name} has no known columns", "identity")
        lowered: dict[str, str] = {}
        for c in columns:
            if c.lower() in lowered:
                raise Refusal(f"relation {name} has columns differing only by case",
                              "identity")
            lowered[c.lower()] = c
        parts = relation.split(".")
        base = exp.Table(this=exp.to_identifier(parts[-1], quoted=True),
                         db=exp.to_identifier(parts[-2], quoted=True) if len(parts) > 1 else None,
                         catalog=(exp.to_identifier(parts[-3], quoted=True)
                                  if len(parts) > 2 else None),
                         alias=exp.TableAlias(this=exp.to_identifier("t0")))
        self.rules.append("table_alias")
        src = _Source(table=base, relation=relation,
                      columns={k: exp.column(v, table="t0", quoted=True)
                               for k, v in lowered.items()},
                      order=list(lowered), predicates=[])
        src.alias = (table.alias_or_name or table.name).lower()  # type: ignore[attr-defined]
        src.names = {table.name.lower(), name.lower(), relation.lower()}  # type: ignore[attr-defined]
        return src

    def _inline_body(self, body: exp.Select, ctes: dict, depth: int) -> _Source:
        """A select-project-filter body, resolved to its base table."""
        for key, why in (("group", "GROUP BY"), ("having", "HAVING")):
            if body.args.get(key) is not None:
                raise Refusal(f"an inlined CTE/derived table uses {why}")
        # ANY clause: an aggregate only in ORDER BY still makes the body a
        # one-row aggregate (sweep 2, #3), and dropping that ORDER BY made it
        # a scan of every row.
        if _aggregates_outside_from(body):
            raise Refusal("an inlined CTE/derived table aggregates")
        inner = self._source(body, ctes, depth + 1)
        columns: dict[str, exp.Expression] = {}
        order: list[str] = []
        for proj in body.expressions:
            if isinstance(proj, exp.Star) or (isinstance(proj, exp.Column)
                                              and isinstance(proj.this, exp.Star)):
                self.rules.append("star")
                for name in inner.order:
                    if name in columns:
                        raise Refusal(f"duplicate output column {name}")
                    columns[name] = inner.columns[name].copy()
                    order.append(name)
                continue
            name = proj.alias_or_name.lower()
            if not name:
                raise Refusal("an inlined projection has no name")
            if name in columns:
                raise Refusal(f"duplicate output column {name}")
            target = proj.this if isinstance(proj, exp.Alias) else proj
            columns[name] = self._resolve(target.copy(), inner)
            order.append(name)
        if body.args.get("order") is not None:
            self.rules.append("order")
        where = body.args.get("where")
        preds = list(inner.predicates)
        preds += [self._resolve(c.copy(), inner) for c in
                  flatten_conjuncts(where.this if where is not None else None)]
        out = _Source(table=inner.table, relation=inner.relation, columns=columns,
                      order=order, predicates=preds)
        out.names = set()  # type: ignore[attr-defined]
        return out

    # ---- name resolution

    def _resolve(self, node: exp.Expression, source: _Source,
                 outputs: dict[str, exp.Expression] | None = None) -> exp.Expression:
        """Replace every column reference with its expression over t0.
        `outputs` (projection aliases) are consulted only for names that are
        NOT source columns; a name that is both is refused."""
        if isinstance(node, exp.Column):
            return self._column(node, source, outputs)
        for key, value in list(node.args.items()):
            if isinstance(value, list):
                node.set(key, [self._resolve(v, source, outputs)
                               if isinstance(v, exp.Expression) else v for v in value])
            elif isinstance(value, exp.Expression):
                node.set(key, self._resolve(value, source, outputs))
        return node

    def _column(self, col: exp.Column, source: _Source,
                outputs: dict[str, exp.Expression] | None) -> exp.Expression:
        if isinstance(col.this, exp.Star):
            raise Refusal("a * outside a projection list")
        name = col.name.lower()
        qualifier = ".".join(p.name.lower() for p in
                             (col.args.get("catalog"), col.args.get("db"), col.args.get("table"))
                             if p is not None)
        if qualifier:
            known = {getattr(source, "alias", None), *getattr(source, "names", set())}
            if qualifier not in known and qualifier.split(".")[-1] not in known:
                raise Refusal(f"column {col.sql(self.dialect)} names an unknown source")
        if name in source.columns:
            if outputs and name in outputs and not qualifier \
                    and not _is_identity(outputs[name], source.columns[name]):
                raise Refusal(f"{name} is both a projection alias and a source column — "
                              f"engines bind it differently")
            target = source.columns[name].copy()
        elif outputs is not None and not qualifier and name in outputs:
            self.rules.append("output_alias")
            target = outputs[name].copy()
        else:
            raise Refusal(f"column {name} is not a column of the source", "identity")
        return _paren_for(target, col)

    # ---- final scope

    def _final(self, select: exp.Select, source: _Source) -> exp.Select:
        projections: list[exp.Expression] = []
        outputs: dict[str, exp.Expression] = {}
        for i, proj in enumerate(select.expressions):
            if isinstance(proj, exp.Star) or (isinstance(proj, exp.Column)
                                              and isinstance(proj.this, exp.Star)):
                self.rules.append("star")
                for name in source.order:
                    projections.append(exp.alias_(source.columns[name].copy(), name,
                                                  quoted=True))
                continue
            target = proj.this if isinstance(proj, exp.Alias) else proj
            resolved = self._resolve(target.copy(), source)
            name = (proj.alias_or_name or f"_col_{i}").lower()
            if name in outputs:
                raise Refusal(f"duplicate output name {name}")
            outputs[name] = resolved
            projections.append(exp.alias_(resolved.copy(), name, quoted=True))
        out = exp.Select(expressions=projections)
        out.set(_FROM_KEY, exp.From(this=source.table.copy()))
        where = select.args.get("where")
        conjuncts = [p.copy() for p in source.predicates]
        conjuncts += [self._resolve(c.copy(), source) for c in
                      flatten_conjuncts(where.this if where is not None else None)]
        if conjuncts:
            self.rules.append("conjuncts")
            out.set("where", exp.Where(this=_and(conjuncts)))
        group = select.args.get("group")
        if group is not None:
            keys = self._group_keys(group, select, source, outputs)
            if keys:
                out.set("group", exp.Group(expressions=keys))
        having = select.args.get("having")
        if having is not None:
            out.set("having", exp.Having(this=self._resolve(having.this.copy(), source,
                                                            outputs)))
        order = select.args.get("order")
        if order is not None:
            # Removing ORDER BY is neutral only when it decides nothing but
            # order: an aggregate there makes an otherwise row-level query an
            # aggregate one.
            if _has_aggregate(order) and not (
                    group is not None or having is not None
                    or any(_has_aggregate(p) for p in select.expressions)):
                raise Refusal("ORDER BY aggregates a query that otherwise does not")
            self.rules.append("order")
        return out

    def _group_keys(self, group: exp.Group, select: exp.Select, source: _Source,
                    outputs: dict[str, exp.Expression]) -> list[exp.Expression]:
        if group.args.get("all"):
            self.rules.append("group_all")
            keys = [v.copy() for v in outputs.values()
                    if not _has_aggregate(v) and not _is_constant(v)]
            if not keys and any(not _has_aggregate(v) for v in outputs.values()):
                raise Refusal("GROUP BY ALL over only constants")
            return keys
        keys = []
        projections = list(outputs.values())
        for item in group.expressions:
            if isinstance(item, exp.Literal) and item.is_int:
                idx = int(item.name) - 1
                if not 0 <= idx < len(projections):
                    raise Refusal(f"GROUP BY {item.name} is not a projection ordinal")
                if _has_aggregate(projections[idx]):
                    raise Refusal("GROUP BY ordinal names an aggregate")
                self.rules.append("group_ordinal")
                keys.append(projections[idx].copy())
                continue
            resolved = self._resolve(item.copy(), source, outputs)
            if _has_aggregate(resolved):
                raise Refusal("GROUP BY names an aggregate")
            keys.append(resolved)
        return keys


def _is_identity(output: exp.Expression, column: exp.Expression) -> bool:
    return isinstance(output, exp.Column) and output.sql() == column.sql()


def _paren_for(target: exp.Expression, original: exp.Column) -> exp.Expression:
    """Parenthesize a substituted expression when its new parent binds tighter
    (or equally, on the right of a non-associative operator)."""
    parent = original.parent
    sub = _rank(target)
    if sub is None or parent is None:
        return target
    outer = _rank(parent)
    if outer is None:
        return target
    right = parent.args.get("expression") is original
    if sub < outer or (sub == outer and right):
        return exp.Paren(this=target)
    return target


def _expand_between(ast: exp.Expression, rules: list[str]) -> exp.Expression:
    """Rule `between`: SQL defines `x BETWEEN a AND b` as `x >= a AND x <= b`
    (three-valued logic included); the grammar's expressions are
    deterministic, so evaluating `x` twice changes nothing. SYMMETRIC is a
    different predicate and is refused."""
    for node in list(ast.find_all(exp.Between)):
        if node.args.get("symmetric"):
            raise Refusal("uses BETWEEN SYMMETRIC")
        expanded = exp.And(this=exp.GTE(this=node.this.copy(), expression=node.args["low"].copy()),
                           expression=exp.LTE(this=node.this.copy(),
                                              expression=node.args["high"].copy()))
        if node is ast:
            return expanded
        node.replace(expanded)
        rules.append("between")
    return ast


def strip_parens(ast: exp.Expression) -> exp.Expression:
    """Rule `parens`, first half: parentheses are syntax, not semantics — the
    parse tree already records the grouping they wrote — so every Paren node
    is removed. `parenthesize` then puts back exactly the ones the tree needs
    to be RENDERED faithfully, so `(status = 'completed')` and
    `status = 'completed'` have one canonical text."""
    def strip(node):
        if not isinstance(node, exp.Expression):
            return node
        while isinstance(node, exp.Paren):
            node = node.this
        for key, value in list(node.args.items()):
            if isinstance(value, list):
                node.set(key, [strip(v) for v in value])
            elif isinstance(value, exp.Expression):
                node.set(key, strip(value))
        return node
    return strip(ast)


_COMPARISON = 3   # rank of the comparison group in _PRECEDENCE
_NEG = 6


def parenthesize(ast: exp.Expression) -> exp.Expression:
    """Rule `parens`, second half: the minimum parentheses operator precedence
    requires for the rendered text to parse back to THIS tree. A child binding
    more weakly than its operator is wrapped; so is one at the same level on
    the right (`a - (b - c)`, `a + (b + c)` — regrouping is not exact for
    floats), and one at the same level anywhere under a comparison, BETWEEN,
    IN, IS or LIKE, which do not associate. Function arguments, CASE parts,
    projections and conjuncts need none."""
    def needs(parent: exp.Expression, key: str, child: exp.Expression) -> bool:
        inner = _rank(child)
        if inner is None:
            return False
        if isinstance(parent, exp.Neg):
            return inner <= _NEG
        if isinstance(parent, exp.Not):
            return inner < _rank(parent)
        if isinstance(parent, (exp.Between, exp.In, exp.Is)):
            return key in ("this", "low", "high") and inner <= _COMPARISON
        outer = _rank(parent)
        if outer is None or key not in ("this", "expression"):
            return False
        if outer == _COMPARISON:
            return inner <= outer
        return inner < outer or (inner == outer and key == "expression")

    def walk(node):
        for key, value in list(node.args.items()):
            if isinstance(value, list):
                node.set(key, [walk(v) if isinstance(v, exp.Expression) else v
                               for v in value])
            elif isinstance(value, exp.Expression):
                child = walk(value)
                node.set(key, exp.Paren(this=child) if needs(node, key, child) else child)
        return node
    return walk(strip_parens(ast))


def round_trips(ast: exp.Expression, dialect: str) -> bool:
    """The canonical text parses back to the canonical tree. Cores are compared
    as RENDERED text, so a rendering two trees could share would let two
    different queries hash alike; this check refuses that instead."""
    try:
        return sqlglot.parse_one(ast.sql(dialect=dialect), read=dialect) == ast
    except Exception:  # noqa: BLE001 — a rendering that does not parse fails the check
        return False


def _and(conjuncts: list[exp.Expression]) -> exp.Expression:
    wrapped = [c if _rank(c) is None or _rank(c) > _rank(exp.And(this=None)) else
               exp.Paren(this=c) for c in conjuncts]
    out = wrapped[0]
    for c in wrapped[1:]:
        out = exp.And(this=out, expression=c)
    return out
