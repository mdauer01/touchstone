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

    if select.args.get("distinct") is not None:
        filter_list.append("SELECT DISTINCT")

    # A bounded measure is a different measure, and its ordering is what
    # decides *which* rows survive — so both travel together.
    bound = _render_bound(select, dialect)
    if bound:
        filter_list.append(bound)

    filters = tuple(sorted(filter_list))

    grain = tuple(sorted(_grain_terms(select.args.get("group"), dialect)))

    join_nodes = tuple(sorted(set(amap.values())))
    join_edges = tuple(sorted(_join_terms(select, amap, dialect)))

    canonical_sql = render(select, dialect)

    # Completeness gate: if this select uses anything the core cannot
    # represent, the fingerprint must not be treated as proof-grade — the
    # unread construct would otherwise read as "identical" to its absence.
    gaps = detect_gaps(select)
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
                fingerprint_id=Fingerprint.make_id(core, name),
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
    memo: dict[str, str] = {}
    stack: list[str] = []

    def digest(name: str) -> tuple[str, bool]:
        """(hash, whether a cycle was involved) — a body reached through a
        recursive reference is not memoized, because its value depends on
        where the walk started."""
        if name in memo:
            return memo[name], False
        body = bodies[name]
        # Normalize the body the same way the outer scope is normalized before
        # hashing it — the optimizer qualifies CTE bodies but leaves the
        # author's aliases, so raw text made `from raw.orders as cust` differ
        # from `from raw.orders`, fabricating a contradiction from a rename.
        text = render(normalize_scope(body.copy(), dialect), dialect)
        stack.append(name)
        nested: list[str] = []
        cyclic = False
        for ref in sorted({t.name for t in body.find_all(exp.Table)} & set(bodies)):
            if ref in stack:  # recursive CTE — do not fold it into itself
                cyclic = True
                continue
            sub, sub_cyclic = digest(ref)
            cyclic = cyclic or sub_cyclic
            nested.append(f"{ref}={sub}")
        stack.pop()
        value = hashlib.sha256(
            (text + "|" + "|".join(nested)).encode()
        ).hexdigest()[:16]
        if not cyclic:
            memo[name] = value
        return value, cyclic

    return {name: digest(name)[0] for name in bodies}


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


def _grain_terms(group: exp.Expression | None, dialect: str) -> list[str]:
    """Grouping keys, including the multi-level constructs that do NOT live in
    group.expressions. ROLLUP(a, b) is a different grain from a flat (a, b) —
    it emits subtotal rows — and both differ from no grouping at all."""
    if group is None:
        return []
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
        label = " ".join(p for p in (side, kind or "INNER") if p) + " JOIN"
        on = join.args.get("on")
        using = join.args.get("using")
        if on is not None:
            terms.append(f"{label} ON {render(on, dialect)}")
        elif using:
            cols = ", ".join(render(u, dialect) for u in using)
            terms.append(f"{label} USING ({cols})")
        else:
            target = join.this.alias_or_name if isinstance(join.this, exp.Table) else "?"
            terms.append(f"{label} {amap.get(target, target)}")
    return terms


def _synthesize_name(agg: exp.AggFunc) -> str:
    col = agg.find(exp.Column)
    base = type(agg).__name__.lower()
    return f"{base}_{col.name}" if col is not None else base
