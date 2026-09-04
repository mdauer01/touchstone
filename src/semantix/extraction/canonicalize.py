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
# Compared with underscores stripped: sqlglot's CLASS names have none
# (ArrayAgg, GroupConcat, AnyValue), while the SQL spellings do — comparing
# the raw class name meant nine of these eleven entries could NEVER match, so
# `array_agg`/`string_agg`/`any_value` over a merged-away inner ORDER BY
# falsely certified. Only FIRST and LAST — the two the round-four test used —
# happened to match.
_ORDER_SENSITIVE = (
    "FIRST", "LAST", "ANY_VALUE", "ARG_MAX", "ARG_MIN", "ARBITRARY",
    "ARRAY_AGG", "STRING_AGG", "LISTAGG", "GROUP_CONCAT", "COLLECT_LIST",
    "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE",
    # MODE tie-breaks on first-seen; ARRAY_CONCAT_AGG concatenates in input
    # order per BigQuery's own docs. Each was a one-word omission from the
    # table the previous round audited, and each certified a merged-away
    # inner ORDER BY at proof grade.
    "MODE", "ARRAY_CONCAT_AGG",
)
_ORDER_SENSITIVE_NORM = frozenset(n.replace("_", "") for n in _ORDER_SENSITIVE)

# Wrappers that sit BETWEEN a window (or projection) and the function it
# applies: `first_value(x IGNORE NULLS)` parses as IgnoreNulls(FirstValue).
# Reading `window.this` directly saw "IGNORENULLS" — never in the table — so
# the mainstream BigQuery/Snowflake navigation idiom defeated the gate.
_FN_WRAPPERS = (exp.IgnoreNulls, exp.RespectNulls, exp.Filter)


def _fn_name(fn: exp.Expression) -> str:
    while isinstance(fn, _FN_WRAPPERS):
        fn = fn.this
    return (str(fn.this) if isinstance(fn, exp.Anonymous)
            else type(fn).__name__).upper().replace("_", "")


def _has_distinct_on(select: exp.Select) -> bool:
    """DISTINCT ON is an order-sensitive consumer that is NOT an aggregate:
    its ORDER BY decides which row survives per key. It was invisible to both
    consumer checks, so the deciding ordering was stripped and asc/desc
    variants falsely certified — the canonical Postgres/DuckDB dedup idiom.
    Both consumer checks call this; the check must never live in one of
    them alone."""
    distinct = select.args.get("distinct")
    return isinstance(distinct, exp.Expression) and bool(distinct.args.get("on"))


def _order_sensitive_names(parsed: exp.Expression) -> set[str]:
    """Normalized names of order-sensitive functions used OUTSIDE windows.

    A function inside an OVER clause takes its ordering from the window spec,
    not from the input rows, and is handled by _window_gaps below.
    """
    names: set[str] = set()
    for node in parsed.find_all(exp.AggFunc):
        if node.find_ancestor(exp.Window) is None:
            names.add(type(node).__name__.upper().replace("_", ""))
    for node in parsed.find_all(exp.Anonymous):
        if node.find_ancestor(exp.Window) is None:
            names.add(str(node.this).upper().replace("_", ""))
    return names & _ORDER_SENSITIVE_NORM


def _window_gaps(parsed: exp.Expression) -> bool:
    """Does any window depend on INPUT order rather than its own spec?

    A blanket `parsed.find(exp.Window)` made ANY window function plus ANY
    inner ORDER BY fire the gap — so mainstream ranked-summary SQL
    (`rank() over (order by sum(x))` over an ordered staging CTE) could never
    certify, and the gap text asserted a consumer that does not exist. A
    window with its OWN ORDER BY is self-specified; only an order-sensitive
    function over a spec WITHOUT one actually consumes input order.
    """
    for window in parsed.find_all(exp.Window):
        if window.args.get("order") is not None:
            continue
        if _fn_name(window.this) in _ORDER_SENSITIVE_NORM:
            return True
    return False


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
    if not _has_inner_order(parsed):
        return ()
    # DISTINCT ON without its own ORDER BY is a consumer too — the gate
    # enumerated only functions and windows here, so the chain below was
    # never even examined for it and asc/desc variants certified equal.
    inherits_order = any(_has_distinct_on(s) and s.args.get("order") is None
                         for s in parsed.find_all(exp.Select))
    if not (_order_sensitive_names(parsed) or _window_gaps(parsed) or inherits_order):
        return ()
    # The gap fires only when an inner ORDER BY sits in a scope a consumer
    # actually READS. The whole-AST test fired on a decorative ordering in an
    # unrelated CTE whenever any consumer existed anywhere — the same
    # coupling that made _strip_neutral fabricate differences. Preserved
    # DISTINCT ON orderings do not gap: they survive stripping and are
    # modelled into the core.
    keep = _order_bearing_selects(parsed)
    outermost = unwrap_select(parsed)
    for select in parsed.find_all(exp.Select):
        if select is outermost or select.args.get("order") is None:
            continue
        if _has_distinct_on(select):
            continue
        if id(select) in keep:
            return ("inner ORDER BY consumed by an order-sensitive aggregate",)
    return ()


def _has_inner_order(parsed: exp.Expression) -> bool:
    outermost = unwrap_select(parsed)
    return any(
        select is not outermost and select.args.get("order") is not None
        for select in parsed.find_all(exp.Select)
    )


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
    # A bind parameter (`$1`, `?`, `:status`) is a value the text does not
    # carry; a core built over it describes a FAMILY of queries. Deny by
    # default: it can be reported and diffed, never certified — and never
    # accused of "differing filters" against a concrete value.
    if parsed.find(exp.Placeholder) is not None or parsed.find(exp.Parameter) is not None:
        pre_gaps = pre_gaps + ("bind parameter placeholder in query text",)

    _pin_short_references(parsed, schema)
    _strip_neutral(parsed)
    pre_gaps = pre_gaps + _resolve_group_ordinals(parsed)

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
        # The `_` pin has done its job (qualification); it must not survive
        # into the canonical text, where it named a relation that does not
        # exist in blame and receipts.
        for table in ast.find_all(exp.Table):
            for part in ("catalog", "db"):
                ident = table.args.get(part)
                if ident is not None and ident.name == "_":
                    table.set(part, None)
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


def _pin_short_references(ast: exp.Expression, schema) -> None:
    """Pin references matching a SHORT catalog entry to the `_` home.

    The registry stores a short entry (a two-part hand-catalog table in a
    three-part estate) under `_` AND under a replica per observed database.
    Without pinning, a short reference races the replicas and sqlglot
    refuses as ambiguous the moment two databases exist; with pinning, short
    references match the `_` home exactly while fully-qualified references
    match their database's replica — both forms bind on multi-database
    estates. The replicas are copies of ONE physical table, so the pin
    changes nothing about meaning or lineage.
    """
    short = getattr(schema, "_semantix_short_entries", None)
    if not short:
        return
    ctes = {cte.alias_or_name.lower() for cte in ast.find_all(exp.CTE)}
    for table in ast.find_all(exp.Table):
        if table.args.get("catalog"):
            continue
        name = table.name.lower()
        if not table.args.get("db"):
            if name in ctes:
                continue
            key: tuple[str, ...] = (name,)
        else:
            key = (table.args["db"].name.lower(), name)
        pad = short.get(key)
        if pad is None and len(key) == 1:
            # A BARE one-part reference has no db to match a `(db, name)`
            # short entry, so on a mixed-depth estate it raced the replicas
            # (`raw._` vs `raw.prod`) and sqlglot refused it as ambiguous —
            # a coverage regression against round five. When exactly one
            # short entry ends in this name, that is its home.
            tails = [k for k in short if len(k) > 1 and k[-1] == name]
            if len(tails) == 1:
                key = tails[0]
                pad = short[key]
                table.set("db", exp.to_identifier(key[-2]))
                if len(key) == 3:
                    table.set("catalog", exp.to_identifier(key[0]))
        if pad is None:
            continue
        if len(key) == 2 and pad >= 1:
            table.set("catalog", exp.to_identifier("_"))
        elif len(key) == 1 and pad >= 1:
            table.set("db", exp.to_identifier("_"))
            if pad >= 2:
                table.set("catalog", exp.to_identifier("_"))


def _strip_neutral(ast: exp.Expression) -> None:
    """Drop genuinely semantics-neutral constructs.

    ORDER BY alone does not change a measure's value, so it is stripped —
    but ONLY when nothing bounds the output. `ORDER BY x LIMIT 10` is a
    top-N measure and the ordering is load-bearing; stripping either half
    would make it identical to the unbounded total. LIMIT/OFFSET are never
    stripped: they are semantic, and the completeness gate needs to see
    them. (Window-internal ordering lives on exp.Window, untouched here.)
    """
    # Inner ORDER BY is load-bearing ONLY for the scopes an order-sensitive
    # consumer actually READS. A whole-AST boolean kept DECORATIVE orderings
    # in unrelated CTEs whenever any consumer existed anywhere — a cosmetic
    # `order by 2` in a lookup CTE fabricated a difference between models
    # that compute identical numbers. The keep-set is the transitive closure
    # of each consumer's sources.
    keep = _order_bearing_selects(ast)
    outermost = unwrap_select(ast)
    for select in ast.find_all(exp.Select):
        if id(select) in keep and select is not outermost:
            continue
        if _has_distinct_on(select):
            # Stripping its ORDER BY certified asc/desc variants as identical
            # in both outer and inner positions.
            continue
        bounded = select.args.get("limit") is not None or select.args.get("offset") is not None
        if not bounded:
            select.set("order", None)


def _order_bearing_selects(ast: exp.Expression) -> set[int]:
    """ids of Selects whose ORDER BY an order-sensitive consumer can see."""
    # Lowercased: this runs on the PRE-normalization AST, and DuckDB/Snowflake
    # fold unquoted identifiers, so `WITH S AS (...) SELECT first(x) FROM s`
    # is one chain. Matching as written missed it, the ordering was stripped,
    # and asc/desc variants certified equal at proof grade.
    bodies: dict[str, exp.Expression] = {
        cte.alias_or_name.lower(): cte.this for cte in ast.find_all(exp.CTE)
    }

    def consumer_selects() -> list[exp.Select]:
        out = []
        for node in ast.find_all(exp.AggFunc):
            if (node.find_ancestor(exp.Window) is None
                    and type(node).__name__.upper().replace("_", "")
                    in _ORDER_SENSITIVE_NORM):
                parent = node.find_ancestor(exp.Select)
                if parent is not None:
                    out.append(parent)
        for window in ast.find_all(exp.Window):
            if (window.args.get("order") is None
                    and _fn_name(window.this) in _ORDER_SENSITIVE_NORM):
                parent = window.find_ancestor(exp.Select)
                if parent is not None:
                    out.append(parent)
        # DISTINCT ON is the third order-sensitive consumer — neither an
        # aggregate nor a window. Without its own ORDER BY it inherits the
        # ordering of the scope it reads (Postgres: "unpredictable" without
        # one), so that inner ordering is load-bearing and must reach the
        # keep-set; both STRIPPING sites knew this, the consumer enumeration
        # did not, and asc/desc variants certified equal.
        for select in ast.find_all(exp.Select):
            if _has_distinct_on(select) and select.args.get("order") is None:
                out.append(select)
        return out

    keep: set[int] = set()

    def absorb_chain(select: exp.Select, seen: set[str]) -> None:
        """Follow SINGLE-source chains only. An aggregate over a JOIN has no
        defined input order regardless of either side's inner ORDER BY, so
        absorbing every referenced scope kept a decorative ordering in a
        joined lookup CTE and fabricated a difference between models that
        compute identical numbers."""
        sources = list(scope_sources(select))
        if len(sources) != 1:
            return
        source = sources[0]
        if isinstance(source, exp.Table) and not source.args.get("db"):
            name = source.name.lower()
            if name in bodies and name not in seen:
                seen.add(name)
                body = bodies[name]
                for inner in body.find_all(exp.Select):
                    keep.add(id(inner))
                inner_select = unwrap_select(body)
                if inner_select is not None:
                    absorb_chain(inner_select, seen)
        elif isinstance(source, exp.Subquery):
            for inner in source.find_all(exp.Select):
                keep.add(id(inner))
            inner_select = unwrap_select(source.this)
            if inner_select is not None:
                absorb_chain(inner_select, seen)

    for consumer in consumer_selects():
        absorb_chain(consumer, set())
    return keep


def _resolve_group_ordinals(ast: exp.Expression) -> tuple[str, ...]:
    """Rewrite GROUP BY 1 to the projection expression it points at, so
    ordinal and explicit grouping canonicalize identically. Returns gaps:
    an ordinal aimed at an aggregate is SQL no warehouse runs, and leaving
    the literal in place was not enough — sqlglot's own positional expansion
    spliced the aggregate into the grain downstream, so the unrunnable query
    still reached proof grade with a content-free grain."""
    gaps: list[str] = []
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
        # ONLY a literal that is itself a grouping ITEM is an ordinal. The
        # previous `container.find_all(exp.Literal)` descended into arbitrary
        # expressions, so the `2` in `GROUP BY status, customer_id % 2` was
        # "resolved" to the second projection — splicing a SUM into the middle
        # of a grouping expression. Two different hash-bucket groupings
        # (`% 2` vs `% 3`, both pointing at identical projections) then
        # collapsed to the same garbage grain and FALSELY CERTIFIED.
        def resolve_items(items: list) -> None:
            for item in items:
                if isinstance(item, exp.Tuple):  # GROUPING SETS ((a, 1), ...)
                    resolve_items(list(item.expressions))
                    continue
                if isinstance(item, exp.Paren):
                    # A single-item grouping set parses as Paren, not Tuple:
                    # `GROUPING SETS ((status), (2))` is [Paren, Paren]. The
                    # Tuple-only descent left `(2)` unresolved, so two
                    # different single-column breakdowns hashed identically
                    # at proof grade — and single-column parenthesized sets
                    # are the most common GROUPING SETS shape.
                    resolve_items([item.this])
                    continue
                if not (isinstance(item, exp.Literal) and item.is_int):
                    continue
                idx = int(item.this) - 1
                if 0 <= idx < len(select.expressions):
                    target = select.expressions[idx]
                    target = target.this if isinstance(target, exp.Alias) else target
                    if target.find(exp.AggFunc) is not None:
                        gaps.append("GROUP BY ordinal targets an aggregate "
                                    "projection (unrunnable grouping)")
                        continue
                    item.replace(target.copy())

        resolve_items(list(group.expressions))
        for construct in ("rollup", "cube", "grouping_sets"):
            for node in group.args.get(construct) or []:
                if isinstance(node, exp.Expression):
                    resolve_items(list(node.expressions))
    return tuple(gaps)


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

    # Rename columns whose qualifier RESOLVES to a top-scope table: those in
    # the top scope itself, and CORRELATED references from nested scopes whose
    # qualifier is not shadowed by a local source of the same name. Renaming
    # every scope blindly corrupted CTE bodies that reuse a top-scope alias
    # name (dbt convention); renaming ONLY the top scope left correlated
    # references on the AUTHOR's alias, so two authors' identical queries
    # hashed differently at proof grade and the canonical text carried a
    # dangling alias.
    def resolves_to_top(column: exp.Column) -> bool:
        scope = column.find_ancestor(exp.Select)
        while scope is not None and scope is not select:
            local = {s.alias_or_name for s in scope_sources(scope)}
            local |= {cte.alias_or_name for cte in (scope.ctes or [])}
            if column.table in local:
                return False  # shadowed by a nested source — not correlated
            scope = scope.find_ancestor(exp.Select)
        return scope is select

    for column in ast.find_all(exp.Column):
        old = column.table
        if not old or old not in rename:
            continue
        if not resolves_to_top(column):
            continue
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
