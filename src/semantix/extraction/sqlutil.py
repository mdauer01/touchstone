"""Shared AST helpers used by canonicalization, fingerprinting and the registry."""

from __future__ import annotations

from sqlglot import exp


_GROUPING_KINDS: tuple[tuple[type, str], ...] = (
    (exp.Rollup, "rollup"), (exp.Cube, "cube"), (exp.GroupingSets, "grouping_sets"))


def split_group(group: exp.Group) -> tuple[list[exp.Expression], list[tuple[str, exp.Expression]]]:
    """(plain grouping items, [(kind, construct node)]) for one GROUP BY.

    The AST shape changed between sqlglot releases: up to 30.12 ROLLUP, CUBE
    and GROUPING SETS lived in dedicated args (`group.args["rollup"]`); from
    the 30.2x line they are nodes inside `group.expressions`. Every site that
    read the dedicated args directly went blind under the newer parser, and
    ordinals inside ROLLUP(1, 2) stopped resolving — two different grouping
    definitions then hashed identically at proof grade. This is the single
    reader every site must use; it accepts both shapes.
    """
    plain: list[exp.Expression] = []
    constructs: list[tuple[str, exp.Expression]] = []
    for item in group.expressions:
        kind = next((k for cls, k in _GROUPING_KINDS if isinstance(item, cls)), None)
        if kind is None:
            plain.append(item)
        else:
            constructs.append((kind, item))
    for _, kind in _GROUPING_KINDS:
        for node in group.args.get(kind) or []:
            if isinstance(node, exp.Expression):
                constructs.append((kind, node))
    return plain, constructs


def render(node: exp.Expression, dialect: str) -> str:
    """The one true canonical text form. Every comparison in the system goes
    through this function so that equality is equality of this rendering."""
    return node.sql(dialect=dialect, comments=False, normalize_functions="upper")


def unwrap_select(ast: exp.Expression | None) -> exp.Select | None:
    node = ast
    while isinstance(node, exp.Subquery):
        node = node.this
    return node if isinstance(node, exp.Select) else None


def table_fqname(table: exp.Table) -> str:
    """The relation a Table node names, WITHOUT the `_` placeholder that
    canonicalize._pin_short_references adds for qualification. The pin is a
    resolution device; letting it leak into join nodes and lineage made two
    byte-identical models — one canonicalized before a full-depth model was
    registered, one after — read as `raw.orders` vs `_.raw.orders` and
    CONTRADICT each other over a relation that does not exist."""
    parts = [table.args.get("catalog"), table.args.get("db"), table.this]
    return ".".join(p.name for p in parts if p is not None and p.name != "_")


def scope_sources(select: exp.Select) -> list[exp.Expression]:
    """FROM + JOIN sources of this select only (not nested scopes)."""
    sources: list[exp.Expression] = []
    # sqlglot renamed the arg key "from" -> "from_" around v30; accept both.
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is not None:
        sources.append(from_.this)
    for join in select.args.get("joins") or []:
        sources.append(join.this)
    return sources


def table_map(select: exp.Select) -> dict[str, str]:
    """Map of alias-or-name -> fully qualified relation for this select's scope.

    Derived tables map to a pseudo relation so lineage degrades visibly
    instead of silently.
    """
    mapping: dict[str, str] = {}
    for source in scope_sources(select):
        if isinstance(source, exp.Table):
            mapping[source.alias_or_name] = table_fqname(source)
        elif isinstance(source, (exp.Subquery, exp.CTE)):
            name = source.alias_or_name or "_subquery"
            mapping[name] = f"<derived:{name}>"
    return mapping


def requalify_columns(node: exp.Expression, table: str | None,
                      only_qualified: bool = True) -> None:
    """Rewrite column qualifiers in place to `table` (or strip them when
    None). Adapters use it to bind a declaration's own naming (`source.`, a
    dataset alias) to the relation the synthesized claim reads FROM; the
    emitter uses it in reverse. With only_qualified, bare columns are left
    alone."""
    for col in node.find_all(exp.Column):
        if only_qualified and not col.table:
            continue
        col.set("catalog", None)
        col.set("db", None)
        col.set("table", exp.to_identifier(table) if table else None)


def flatten_conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.And):
        return list(node.flatten())
    return [node]
