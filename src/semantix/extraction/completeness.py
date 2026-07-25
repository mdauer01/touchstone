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


def detect_gaps(select: exp.Select) -> tuple[str, ...]:
    """Constructs used by this select that the SemanticCore cannot represent.

    Empty tuple means the core captures everything semantically material and
    the fingerprint may be treated as proof-grade.
    """
    gaps: set[str] = set()

    # MODELED (no longer gaps), for the record — each is now folded into the
    # SemanticCore by extraction.fingerprint and covered by a benchmark case:
    #   HAVING / QUALIFY .... tagged entries in filters
    #   LIMIT / OFFSET ...... a tagged BOUND entry in filters, with its ORDER BY
    #   SELECT DISTINCT ..... a tagged entry in filters
    #   ROLLUP/CUBE/SETS .... explicit terms in grain
    #   join side and kind .. rendered into join_edges, including ON-less joins
    #   CTE bodies .......... folded into join_nodes as a recursive body digest
    # Do not re-add a detector here without also removing the modeling.

    # 'totals' (ClickHouse WITH TOTALS) is a grouping construct we do not model.
    group = select.args.get("group")
    if group is not None and group.args.get("totals"):
        gaps.add("GROUP BY WITH TOTALS")

    # Sources whose body the core still never reads. CTE references are now
    # folded (fingerprint._resolve_cte_sources), but an inline derived table
    # that survived optimization, a LATERAL, or an UNNEST is not.
    for source in scope_sources(select):
        if isinstance(source, (exp.Subquery, exp.Lateral)):
            gaps.add("opaque derived table")
        elif isinstance(source, exp.Unnest):
            gaps.add("UNNEST source")

    # Set operations restructure the row universe entirely; only one branch of
    # a UNION is ever measured, so the other branch's rows are invisible.
    if isinstance(select.parent, (exp.Union, exp.Except, exp.Intersect)):
        gaps.add("set operation")

    return tuple(sorted(gaps))
