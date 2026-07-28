"""Canonicalization pipeline (§3 of the architecture spec).

Ladder of degradation, never a hard failure:

    OPTIMIZED  — sqlglot full optimize: qualify identifiers, expand *,
                 inline CTEs / merge subqueries, CNF + constant folding.
    QUALIFIED  — identifiers resolved against the catalog, no rewriting.
    PARSED     — AST only; equality on this level is never proof.
    FAILED     — unparseable; artifact is recorded but unfingerprinted.

On top of sqlglot's passes we add two normalizations that canonical *text*
equality needs and sqlglot does not guarantee:

  * deterministic table aliases (t0, t1, ... assigned by sorted relation
    name), so author-chosen aliases never show up as semantic differences;
  * operand ordering for symmetric operators (AND/OR/EQ/NEQ/ADD/MUL) and
    NOT-elimination over comparisons, so `not a = b` == `b <> a`.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer import optimize
from sqlglot.optimizer.qualify import qualify

from semantix.models import CanonLevel
from semantix.extraction.sqlutil import render, scope_sources, table_fqname, unwrap_select


@dataclass
class CanonResult:
    ast: exp.Expression | None
    level: CanonLevel
    error: str | None = None
    # Constructs visible in the SOURCE that the optimizer then destroys, so the
    # completeness gate can never see them in the optimized AST. Merged into
    # the fingerprint's gaps by extract_fingerprints.
    gaps: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.ast is not None


# Aggregates whose result depends on the order of the rows they consume. If
# one of these is present, an ORDER BY in an inner scope is NOT decoration.
_ORDER_SENSITIVE = (
    "FIRST", "LAST", "ANY_VALUE", "ARG_MAX", "ARG_MIN", "ARBITRARY",
    "ARRAY_AGG", "STRING_AGG", "LISTAGG", "GROUP_CONCAT", "COLLECT_LIST",
)


def _pre_optimization_gaps(parsed: exp.Expression, dialect: str) -> tuple[str, ...]:
    """Gaps that only exist BEFORE the optimizer runs.

    sqlglot's subquery merge folds a CTE into its parent and discards the
    CTE's ORDER BY. That ordering is load-bearing whenever the scope above
    consumes it with an order-sensitive aggregate — `first(amount)` over a
    CTE ordered ASC and the same CTE ordered DESC compute different numbers
    — and by the time the gate looks at the optimized AST there is nothing
    left to see. We cannot preserve it, so we refuse to certify through it.

    Only when an order-sensitive consumer is actually present: an ORDER BY in
    a staging model is otherwise genuinely neutral, and gating every one of
    them would make ordinary dbt SQL uncertifiable.
    """
    outermost = unwrap_select(parsed)
    inner_ordered = any(
        select is not outermost and select.args.get("order") is not None
        for select in parsed.find_all(exp.Select)
    )
    if not inner_ordered:
        return ()
    has_window = parsed.find(exp.Window) is not None
    consumers = {type(node).__name__.upper() for node in parsed.find_all(exp.AggFunc)}
    consumers |= {str(node.this).upper() for node in parsed.find_all(exp.Anonymous)}
    if has_window or consumers & set(_ORDER_SENSITIVE):
        return ("inner ORDER BY consumed by an order-sensitive aggregate",)
    return ()


def canonicalize_sql(sql: str, schema, dialect: str) -> CanonResult:
    """`schema` is a raw nested dict or a prebuilt sqlglot MappingSchema;
    the registry hands over the latter so it is normalized once per scan
    rather than once per model."""
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception as e:  # noqa: BLE001 — adapter input is arbitrary SQL
        return CanonResult(None, CanonLevel.FAILED, f"parse: {e}")

    # Read what the optimizer is about to destroy, before it runs.
    pre_gaps = _pre_optimization_gaps(parsed, dialect)

    _strip_neutral(parsed)
    _resolve_group_ordinals(parsed)

    error = None
    try:
        ast = optimize(parsed.copy(), schema=schema, dialect=dialect)
        level = CanonLevel.OPTIMIZED
    except Exception as e1:  # noqa: BLE001
        error = f"optimize: {e1}"
        try:
            ast = qualify(parsed.copy(), schema=schema, dialect=dialect)
            level = CanonLevel.QUALIFIED
        except Exception as e2:  # noqa: BLE001
            return CanonResult(parsed, CanonLevel.PARSED,
                               f"{error}; qualify: {e2}", pre_gaps)

    try:
        ast = _canonical_table_aliases(ast)
        ast = _normalize_symmetric(ast, dialect)
    except Exception as e3:  # noqa: BLE001
        # Normalization is best-effort; a partially normalized AST is still
        # usable, it just weakens equality to a hypothesis — and THAT is what
        # the level must say. It previously stayed at a PROOF level, so a
        # certificate could be issued from an AST the engine had already
        # recorded that it failed to canonicalize. Neither step is atomic, so
        # a throw mid-loop leaves partially renamed tables behind.
        error = f"{error + '; ' if error else ''}normalize: {e3}"
        level = CanonLevel.PARSED

    return CanonResult(ast, level, error, pre_gaps)


def _strip_neutral(ast: exp.Expression) -> None:
    """Drop genuinely semantics-neutral constructs.

    ORDER BY alone does not change a measure's value, so it is stripped —
    but ONLY when nothing bounds the output. `ORDER BY x LIMIT 10` is a
    top-N measure and the ordering is load-bearing; stripping either half
    would make it identical to the unbounded total. LIMIT/OFFSET are never
    stripped: they are semantic, and the completeness gate needs to see
    them. (Window-internal ordering lives on exp.Window, untouched here.)
    """
    # Only the OUTERMOST select's ordering is neutral. Inside a CTE or derived
    # table, ORDER BY is load-bearing for order-sensitive aggregates in the
    # scope above — FIRST, LAST, ANY_VALUE, ARG_MAX, ARRAY_AGG — so stripping
    # it made a staging CTE ordered ASC hash identically to the same CTE
    # ordered DESC, which computes different numbers. Stripping happens before
    # CTE bodies are digested, so the digest could not recover it either.
    outermost = unwrap_select(ast)
    for select in ast.find_all(exp.Select):
        if select is not outermost:
            continue
        bounded = select.args.get("limit") is not None or select.args.get("offset") is not None
        if not bounded:
            select.set("order", None)


def _resolve_group_ordinals(ast: exp.Expression) -> None:
    """Rewrite GROUP BY 1 to the projection expression it points at, so
    ordinal and explicit grouping canonicalize identically."""
    for select in ast.find_all(exp.Select):
        group = select.args.get("group")
        if group is None:
            continue
        # Ordinals inside ROLLUP(...) / CUBE(...) / GROUPING SETS(...) live in
        # their OWN args, not in group.expressions. Iterating only the latter
        # left `GROUP BY ROLLUP(1, 2)` unresolved, so the grain was
        # non-empty but content-free — `rollup(1,2)` over (status, channel)
        # hashed identically to `rollup(1,2)` over (channel, status), which
        # are different breakdowns with different subtotal rows.
        containers: list[exp.Expression] = [group]
        for construct in ("rollup", "cube", "grouping_sets"):
            containers += [n for n in (group.args.get(construct) or [])
                           if isinstance(n, exp.Expression)]
        for container in containers:
            for item in list(container.find_all(exp.Literal)):
                if not item.is_int:
                    continue
                idx = int(item.this) - 1
                if 0 <= idx < len(select.expressions):
                    target = select.expressions[idx]
                    target = target.this if isinstance(target, exp.Alias) else target
                    item.replace(target.copy())


def normalize_scope(ast: exp.Expression, dialect: str) -> exp.Expression:
    """Apply the alias and operand normalizations to one scope.

    Exposed because CTE bodies need the same treatment as the top-level
    select: the optimizer qualifies them but leaves the author's table
    aliases in place, so hashing a body's rendered text made a rename inside
    a CTE look like a definition change.
    """
    if isinstance(ast, (exp.Union, exp.Except, exp.Intersect)):
        # A set operation has no single scope: `_canonical_table_aliases`
        # unwraps to a Select, finds none, and silently returns the AST
        # untouched — so an alias rename inside a UNION'd CTE body changed its
        # digest and fabricated a contradiction. Normalize each branch.
        for side in ("this", "expression"):
            branch = ast.args.get(side)
            if isinstance(branch, exp.Expression):
                ast.set(side, normalize_scope(branch, dialect))
        return _normalize_symmetric(ast, dialect)
    return _normalize_symmetric(_canonical_table_aliases(ast), dialect)


def _canonical_table_aliases(ast: exp.Expression) -> exp.Expression:
    """Assign deterministic aliases t0..tn to the top scope's tables, ordered
    by relation name. Ambiguous re-used ref names are left untouched — a
    missed rename degrades equality to a difference-hypothesis, which is the
    safe direction."""
    select = unwrap_select(ast)
    if select is None:
        return ast

    tables = [s for s in scope_sources(select) if isinstance(s, exp.Table)]
    if not tables:
        return ast

    ordered = sorted(enumerate(tables), key=lambda it: (table_fqname(it[1]), it[0]))

    rename: dict[str, str] = {}
    ambiguous: set[str] = set()
    assignments: list[tuple[exp.Table, str]] = []
    for new_idx, (_, table) in enumerate(ordered):
        old = table.alias_or_name
        new = f"t{new_idx}"
        assignments.append((table, new))
        if old in rename:
            ambiguous.add(old)
        else:
            rename[old] = new

    for old in ambiguous:
        rename.pop(old, None)

    for table, new in assignments:
        if table.alias_or_name in ambiguous:
            continue
        # Replacing the whole TableAlias node discarded its COLUMN RENAME list.
        # `raw.refunds t(a, b, c)` and `raw.refunds t(b, a, c)` then became the
        # same relation, so `sum(a)` meant refund_id in one and order_id in the
        # other — and both reported source_columns=('raw.refunds.a',), a
        # physical lineage naming a column that does not exist. Receipts cite
        # that field as lineage, so this was fabrication, not omission.
        existing = table.args.get("alias")
        columns = existing.args.get("columns") if isinstance(existing, exp.TableAlias) else None
        table.set("alias", exp.TableAlias(this=exp.to_identifier(new),
                                          **({"columns": columns} if columns else {})))

    for column in ast.find_all(exp.Column):
        old = column.table
        if old and old in rename:
            column.set("table", exp.to_identifier(rename[old]))

    return ast


_NOT_INVERSE: dict[type, type] = {exp.EQ: exp.NEQ, exp.NEQ: exp.EQ}


def _flatten_chain(node: exp.Expression, cls: type) -> list[exp.Expression]:
    out: list[exp.Expression] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if type(n) is cls:
            stack.append(n.this)
            stack.append(n.expression)
        else:
            out.append(n)
    return out


def _normalize_symmetric(ast: exp.Expression, dialect: str) -> exp.Expression:
    """Explicit post-order rewrite. sqlglot's Expression.transform is
    pre-order and does not descend into replacement nodes, which silently
    skips exactly the rewrites this pass exists for."""
    key = functools.partial(render, dialect=dialect)

    def sort_rank(node: exp.Expression) -> tuple[int, str]:
        # Columns before literals keeps `status = 'completed'` readable while
        # staying fully deterministic.
        is_literalish = 1 if node.find(exp.Column) is None else 0
        return (is_literalish, key(node))

    def rebuild(cls: type, operands: list[exp.Expression]) -> exp.Expression:
        return functools.reduce(lambda a, b: cls(this=a, expression=b), operands)

    def norm(node):
        if not isinstance(node, exp.Expression):
            return node
        for arg_key, value in list(node.args.items()):
            if isinstance(value, list):
                node.set(arg_key, [norm(v) for v in value])
            elif isinstance(value, exp.Expression):
                node.set(arg_key, norm(value))

        if isinstance(node, exp.Not) and type(node.this) in _NOT_INVERSE:
            inner = node.this
            inverse = _NOT_INVERSE[type(inner)]
            return inverse(this=inner.this, expression=inner.args.get("expression"))
        if isinstance(node, (exp.And, exp.Or, exp.Add, exp.Mul)):
            operands = _flatten_chain(node, type(node))
            if len(operands) > 1:
                return rebuild(type(node), sorted(operands, key=sort_rank))
        elif isinstance(node, (exp.EQ, exp.NEQ)):
            left, right = node.this, node.args.get("expression")
            if right is not None and sort_rank(left) > sort_rank(right):
                return type(node)(this=right, expression=left)
        return node

    return norm(ast)
