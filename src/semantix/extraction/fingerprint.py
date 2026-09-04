"""Fingerprint builder: one fingerprint per measure expression (§3).

A projection is a measure iff it contains a non-windowed aggregate function.
Windowed aggregates are analytic columns, not metric definitions.
"""

from __future__ import annotations

import hashlib

from sqlglot import exp

from semantix.extraction.canonicalize import CanonResult, normalize_scope
from semantix.extraction.completeness import detect_gaps
from semantix.extraction.registry import SchemaRegistry
from semantix.extraction.sqlutil import flatten_conjuncts, render, table_map, unwrap_select
from semantix.models import CanonLevel, Fingerprint, PROOF_LEVELS, Provenance, SemanticCore


def extract_fingerprints(
    canon: CanonResult,
    relation: str,
    provenance: Provenance,
    dialect: str,
    registry: SchemaRegistry,
) -> list[Fingerprint]:
    select = unwrap_select(canon.ast)
    set_op = False
    if select is None and isinstance(canon.ast, (exp.Union, exp.Except, exp.Intersect)):
        # A set-operation model produced NOTHING before — no fingerprint, no
        # gap, no mention in coverage. Descend the whole tree rather than just
        # `.this`, which missed every branch of a 3+-way union, and mark the
        # result: no single branch is the whole measure, so it is never proof
        # grade.
        branches = _set_op_branches(canon.ast)
        select = branches[0] if branches else None
        set_op = True
    if select is None:
        return []

    amap = _resolve_cte_sources(table_map(select), select, dialect)
    # One physical table, one name — whatever form the query wrote it in.
    amap = {alias: registry.canonical_relation(rel) for alias, rel in amap.items()}
    single_source = next(iter(amap.values())) if len(amap) == 1 else None

    # Everything below is encoded into the EXISTING SemanticCore fields rather
    # than new ones, so a query using none of these constructs keeps the exact
    # hash it had before — only the previously mis-fingerprinted ones move.
    # Post-aggregate and bound predicates are tagged so they can never alias a
    # plain WHERE conjunct of the same text.
    where = select.args.get("where")
    filter_list = [render(p, dialect) for p in flatten_conjuncts(where.this if where else None)]

    having = select.args.get("having")
    filter_list += [f"HAVING {render(p, dialect)}" for p in flatten_conjuncts(having.this if having else None)]

    qualify = select.args.get("qualify")
    filter_list += [f"QUALIFY {render(p, dialect)}" for p in flatten_conjuncts(qualify.this if qualify else None)]

    distinct = select.args.get("distinct")
    if distinct is not None:
        # DISTINCT ON (k) keeps one row per k; plain DISTINCT de-duplicates
        # whole rows. Collapsing both to the bare tag made a keyed dedup
        # certify equal to an unkeyed one, and to a differently-keyed one.
        on = distinct.args.get("on") if isinstance(distinct, exp.Expression) else None
        if on is not None:
            # The ORDER BY decides WHICH row survives per key, so it is part
            # of the definition: asc/desc variants compute different rows and
            # previously hashed identically at proof grade.
            order = select.args.get("order")
            ordering = (" ORDER BY " + ", ".join(render(e, dialect)
                                                 for e in order.expressions)
                        if order is not None else "")
            filter_list.append(f"SELECT DISTINCT ON ({render(on, dialect)}){ordering}")
        else:
            filter_list.append("SELECT DISTINCT")

    # A bounded measure is a different measure, and its ordering is what
    # decides *which* rows survive — so both travel together.
    bound = _render_bound(select, dialect)
    if bound:
        filter_list.append(bound)

    filters = tuple(sorted(filter_list))

    grain = tuple(sorted(_grain_terms(select, select.args.get("group"), dialect)))

    join_nodes = tuple(sorted(set(amap.values())))
    join_edges = tuple(sorted(_join_terms(select, amap, dialect)))

    canonical_sql = render(select, dialect)

    # Completeness gate: if this select uses anything the core cannot
    # represent, the fingerprint must not be treated as proof-grade — the
    # unread construct would otherwise read as "identical" to its absence.
    # Gaps the OPTIMIZER destroyed travel on the CanonResult: by the time we
    # see the optimized AST, a merged CTE's ORDER BY is simply gone.
    gaps = tuple(sorted({*detect_gaps(select), *canon.gaps}))
    if set_op:
        gaps = tuple(sorted({*gaps, "set operation"}))
    level = canon.level.value
    if gaps and level in PROOF_LEVELS:
        level = CanonLevel.INCOMPLETE.value

    fingerprints: list[Fingerprint] = []
    for proj in select.expressions:
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        aggs = [a for a in inner.find_all(exp.AggFunc) if a.find_ancestor(exp.Window) is None]
        if not aggs:
            continue

        name = proj.alias_or_name or _synthesize_name(aggs[0])

        terminal_set: set[str] = set()
        for col in inner.find_all(exp.Column):
            src = amap.get(col.table) or single_source or col.table or "<unknown>"
            terminal_set.update(registry.terminals_for(src, col.name))

        core = SemanticCore(
            measure_expr=render(inner, dialect),
            filters=filters,
            grain=grain,
            join_nodes=join_nodes,
            join_edges=join_edges,
            source_columns=tuple(sorted(terminal_set)),
        )
        fingerprints.append(
            Fingerprint(
                fingerprint_id=Fingerprint.make_id(core, name, relation, level, gaps),
                concept_candidate=name,
                core=core,
                relation=relation,
                dialect=dialect,
                canon_level=level,
                canonical_sql=canonical_sql,
                provenance=provenance,
                gaps=gaps,
            )
        )
    return fingerprints


def _set_op_branches(node: exp.Expression) -> list[exp.Select]:
    """Every leaf SELECT of a set-operation tree, left to right.

    `UNION` nests left-associatively, so reading only `.this` reached one
    branch of a two-way union and none of a three-way one.
    """
    if isinstance(node, (exp.Union, exp.Except, exp.Intersect)):
        out: list[exp.Select] = []
        for side in ("this", "expression"):
            branch = node.args.get(side)
            if isinstance(branch, exp.Expression):
                out.extend(_set_op_branches(branch))
        return out
    select = unwrap_select(node)
    return [select] if select is not None else []


def _cte_bodies(select: exp.Select) -> dict[str, exp.Expression]:
    """Every CTE visible from this select, innermost scope winning."""
    bodies: dict[str, exp.Expression] = {}
    node: exp.Select | None = select
    while node is not None:
        for cte in node.ctes or []:
            bodies.setdefault(cte.alias_or_name, cte.this)
        node = node.parent_select
    return bodies


def _body_digests(bodies: dict[str, exp.Expression], dialect: str) -> dict[str, str]:
    """Content hash of every CTE body, with the CTEs each one references
    folded in.

    Without this the body is invisible: an unmergeable CTE (one containing an
    aggregate, DISTINCT or a window — the standard dbt staging shape) survives
    optimization as a plain table reference, so two entirely unrelated measures
    reading a CTE that happens to share a name hashed identically.

    Computed once per body with a memo rather than by re-descending from each
    reference. The previous shape re-hashed a shared body once per PATH to it,
    which is exponential in depth on a diamond-shaped CTE graph — precisely
    the shape a layered dbt project produces (several models reading the same
    staging CTE, four or five layers deep), where extraction stopped
    responding rather than failing visibly.
    """
    # Which CTEs sit on a cycle (a recursive CTE). Only their presence on the
    # walk stack can change a digest, so only they belong in the memo key.
    # Keying on "was ANY cycle involved" disabled memoization for the whole
    # graph, restoring the exponential blowup this function was rewritten to
    # remove: one `WITH RECURSIVE` date spine — routine — took a 32-CTE model
    # from 0.06s to 58s, and two more layers took it to four minutes.
    cyclic = _cyclic_names(bodies)
    memo: dict[tuple[str, frozenset[str]], str] = {}
    stack: list[str] = []

    def digest(name: str) -> tuple[str, bool]:
        """(hash, whether a cycle was involved)."""
        key = (name, frozenset(n for n in stack if n in cyclic))
        if key in memo:
            return memo[key], False
        body = bodies[name]
        stack.append(name)
        nested: list[str] = []
        saw_cycle = False
        sub_digests: dict[str, str] = {}
        # Only UNQUALIFIED references can point at a CTE — the same guard the
        # text-replacement below applies. Without it, a body reading the
        # PHYSICAL `raw.stg` had the unrelated CTE `stg`'s digest folded in
        # because the base names matched, so renaming that CTE changed this
        # body's digest and fabricated a CONTRADICTION on a pure refactor.
        # dbt convention makes `stg`-style base-name collisions routine.
        for ref in sorted({t.name for t in body.find_all(exp.Table)
                           if not t.args.get("db")} & set(bodies)):
            if ref in stack:  # recursive CTE — do not fold it into itself
                saw_cycle = True
                continue
            sub, sub_cycle = digest(ref)
            saw_cycle = saw_cycle or sub_cycle
            sub_digests[ref] = sub
            # The DIGEST only — `f"{ref}={sub}"` put the referenced CTE's NAME
            # back into the payload, so renaming an intermediate CTE still
            # changed every downstream hash even after the body text stopped
            # naming it. Sorted below, so order is stable without the key.
            nested.append(sub)
        stack.pop()

        # Normalize the body the same way the outer scope is normalized before
        # hashing it — the optimizer qualifies CTE bodies but leaves the
        # author's aliases, so raw text made `from raw.orders as cust` differ
        # from `from raw.orders`, fabricating a contradiction from a rename.
        # References to OTHER CTEs are then replaced by their digests, because
        # the rendered text otherwise carries their NAMES: renaming an
        # intermediate CTE changed every downstream fingerprint, so a pure
        # refactor of a layered dbt project reported a CONTRADICTION on every
        # metric it fed — contradicting this module's own stated invariant
        # that a body is keyed on its content, never its name.
        normalized = normalize_scope(body.copy(), dialect)
        for table in normalized.find_all(exp.Table):
            digest_of = sub_digests.get(table.name)
            if digest_of is not None and not table.args.get("db"):
                table.set("this", exp.to_identifier(f"cte_{digest_of}"))
        text = render(normalized, dialect)

        value = hashlib.sha256(
            (text + "|" + "|".join(sorted(nested))).encode()
        ).hexdigest()[:16]
        memo[key] = value
        return value, saw_cycle

    return {name: digest(name)[0] for name in bodies}


def _cyclic_names(bodies: dict[str, exp.Expression]) -> set[str]:
    """CTE names that participate in a cycle (i.e. recursive CTEs).

    Same no-db guard as the digest loop and the text replacement — this was
    the THIRD place base names are matched, and the one the guard missed: a
    CTE `stg` reading the physical `raw.stg` (dbt convention) read as a
    self-edge, marking every CTE cyclic, fragmenting the memo by stack, and
    restoring the exponential blowup (~600x at 30 CTEs).
    """
    edges = {name: {t.name for t in body.find_all(exp.Table)
                    if not t.args.get("db")} & set(bodies)
             for name, body in bodies.items()}
    cyclic: set[str] = set()
    for start in bodies:
        seen, queue = set(), list(edges[start])
        while queue:
            node = queue.pop()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(edges.get(node, ()))
        if start in seen:
            cyclic.add(start)
    return cyclic


def _resolve_cte_sources(amap: dict[str, str], select: exp.Select,
                         dialect: str) -> dict[str, str]:
    """Replace CTE references in the source map with a digest of their body.

    Keyed on the body alone, never the CTE's name, so the same logic staged
    under different names still compares equal.
    """
    bodies = _cte_bodies(select)
    if not bodies:
        return amap
    digests = _body_digests(bodies, dialect)
    return {alias: (f"cte:{digests[relation]}" if relation in digests else relation)
            for alias, relation in amap.items()}


def _render_bound(select: exp.Select, dialect: str) -> str | None:
    """LIMIT/OFFSET plus the ORDER BY that decides which rows they keep."""
    limit = select.args.get("limit")
    offset = select.args.get("offset")
    if limit is None and offset is None:
        return None
    parts = []
    order = select.args.get("order")
    if order is not None:
        parts.append("ORDER BY " + ", ".join(render(e, dialect) for e in order.expressions))
    if limit is not None:
        parts.append(render(limit, dialect))
    if offset is not None:
        parts.append(render(offset, dialect))
    return "BOUND " + " ".join(parts)


def _grain_terms(select: exp.Select, group: exp.Expression | None,
                 dialect: str) -> list[str]:
    """Grouping keys, including the multi-level constructs that do NOT live in
    group.expressions. ROLLUP(a, b) is a different grain from a flat (a, b) —
    it emits subtotal rows — and both differ from no grouping at all."""
    if group is None:
        return []
    # GROUP BY ALL groups by every non-aggregate projection. It lives in its
    # own arg, so the grain read as EMPTY and a full breakdown hashed as a
    # grand total — the same hole ROLLUP had, reopened by the idiom that is
    # now the default in DuckDB and Databricks SQL. Resolve it to the keys it
    # actually means, so it also compares equal to the explicit spelling.
    if group.args.get("all"):
        return [
            render(proj.this if isinstance(proj, exp.Alias) else proj, dialect)
            for proj in select.expressions
            if not any(a.find_ancestor(exp.Window) is None
                       for a in (proj.this if isinstance(proj, exp.Alias) else proj)
                       .find_all(exp.AggFunc))
        ]
    terms = [render(g, dialect) for g in group.expressions]
    for construct in ("rollup", "cube", "grouping_sets"):
        for node in group.args.get(construct) or []:
            terms.append(f"{construct.replace('_', ' ').upper()}({render(node, dialect)})")
    return terms


def _join_terms(select: exp.Select, amap: dict[str, str], dialect: str) -> list[str]:
    """Join edges carrying their SEMANTICS, not just the predicate. INNER and
    LEFT over the same ON are different row universes; a join with no ON at
    all is a cartesian product and must not vanish from the core."""
    terms = []
    for join in select.args.get("joins") or []:
        side = (join.args.get("side") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        # `kind or "INNER"` rendered every outer join as "LEFT INNER JOIN".
        # Harmless for hashing (it was consistent) but the string is quoted
        # verbatim in blame and receipts, so it stated a self-contradiction.
        label = " ".join(p for p in (side, kind) if p) or "INNER"
        # ASOF keeps one nearest match per probe row; a plain join keeps all of
        # them. Dropping `method` made a 3x row-multiplication difference
        # invisible, and MATCH_CONDITION is what an ASOF join actually joins on.
        method = (join.args.get("method") or "").upper()
        if method:
            label = f"{method} {label}"
        match_condition = join.args.get("match_condition")
        if match_condition is not None:
            label = f"{label} MATCH_CONDITION({render(match_condition, dialect)})"
        label += " JOIN"

        # WHICH relation is being joined is part of the edge. Without it, and
        # with the ON predicate's operands sorted by normalization, `A LEFT
        # JOIN B` and `B LEFT JOIN A` produced byte-identical edges — two
        # ordinary models with opposite row-preservation semantics certifying
        # as identical. This needs no exotic syntax and was the worst
        # false-certification path in the engine.
        target_node = join.this
        target = target_node.alias_or_name if isinstance(target_node, exp.Table) else None
        target_relation = amap.get(target, target) if target else render(target_node, dialect)

        on = join.args.get("on")
        using = join.args.get("using")
        if on is not None:
            terms.append(f"{label} {target_relation} ON {render(on, dialect)}")
        elif using:
            cols = ", ".join(render(u, dialect) for u in using)
            terms.append(f"{label} {target_relation} USING ({cols})")
        else:
            terms.append(f"{label} {target_relation}")
    return terms


def _synthesize_name(agg: exp.AggFunc) -> str:
    col = agg.find(exp.Column)
    base = type(agg).__name__.lower()
    return f"{base}_{col.name}" if col is not None else base
