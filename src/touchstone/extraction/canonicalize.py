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
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.pushdown_projections import pushdown_projections
from sqlglot.optimizer.qualify import qualify

from touchstone.models import CanonLevel
from touchstone.extraction.sqlutil import (
    render,
    scope_sources,
    split_group,
    table_fqname,
    unwrap_select,
)


@dataclass
class CanonResult:
    ast: exp.Expression | None
    level: CanonLevel
    error: str | None = None
    # Constructs visible in the SOURCE that the optimizer then destroys, so the
    # completeness gate can never see them in the optimized AST. Merged into
    # the fingerprint's gaps by extract_fingerprints.
    gaps: tuple[str, ...] = ()
    # The proof policy's rules that produced this form (SUPPORTED only).
    rules: tuple[str, ...] = ()
    # The parser-disagreement detector's result on the ORIGINAL text, for
    # forms inside the grammar (extraction.parse_agreement.ParseCheck.as_dict).
    parse_check: dict | None = None

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


def _inherits_input_order(select: exp.Select) -> bool:
    """A select whose surviving rows depend on the order of its INPUT.

    DISTINCT ON keeps one row per key: unless its own ORDER BY breaks every
    tie, the input order decides which — and an ORDER BY that only repeats
    the key breaks none (it certified asc/desc staging CTEs as equal). Not
    being able to prove the ties broken, every DISTINCT ON counts. A
    LIMIT/OFFSET with no ORDER BY of its own keeps whichever rows arrive
    first, so top-N and bottom-N over an ordered CTE certified equal."""
    if _has_distinct_on(select):
        return True
    bounded = select.args.get("limit") is not None or select.args.get("offset") is not None
    return bounded and select.args.get("order") is None


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
    inherits_order = any(_inherits_input_order(s) for s in parsed.find_all(exp.Select))
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


def canonicalize(sql: str, registry, dialect: str) -> CanonResult:
    """The canonical form every fingerprint is built from.

    Proof path first (extraction.proof): inside the supported grammar the
    result is SUPPORTED, produced only by the policy's stated rules. Outside
    it, the general optimizer still supplies a form — useful for diffs,
    candidate matching and blame — at a HYPOTHESIS level, with the reason it
    is not proof carried as a gap. Nothing on that path can certify, alias
    or conform, and nothing on it is stated as a definitive difference."""
    from touchstone.extraction.proof import (GRAMMAR_GAP, IDENTITY_GAP, certify,
                                           parenthesize, round_trips, strip_parens)

    proof = certify(sql, registry, dialect)
    refusal = proof.reason
    if proof.supported:
        try:
            ast = parenthesize(_normalize_symmetric(strip_parens(proof.ast), dialect))
        except Exception as e:  # noqa: BLE001 — never certify a half-normalized form
            ast, refusal = None, f"normalization failed: {e}"
        if ast is not None and not round_trips(ast, dialect):
            ast, refusal = None, "the canonical text does not parse back to the same tree"
        check = None
        if ast is not None:
            # round_trips compares the parser only with itself; ask the
            # engine too, from the original text. A disagreement refuses; no
            # disagreement found qualifies nothing (extraction.parse_agreement).
            from touchstone.extraction.parse_agreement import DISAGREES, check_parse

            parse = check_parse(sql, dialect)
            check = parse.as_dict()
            if parse.status == DISAGREES:
                ast, refusal = None, (f"the parser and {parse.engine} {parse.engine_version} "
                                      f"read this statement differently — {parse.detail}")
        if ast is not None:
            try:
                # Type INFERENCE only (no rewrite): registered models keep their
                # output column types, which the catalog of downstream reads uses.
                annotate_types(ast, schema=registry.sqlglot_schema(dialect), dialect=dialect)
            except Exception:  # noqa: BLE001 — types are informational here
                pass
            return CanonResult(ast, CanonLevel.SUPPORTED, None, (),
                               tuple(dict.fromkeys((*proof.rules, "commutative",
                                                    "not_equality", "parens"))),
                               check)
    hypothesis = canonicalize_sql(sql, registry.sqlglot_schema(dialect), dialect)
    prefix = IDENTITY_GAP if proof.kind == "identity" and refusal == proof.reason \
        else GRAMMAR_GAP
    return CanonResult(hypothesis.ast, hypothesis.level, hypothesis.error,
                       (*hypothesis.gaps, f"{prefix}: {refusal}"))


def canonicalize_sql(sql: str, schema, dialect: str) -> CanonResult:
    """The general optimizer's canonical form — a HYPOTHESIS, never proof.
    Used for model registration (names, types, lineage), relationship
    mining, and as canonicalize()'s fallback outside the proof grammar.

    `schema` is a raw nested dict or a prebuilt sqlglot MappingSchema;
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
    # GROUP BY ALL / ORDER BY ALL mean "every projection": spelled out before
    # projection pruning can remove the very projections they stand for.
    pre_gaps = pre_gaps + _expand_all_shorthand(parsed)
    pre_gaps = pre_gaps + _ambiguous_output_refs(parsed, schema, dialect)
    pre_gaps = pre_gaps + _pruning_hazards(parsed, schema, dialect)

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
    estates. The pin and the replicas exist ONLY so columns get types: they
    are not evidence that two references read one table. Relation identity
    is decided separately (registry.canonical_relation), from what the SQL
    wrote plus operator-supplied context, and the `_` never survives into a
    core.
    """
    short = getattr(schema, "_touchstone_short_entries", None)
    # The operator-supplied session schema is the only thing allowed to fill
    # an omitted schema. Round seven GUESSED it from a unique short-entry
    # tail; with an explicit context naming a different schema the guess
    # overrode the operator, and without one it put a schema the SQL never
    # wrote into the reference's identity.
    _ctx_database, ctx_schema = getattr(schema, "_touchstone_context", (None, None))
    ctes = {cte.alias_or_name.lower() for cte in ast.find_all(exp.CTE)}
    for table in ast.find_all(exp.Table):
        if table.args.get("catalog"):
            continue
        name = table.name.lower()
        if not table.args.get("db"):
            if name in ctes:
                continue
            if ctx_schema:
                table.set("db", exp.to_identifier(ctx_schema))
                key: tuple[str, ...] = (ctx_schema.lower(), name)
            else:
                key = (name,)
        else:
            key = (table.args["db"].name.lower(), name)
        if not short:
            continue
        pad = short.get(key)
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
            if _inherits_input_order(select):
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

        plain, constructs = split_group(group)
        resolve_items(plain)
        for _, node in constructs:
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


_ADDITIVE = (exp.Add, exp.Sub)
_MULTIPLICATIVE = (exp.Mul, exp.Div, exp.Mod, exp.IntDiv)
_ARITHMETIC = _ADDITIVE + _MULTIPLICATIVE


def _same_level(node: exp.Expression) -> tuple[type, ...]:
    return _ADDITIVE if isinstance(node, _ADDITIVE) else _MULTIPLICATIVE


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
        if isinstance(node, (exp.And, exp.Or)):
            # Boolean AND/OR are associative and commutative in SQL's
            # three-valued logic, so the whole chain may be flattened and
            # sorted.
            operands = _flatten_chain(node, type(node))
            if len(operands) > 1:
                return rebuild(type(node), sorted(operands, key=sort_rank))
        elif isinstance(node, _ARITHMETIC):
            # Arithmetic is COMMUTATIVE but not ASSOCIATIVE. Swapping the two
            # operands of one + or * is exact for IEEE floats, integers and
            # decimals alike; regrouping across nested nodes is not. The first
            # version flattened `a + b + c` into a sorted chain, so
            # SUM(a + b + c) and SUM(a + c + b) hashed identically and
            # CERTIFIED — over DOUBLE values 1e16, -1e16, 1 they return 1 and
            # 0. Integer reassociation can also move where an overflow
            # happens. Evaluation structure is preserved; only each node's
            # own two children are ordered.
            if isinstance(node, (exp.Add, exp.Mul)):
                left, right = node.this, node.args.get("expression")
                if right is not None and sort_rank(left) > sort_rank(right):
                    node = type(node)(this=right, expression=left)
            # The tree is only half the story: equality is taken over the
            # RENDERED text, and sqlglot's simplifier strips parentheses it
            # deems mathematically redundant, so `a + (b + c)` and
            # `(a + b) + c` both rendered `a + b + c` — different evaluation
            # orders, one hash (found by the execution oracle: 1.6 vs 0.6).
            # A right operand at the SAME precedence level is exactly the
            # case the generator flattens, so it is parenthesized explicitly.
            right = node.args.get("expression")
            if isinstance(right, _same_level(node)):
                node.set("expression", exp.Paren(this=right))
            return node
        elif isinstance(node, (exp.EQ, exp.NEQ)):
            left, right = node.this, node.args.get("expression")
            if right is not None and sort_rank(left) > sort_rank(right):
                return type(node)(this=right, expression=left)
        return node

    return norm(ast)


def _is_all(node: exp.Expression | None) -> bool:
    return isinstance(node, (exp.Var, exp.Column)) and not getattr(node, "table", None) \
        and str(node.name).upper() == "ALL"


_CONSTANT_NODES = (exp.Literal, exp.Null, exp.Boolean, exp.Neg, exp.Paren, exp.Cast,
                   exp.TryCast, exp.DataType, exp.DataTypeParam)


def _is_constant(node: exp.Expression) -> bool:
    """Literals and casts of literals only — not `RANDOM()` or `NOW()`."""
    return all(isinstance(n, _CONSTANT_NODES) for n in node.walk())


def _has_aggregate(node: exp.Expression) -> bool:
    return any(a.find_ancestor(exp.Window) is None for a in node.find_all(exp.AggFunc))


def _expand_all_shorthand(ast: exp.Expression) -> tuple[str, ...]:
    """Spell GROUP BY ALL and ORDER BY ALL out as the projections they mean.

    sqlglot's projection pruning removes CTE projections the outer query
    does not read; it guards DISTINCT and ORDER BY column references, not
    these shorthands. Pruned, a CTE `GROUP BY ALL` over (country, refund)
    collapsed to grouping by nothing, and `ORDER BY ALL LIMIT 1` lost its
    sort keys — and at the top level ORDER BY ALL was recorded as the bare
    word ALL. Explicit expressions survive pruning and resolve like any
    other term. A shorthand over `*` or a window cannot be spelled out here,
    so it is a gap."""
    gaps: list[str] = []
    for select in list(ast.find_all(exp.Select)):
        projections = list(select.expressions)
        unknowable = any(isinstance(p, exp.Star) or (isinstance(p, exp.Column)
                                                      and isinstance(p.this, exp.Star))
                         or p.find(exp.Window) is not None for p in projections)
        bare = [p.this if isinstance(p, exp.Alias) else p for p in projections]
        # A constant key orders/groups nothing beside other keys, and spelled
        # out as an integer it would be re-read as an ORDINAL — so constants
        # are dropped. A grouping made ONLY of constants is not the same as
        # no grouping (an empty input yields no row instead of one): a gap.
        keys = [b for b in bare if not _has_aggregate(b) and not _is_constant(b)]
        group = select.args.get("group")
        if group is not None and group.args.get("all"):
            constants_only = not keys and any(not _has_aggregate(b) for b in bare)
            if unknowable or constants_only:
                gaps.append("GROUP BY ALL over *, a window or only constants — its keys "
                            "are not established")
            elif keys:
                group.set("all", None)
                group.set("expressions", [k.copy() for k in keys])
            else:
                select.set("group", None)   # nothing to group by: a total
        order = select.args.get("order")
        if order is not None and any(_is_all(o.this if isinstance(o, exp.Ordered) else o)
                                     for o in order.expressions):
            if unknowable:
                gaps.append("ORDER BY ALL over * or a window — its keys are not established")
                continue
            expanded: list[exp.Expression] = []
            for o in order.expressions:
                key = o.this if isinstance(o, exp.Ordered) else o
                if not _is_all(key):
                    expanded.append(o)
                    continue
                for b in bare:
                    if _is_constant(b):
                        continue
                    term = o.copy() if isinstance(o, exp.Ordered) else exp.Ordered()
                    term.set("this", b.copy())
                    expanded.append(term)
            order.set("expressions", expanded)
    return tuple(gaps)


def _output_names(select: exp.Select, schema, bodies: dict, dialect: str,
                  depth: int = 0) -> set[str] | None:
    """Lower-cased output column names of a select, or None if unknowable."""
    if depth > 8:
        return None
    names: set[str] = set()
    for p in select.expressions:
        star = isinstance(p, exp.Star) or (isinstance(p, exp.Column)
                                           and isinstance(p.this, exp.Star))
        if star:
            inner = _source_names(select, schema, bodies, dialect, depth + 1)
            if inner is None:
                return None
            names |= inner
        else:
            names.add(p.alias_or_name.lower())
    return names


def _source_names(select: exp.Select, schema, bodies: dict, dialect: str,
                  depth: int = 0) -> set[str] | None:
    """Lower-cased column names a select's FROM/JOIN sources provide."""
    names: set[str] = set()
    for src in scope_sources(select):
        if isinstance(src, exp.Table):
            key = src.name.lower()
            if not src.args.get("db") and key in bodies:
                body = unwrap_select(bodies[key])
                got = _output_names(body, schema, bodies, dialect, depth + 1) if body else None
            else:
                try:
                    got = {c.lower() for c in schema.column_names(src, dialect=dialect)}
                except Exception:  # noqa: BLE001 — unknown table: not knowable here
                    got = None
                got = got or None
        elif isinstance(src, exp.Subquery):
            body = unwrap_select(src.this)
            got = _output_names(body, schema, bodies, dialect, depth + 1) if body else None
        else:
            got = None
        if got is None:
            return None
        names |= got
    return names


def _ambiguous_output_refs(ast: exp.Expression, schema, dialect: str) -> tuple[str, ...]:
    """A HAVING or QUALIFY name that is BOTH a projection alias and a source
    column binds differently across engines: sqlglot's alias expansion
    rewrites it to the projection, while DuckDB binds the source column in
    several positions (a grouped key, an aggregate's argument, a window's
    PARTITION BY). The core then stated a predicate the SQL does not apply.
    An alias that is the identity of the same-named column is unambiguous.
    WHERE and GROUP BY bind the source column in both, so they are left
    alone."""
    if not hasattr(schema, "column_names"):
        return ()
    bodies = {cte.alias_or_name.lower(): cte.this for cte in ast.find_all(exp.CTE)}
    for select in ast.find_all(exp.Select):
        aliases = {p.alias.lower() for p in select.expressions
                   if isinstance(p, exp.Alias) and p.alias
                   and not (isinstance(p.this, exp.Column) and not p.this.table
                            and p.this.name.lower() == p.alias.lower())}
        if not aliases:
            continue
        refs = set()
        for clause in (select.args.get("having"), select.args.get("qualify")):
            if clause is None:
                continue
            refs |= {c.name.lower() for c in clause.find_all(exp.Column)
                     if not c.table and c.find_ancestor(exp.Select) is select}
        hit = refs & aliases
        if not hit:
            continue
        sources = _source_names(select, schema, bodies, dialect)
        if sources is None or hit & sources:
            names = ", ".join(sorted(hit & sources if sources else hit))
            return (f"HAVING/QUALIFY name {names} is both a projection alias and a source "
                    f"column — engines bind it differently",)
    return ()


def _pruning_hazards(ast: exp.Expression, schema, dialect: str) -> tuple[str, ...]:
    """Run the optimizer's projection pruning on a copy and see what it does
    to aggregate subqueries. An UNGROUPED aggregate (`SELECT 'all' AS seg,
    SUM(x) AS total FROM t`, one row) whose aggregate output nobody reads is
    pruned to `SELECT 'all' AS seg FROM t` — one row per input — and then
    certified equal to a plain table scan (COUNT(*) 1 vs 5). Detected, not
    repaired: the measure over it is not proof grade."""
    outermost = unwrap_select(ast)
    candidates = [s for s in ast.find_all(exp.Select)
                  if s is not outermost and s.args.get("group") is None
                  and any(_has_aggregate(p) for p in s.expressions)]
    if not candidates:
        return ()
    try:
        copy = qualify(ast.copy(), schema=schema, dialect=dialect)
    except Exception:  # noqa: BLE001 — optimize will degrade for the same reason
        return ()
    outer = unwrap_select(copy)
    marked = [s for s in copy.find_all(exp.Select)
              if s is not outer and s.args.get("group") is None
              and any(_has_aggregate(p) for p in s.expressions)]
    try:
        pushdown_projections(copy, schema=schema, dialect=dialect)
    except Exception:  # noqa: BLE001
        return ()
    for select in marked:
        if not any(_has_aggregate(p) for p in select.expressions):
            return ("an aggregate subquery loses its only aggregate to projection "
                    "pruning — its row count changes (one row becomes one per input)",)
    return ()
