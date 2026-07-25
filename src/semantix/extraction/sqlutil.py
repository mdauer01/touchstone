"""Shared AST helpers used by canonicalization, fingerprinting and the registry."""

from __future__ import annotations

from sqlglot import exp


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
    parts = [table.args.get("catalog"), table.args.get("db"), table.this]
    return ".".join(p.name for p in parts if p is not None)


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


def flatten_conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.And):
        return list(node.flatten())
    return [node]
