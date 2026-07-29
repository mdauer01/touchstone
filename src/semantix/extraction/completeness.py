"""Completeness gate: what the SemanticCore does NOT model.

The core is a whitelist — measure_expr, filters, grain, join graph, lineage.
Anything a query says that the core does not read is, by construction,
treated as absent, and two definitions differing *only* in an unread
construct hash identically. Under the doctrine "canonical equality is proof
of sameness," that is a false certification.

So the default is inverted here. This module enumerates the constructs the
core cannot represent; any measure whose select uses one is marked
INCOMPLETE and drops below PROOF_LEVELS. It can then still be reported,
diffed and blamed — it simply can never be certified equal to anything.

Every entry is a debt, not a design: as each construct becomes modeled, its
detector is deleted and the corresponding benchmark case moves from
`unproven` to `distinguished`. Deleting a detector without modeling the
construct re-opens a false-certification path, so each removal must be
accompanied by a red-then-green benchmark case.
"""

from __future__ import annotations

from sqlglot import exp

from semantix.extraction.sqlutil import scope_sources


# Arguments the SemanticCore genuinely reads. EVERYTHING ELSE IS A GAP.
#
# This list is the gate. It was previously a list of known-bad constructs,
# which meant the gate only caught what its author had already thought of —
# and a 2026-07-28 review found ELEVEN reproduced false certifications behind
# it, because sqlglot exposes 41 further arguments on these four node types
# that the core never reads and never gated: TABLESAMPLE, time travel
# (`VERSION AS OF`), PIVOT, GROUP BY ALL, named WINDOW clauses, ASOF joins,
# LATERAL VIEW, MATCH_RECOGNIZE, CONNECT BY, and more. Each made two queries
# over genuinely different data hash identically at proof grade.
#
# Enumerating bad constructs cannot converge: sqlglot adds arguments as
# warehouses add syntax, and each addition silently re-opens the hole. So the
# default is inverted twice over — a construct is unmodeled until it appears
# in one of these sets, and adding a name here is a claim that
# extraction.fingerprint actually folds it into the core.
_MODELLED_SELECT_ARGS = frozenset({
    "expressions",   # projections -> measure_expr
    "from_", "from",  # -> join_nodes
    "joins",         # -> join_edges
    "where",         # -> filters
    "group",         # -> grain (see _MODELLED_GROUP_ARGS)
    "having",        # -> filters, tagged
    "qualify",       # -> filters, tagged
    "limit", "offset", "order",  # -> a tagged BOUND entry in filters
    "with_", "with",  # CTE bodies folded into join_nodes as a body digest
    "distinct",      # -> a tagged entry in filters, including DISTINCT ON keys
    # NOT modelled, deliberately: "kind" (BigQuery SELECT AS STRUCT changes
    # the output shape and hashed identically to a plain SELECT) and
    # Table "joins" (never read by the core; listing it asserted a fold that
    # does not exist). Deny-by-default means absence here IS the gate.
    "operation_modifiers",  # MySQL execution hints; result-neutral, unread
    "hint",          # optimizer hints do not change results
})
_MODELLED_TABLE_ARGS = frozenset({"this", "db", "catalog", "alias"})
_MODELLED_JOIN_ARGS = frozenset({"this", "on", "side", "kind", "method",
                                 "match_condition", "using"})
_MODELLED_GROUP_ARGS = frozenset({"expressions", "rollup", "cube",
                                  "grouping_sets", "all"})

# Human names for the arguments most likely to appear, so a report says
# "TABLESAMPLE" rather than "sample".
_GAP_NAMES = {
    "sample": "TABLESAMPLE/SAMPLE",
    "version": "table version (time travel)",
    "system_time": "FOR SYSTEM_TIME AS OF (time travel)",
    "changes": "CHANGES (change tracking)",
    "pivots": "PIVOT/UNPIVOT",
    "windows": "named WINDOW clause",
    "laterals": "LATERAL VIEW",
    "match": "MATCH_RECOGNIZE",
    "connect": "CONNECT BY",
    "prewhere": "PREWHERE",
    "totals": "GROUP BY WITH TOTALS",
    "partition": "table PARTITION selector",
    "rows_from": "ROWS FROM",
    "ordinality": "WITH ORDINALITY",
    "pattern": "table PATTERN",
    "when": "table WHEN clause",
    "into": "SELECT INTO",
    "cluster": "CLUSTER BY",
    "distribute": "DISTRIBUTE BY",
    "sort": "SORT BY",
    "locks": "row locking clause",
    "exclude": "SELECT * EXCLUDE",
    "format": "table FORMAT",
    "expressions": "join expressions",
    "global_": "GLOBAL join",
    "directed": "directed join",
}


def _unmodelled(node: exp.Expression, modelled: frozenset[str],
                label: str) -> set[str]:
    """Every argument this node carries that the core does not read."""
    return {
        f"{label}: {_GAP_NAMES.get(key, key)}"
        for key, value in node.args.items()
        if value and key not in modelled
    }


def detect_gaps(select: exp.Select) -> tuple[str, ...]:
    """Constructs used by this select that the SemanticCore cannot represent.

    Empty tuple means the core captures everything semantically material and
    the fingerprint may be treated as proof-grade.
    """
    gaps: set[str] = set()

    gaps |= _unmodelled(select, _MODELLED_SELECT_ARGS, "SELECT")

    group = select.args.get("group")
    if isinstance(group, exp.Expression):
        gaps |= _unmodelled(group, _MODELLED_GROUP_ARGS, "GROUP BY")

    for join in select.args.get("joins") or []:
        if isinstance(join, exp.Expression):
            gaps |= _unmodelled(join, _MODELLED_JOIN_ARGS, "JOIN")

    # Sources whose body the core still never reads. CTE references are folded
    # (fingerprint._resolve_cte_sources); everything else that is not a plain
    # table is opaque — including VALUES lists and table functions, whose
    # contents ARE the data being read and which previously rendered as an
    # empty relation name, so `generate_series(1, 10)` and
    # `read_parquet('s3://test/*')` were indistinguishable from each other.
    for source in scope_sources(select):
        if isinstance(source, exp.Table):
            gaps |= _unmodelled(source, _MODELLED_TABLE_ARGS, "FROM")
            # We model the alias NAME, never its column-rename list.
            # `raw.refunds t(a, b, c)` renames columns POSITIONALLY, so
            # `sum(a)` means refund_id under one ordering and order_id under
            # another — and lineage resolved `t0.a` to `raw.refunds.a`, a
            # column that does not exist. Receipts cite that field as physical
            # lineage, so this was fabrication rather than omission.
            alias = source.args.get("alias")
            if isinstance(alias, exp.TableAlias) and alias.args.get("columns"):
                gaps.add("FROM: table alias column list")
            if not isinstance(source.this, (exp.Identifier, exp.Literal, str)):
                # A table FUNCTION: `read_parquet(...)`, `generate_series(...)`,
                # a UDTF. Its arguments select the data and the core does not
                # read them.
                gaps.add(f"FROM: table function {type(source.this).__name__}")
        elif isinstance(source, (exp.Subquery, exp.Lateral)):
            gaps.add("opaque derived table")
        elif isinstance(source, exp.Unnest):
            gaps.add("UNNEST source")
        elif isinstance(source, exp.Values):
            gaps.add("inline VALUES source")
        elif source is not None:
            gaps.add(f"unmodelled source {type(source).__name__}")

    # Set operations restructure the row universe entirely; only one branch of
    # a UNION is ever measured, so the other branch's rows are invisible.
    if isinstance(select.parent, (exp.Union, exp.Except, exp.Intersect)):
        gaps.add("set operation")

    return tuple(sorted(gaps))
