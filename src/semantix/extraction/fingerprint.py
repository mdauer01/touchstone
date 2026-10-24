"""Fingerprint builder: one fingerprint per measure expression (§3).

A projection is a measure iff it contains a non-windowed aggregate function.
Windowed aggregates are analytic columns, not metric definitions.
"""

from __future__ import annotations

import hashlib
from collections import Counter

from sqlglot import exp

from semantix.extraction.canonicalize import CanonResult, normalize_scope
from semantix.extraction.completeness import detect_gaps
from semantix.extraction.registry import SchemaRegistry
from semantix.extraction.sqlutil import (
    flatten_conjuncts,
    render,
    split_group,
    table_map,
    unwrap_select,
)
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

    # The core is rendered RELATION-qualified, not alias-qualified. Positional
    # aliases (t0, t1) are assigned per query, so `SUM(t0."amount")` over one
    # table and the same math inside a join over two tables rendered as
    # different EXPRESSIONS — and blame then named "expression" as a
    # difference when only the numbering moved. Relations that appear once
    # are named; a self-joined relation keeps its positional aliases, since
    # collapsing them would equate `o1.amount - o2.amount` with its negation.
    qselect = _relation_qualified(select, amap)

    # Everything below is encoded into the EXISTING SemanticCore fields rather
    # than new ones. Post-aggregate and bound predicates are tagged so they
    # can never alias a plain WHERE conjunct of the same text.
    where = qselect.args.get("where")
    where_preds = flatten_conjuncts(where.this if where else None)
    join_edges_list, filter_list = _join_graph(qselect, amap, where_preds, dialect)

    having = qselect.args.get("having")
    filter_list += [f"HAVING {render(p, dialect)}" for p in flatten_conjuncts(having.this if having else None)]

    qualify = qselect.args.get("qualify")
    filter_list += [f"QUALIFY {render(p, dialect)}" for p in flatten_conjuncts(qualify.this if qualify else None)]

    select_for_render = qselect
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
            order = select_for_render.args.get("order")
            ordering = (" ORDER BY " + ", ".join(render(e, dialect)
                                                 for e in order.expressions)
                        if order is not None else "")
            q_on = select_for_render.args.get("distinct").args.get("on")
            filter_list.append(f"SELECT DISTINCT ON ({render(q_on, dialect)}){ordering}")
        else:
            filter_list.append("SELECT DISTINCT")

    # A bounded measure is a different measure, and its ordering is what
    # decides *which* rows survive — so both travel together.
    bound = _render_bound(select_for_render, dialect)
    if bound:
        filter_list.append(bound)

    filters = tuple(sorted(filter_list))

    grain = tuple(sorted(_grain_terms(select_for_render,
                                      select_for_render.args.get("group"), dialect)))

    join_nodes = tuple(sorted(set(amap.values())))
    join_edges = tuple(sorted(join_edges_list))

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
    for proj, qproj in zip(select.expressions, qselect.expressions):
        inner = proj.this if isinstance(proj, exp.Alias) else proj
        aggs = [a for a in inner.find_all(exp.AggFunc) if a.find_ancestor(exp.Window) is None]
        if not aggs:
            continue

        name = proj.alias_or_name or _synthesize_name(aggs[0])

        # Terminals resolve through the ORIGINAL aliases; the rendered text
        # comes from the relation-qualified copy.
        terminal_set: set[str] = set()
        for col in inner.find_all(exp.Column):
            src = amap.get(col.table) or single_source or col.table or "<unknown>"
            terminal_set.update(registry.terminals_for(src, col.name))
        qinner = qproj.this if isinstance(qproj, exp.Alias) else qproj

        # Identity that depends on the session: a join node or lineage
        # terminal whose name, after the operator-supplied context, is still
        # shorter than the estate's names. Equality over such a core proves
        # nothing about WHICH table was read, so it is a gap — missing
        # metadata must never become evidence of sameness.
        loose = sorted({rel for rel in join_nodes if registry.under_qualified(rel)}
                       | {t.rsplit(".", 1)[0] for t in terminal_set
                          if "." in t and registry.under_qualified(t.rsplit(".", 1)[0])})
        fp_gaps = gaps
        fp_level = level
        if loose:
            fp_gaps = tuple(sorted({*gaps, (
                f"relation identity depends on the session: {', '.join(loose)} "
                f"is not fully qualified for this estate "
                f"({registry.estate_depth()}-part names) and no default "
                f"database/schema was supplied")}))
            if fp_level in PROOF_LEVELS:
                fp_level = CanonLevel.INCOMPLETE.value

        core = SemanticCore(
            measure_expr=render(qinner, dialect),
            filters=filters,
            grain=grain,
            join_nodes=join_nodes,
            join_edges=join_edges,
            source_columns=tuple(sorted(terminal_set)),
        )
        fingerprints.append(
            Fingerprint(
                fingerprint_id=Fingerprint.make_id(core, name, relation, fp_level, fp_gaps),
                concept_candidate=name,
                core=core,
                relation=relation,
                dialect=dialect,
                canon_level=fp_level,
                canonical_sql=canonical_sql,
                provenance=provenance,
                gaps=fp_gaps,
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
    plain, constructs = split_group(group)
    terms = [render(g, dialect) for g in plain]
    for kind, node in constructs:
        terms.append(f"{kind.replace('_', ' ').upper()}({render(node, dialect)})")
    return terms


def _relation_qualified(select: exp.Select, amap: dict[str, str]) -> exp.Select:
    """A copy of the select rendered without positional aliases.

    Single-source selects render columns UNQUALIFIED: the qualifier adds
    nothing (the relation lives in join_nodes and lineage), and qualifying
    it would make two models with identical grain over different tables
    read as "different grain" in blame — a false clause-level accusation.
    Multi-source selects qualify columns by RELATION for every relation that
    appears exactly once, so join order cannot move a column's name; CTE
    digests, derived tables and self-joined relations keep their aliases,
    since there is no single name to give them and collapsing two aliases
    of one table would equate `o1.amount - o2.amount` with its negation."""
    counts = Counter(amap.values())
    single = len(amap) <= 1
    copy = select.copy()
    for col in copy.find_all(exp.Column):
        alias = col.table
        rel = amap.get(alias) if alias else None
        if rel is None:
            continue  # not a top-scope source: leave nested scopes alone
        if single:
            col.set("catalog", None)
            col.set("db", None)
            col.set("table", None)
            continue
        if counts[rel] != 1 or rel.startswith(("cte:", "<")):
            continue
        parts = rel.split(".")
        col.set("catalog", exp.to_identifier(parts[0]) if len(parts) == 3 else None)
        col.set("db", exp.to_identifier(parts[-2]) if len(parts) >= 2 else None)
        col.set("table", exp.to_identifier(parts[-1]))
    return copy


def _qualifiers_in(pred: exp.Expression) -> set[tuple[str, ...]]:
    """Distinct source identities a predicate references (relation-qualified
    or positional, whichever the column carries)."""
    out: set[tuple[str, ...]] = set()
    for col in pred.find_all(exp.Column):
        if not col.table:
            continue
        out.add(tuple(p.name for p in (col.args.get("catalog"), col.args.get("db"),
                                        col.args.get("table")) if p is not None))
    return out


def _is_plain_inner(join: exp.Join) -> bool:
    return (not join.args.get("side")
            and (join.args.get("kind") or "").upper() in ("", "INNER", "CROSS")
            and not join.args.get("method")
            and join.args.get("match_condition") is None)


def _join_graph(select: exp.Select, amap: dict[str, str],
                where_preds: list[exp.Expression], dialect: str
                ) -> tuple[list[str], list[str]]:
    """(join edges, WHERE-level filters) for one select.

    For a PURE inner-join graph, ON and WHERE are one conjunction and join
    order is irrelevant, so the edge set is the set of predicates that link
    two sources, and every single-source predicate — wherever the author or
    the optimizer put it — is a filter. Written `orders JOIN customers` or
    `customers JOIN orders`, with the segment filter in ON or in WHERE, the
    core is the same; before, the optimizer's placement decided the hash and
    identical models CONTRADICTED. A join graph with a component no
    predicate links is a cartesian product and is named as one.

    Any outer, semi, anti or ASOF join makes predicate placement and join
    direction load-bearing, and the per-join, directional rendering is kept.
    """
    joins = list(select.args.get("joins") or [])
    if joins and all(_is_plain_inner(j) for j in joins):
        preds: list[exp.Expression] = list(where_preds)
        edges: list[str] = []
        for join in joins:
            on = join.args.get("on")
            if on is not None:
                preds.extend(flatten_conjuncts(on))
            if join.args.get("using"):
                cols = ", ".join(render(u, dialect) for u in join.args["using"])
                edges.append(f"INNER JOIN USING ({cols})")
        filters: list[str] = []
        linked: list[set[tuple[str, ...]]] = []
        for pred in preds:
            quals = _qualifiers_in(pred)
            if len(quals) >= 2:
                edges.append(f"INNER JOIN ON {render(pred, dialect)}")
                linked.append(quals)
            else:
                filters.append(render(pred, dialect))
        # Connectivity: sources no predicate links are multiplied, not joined.
        sources = [s for s in table_map(select)]
        if len(sources) > 1:
            parent = {s: s for s in sources}

            def find(x):
                while parent[x] != x:
                    x = parent[x]
                return x

            names = {s: (amap.get(s) or s) for s in sources}
            for quals in linked:
                touched = [s for s in sources
                           if any(q[-1] == s or names[s].endswith(q[-1]) for q in quals)]
                for a, b in zip(touched, touched[1:]):
                    parent[find(a)] = find(b)
            roots = {find(s) for s in sources}
            if len(roots) > 1 and not any(j.args.get("using") for j in joins):
                edges.append("CARTESIAN(" + ", ".join(sorted(names[s] for s in sources)) + ")")
        return edges, filters
    return (_join_terms(select, amap, dialect),
            [render(p, dialect) for p in where_preds])


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
