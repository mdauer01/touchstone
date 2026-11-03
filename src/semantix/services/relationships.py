"""Relationship layer: mine join edges from executed SQL, classify them
against declarations, verify cardinality against a parameterized warehouse.

Doctrine, one level down from metrics: **used is not valid.** Query history
is the estate's revealed preference about how tables connect — the richest
source of join knowledge anywhere, and also a faithful record of every
popular wrong join. Mining therefore produces *candidates* with usage
weights; declarations (catalog keys, semantic-layer joins) produce *claims*;
only the warehouse checks — key uniqueness, orphan counts, cardinality
class — upgrade an edge to verified. Usage decides what gets verified
first, never what is true.

Every edge carries TWO orthogonal axes, because conflating them is the exact
mistake this layer exists to avoid:

  status (validity — what the warehouse showed ON THE DATA IT CHECKED)
    checks_passed_on_observed_data
                 checked and passed: a unique side, zero blocking orphans —
                 on the rows present when checked, over the stated scope
    failed       checked and failed, with measured evidence (orphans, N:M)
    unverified   no check has run yet, or it demonstrated nothing

  Passed checks support uniqueness and zero orphans on the checked data.
  They do not establish that the join is the right BUSINESS relationship,
  that it preserves a metric's meaning, or that it will stay valid
  (round seventeen renamed `certified`, which claimed all three). The check
  SQL is built from the stated edge (`qident`), never through the SQL
  interpretation path whose readings are suspended.

  usage (declaration vs. practice — what the estate does)
    declared+used  in the ERD and exercised by real queries
    shadow         exercised by real queries, declared nowhere
    orphan         declared, never seen in executed SQL

A passed shadow edge is a documentation gap; a failed heavily-used edge is
an active bug; a passed orphan means the ERD's keys held and nobody joins
it. One number can't say that, so we don't try.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from semantix.extraction.canonicalize import canonicalize_sql
from semantix.extraction.registry import SchemaRegistry
from semantix.extraction.sqlutil import annotate_truncations, flatten_conjuncts, table_map
from semantix.store import Store, utcnow
from semantix.warehouse import qident

CHECKS_PASSED = "checks_passed_on_observed_data"
LIMITATION = ("checks passed on the observed data: they support key uniqueness and zero "
              "orphans on the rows checked; they do not establish that the join is the right "
              "business relationship, that it preserves a metric's meaning, or that it will "
              "remain valid")


@dataclass(frozen=True)
class Edge:
    """Direction-free join edge. Endpoints are stored sorted so that
    `a.x = b.y` and `b.y = a.x` — and any authoring style in between — are
    one identity. Direction (child -> parent) is not an input; it is an
    OUTPUT of verification, derived from which side proves unique."""

    left_relation: str
    left_column: str
    right_relation: str
    right_column: str

    @staticmethod
    def make(rel_a: str, col_a: str, rel_b: str, col_b: str) -> "Edge":
        a, b = sorted([(rel_a, col_a), (rel_b, col_b)])
        return Edge(a[0], a[1], b[0], b[1])

    @staticmethod
    def from_qualified(src: str, dst: str) -> "Edge | None":
        """From 'schema.table.column' strings (declared form)."""
        try:
            rel_a, _, col_a = src.rpartition(".")
            rel_b, _, col_b = dst.rpartition(".")
        except AttributeError:
            return None
        if not (rel_a and col_a and rel_b and col_b):
            return None
        return Edge.make(rel_a, col_a, rel_b, col_b)

    @property
    def edge_id(self) -> str:
        raw = f"{self.left_relation}.{self.left_column}|{self.right_relation}.{self.right_column}"
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def label(self) -> str:
        return (f"{self.left_relation}.{self.left_column} = "
                f"{self.right_relation}.{self.right_column}")


# ------------------------------------------------------------------- mining


def mine_sql_text(
    text: str, registry: SchemaRegistry, dialect: str
) -> tuple[dict[Edge, dict], dict]:
    """Extract join edges from a blob of executed SQL (multi-statement).

    Catches explicit JOIN ... ON conditions *and* implicit `FROM a, b WHERE
    a.x = b.y` joins — decades of BI SQL use both. Best-effort by design:
    statements that fail to parse or columns that cannot be resolved to two
    distinct physical relations are skipped and counted, never fatal.

    Returns (per-edge stats keyed by Edge, mining meta)."""
    stats: dict[Edge, dict] = {}
    parsed, skipped = _parse_statements(text, dialect)

    for statement in parsed:
        canon = canonicalize_sql(statement.sql(dialect=dialect), registry.sqlglot_schema(dialect), dialect)
        ast = canon.ast if canon.ast is not None else statement
        statement_edges: set[Edge] = set()
        for select in ast.find_all(exp.Select):
            statement_edges |= _edges_in_select(select)
        for edge in statement_edges:
            stats.setdefault(edge, {"queries": 0})["queries"] += 1

    meta = {"statements": len(parsed), "skipped_statements": skipped,
            "edges": len(stats)}
    return stats, meta


def _parse_statements(text: str, dialect: str) -> tuple[list[exp.Expression], int]:
    """(parsed statements, unparseable statement count) for a history blob.
    See parse_statement_list, which keeps each unparseable statement's
    position."""
    listed = parse_statement_list(text, dialect)
    return [s for s in listed if s is not None], sum(1 for s in listed if s is None)


def parse_statement_list(text: str, dialect: str) -> list[exp.Expression | None]:
    """Every statement in a history blob, in order; None for one that does
    not parse (statement_chunks also gives each one's text)."""
    return [tree for _, tree in statement_chunks(text, dialect)]


def statement_chunks(text: str, dialect: str) -> list[tuple[str, exp.Expression | None]]:
    """(text, tree) for every statement in a history blob, in order; the tree
    None for one that does not parse, so what came before what is never lost.

    Statements are split on semicolons OUTSIDE strings and comments, using
    the dialect's tokenizer, and each is parsed on its own. The previous
    fallback split the raw text on `;`, so a semicolon inside a literal or a
    comment split one statement into fragments — a valid statement was lost
    and double-counted as unparseable, a comment fragment parsed as a
    "non-query statement", and a SELECT embedded in an INSERT's string
    literal was counted as an EXECUTION. Counts here are per statement the
    operator actually ran.

    A comment on the SAME line after a statement's semicolon belongs to that
    statement (`SELECT …; -- semantix: metric=gross_revenue`): attached to the
    next one, it named the wrong query.
    """
    from sqlglot.tokens import TokenType

    try:
        tokens = sqlglot.tokenize(text, read=dialect)
    except Exception:  # noqa: BLE001 — a blob the tokenizer rejects outright
        whole, bad = _parse_whole(text, dialect)
        return [(t.meta.get("source_text") or text.strip(), t) for t in whole] + \
            [(text.strip(), None)] * bad
    chunks: list[str] = []
    start = 0
    for tok in tokens:
        if tok.token_type == TokenType.SEMICOLON:
            if tok.start < start:
                continue   # consumed with a trailing comment
            chunk = text[start:tok.start]
            after = tok.end + 1
            eol = text.find("\n", after)
            line = text[after: eol if eol >= 0 else len(text)]
            tail = line.strip()
            if tail and (tail.startswith("--") or (tail.startswith("/*") and tail.endswith("*/")
                                                  and "*/" not in tail[2:-2])):
                chunk = f"{chunk} {tail}"
                after = eol if eol >= 0 else len(text)
            chunks.append(chunk)
            start = after
    chunks.append(text[start:])

    statements: list[tuple[str, exp.Expression | None]] = []
    for chunk in chunks:
        if not _has_code(chunk, dialect):
            continue  # whitespace or comments only — nothing was executed
        try:
            parsed = sqlglot.parse_one(chunk, read=dialect)
        except Exception:  # noqa: BLE001
            statements.append((chunk.strip(), None))
            continue
        if parsed is None or isinstance(parsed, exp.Semicolon):
            continue
        annotate_truncations(parsed, chunk, dialect)
        # The text as it ran, for evidence records (assess.record): the tree
        # is qualified in place later, and a re-render is not always faithful.
        parsed.meta["source_text"] = chunk.strip()
        statements.append((chunk.strip(), parsed))
    return statements


def _has_code(chunk: str, dialect: str) -> bool:
    """True when a chunk holds anything besides whitespace and comments."""
    if not chunk.strip():
        return False
    try:
        from sqlglot.tokens import TokenType
        return any(t.token_type != TokenType.SEMICOLON
                   for t in sqlglot.tokenize(chunk, read=dialect))
    except Exception:  # noqa: BLE001 — let the parser report it
        return True


def _parse_whole(text: str, dialect: str) -> tuple[list[exp.Expression], int]:
    try:
        trees = [s for s in sqlglot.parse(text, read=dialect) if s is not None]
    except Exception:  # noqa: BLE001
        return [], 1
    for tree in trees:
        annotate_truncations(tree, text, dialect)
    return trees, 0


def _cte_names(select: exp.Select) -> set[str]:
    """CTE aliases visible from this select, at any enclosing scope."""
    names = {cte.alias_or_name for cte in select.find_all(exp.CTE)}
    scope = select
    while scope is not None:
        names |= {cte.alias_or_name for cte in (scope.ctes or [])}
        scope = scope.parent_select
    return names


def _edges_in_select(select: exp.Select) -> set[Edge]:
    amap = table_map(select)
    if len(amap) < 2:
        return set()

    # A CTE reference is not a physical relation. Mining one as though it were
    # produces an edge naming a table that may not exist — or worse, a real
    # table that merely shares the CTE's name, which verification would then
    # happily certify while describing a join that never happens.
    ctes = _cte_names(select)
    amap = {alias: rel for alias, rel in amap.items() if rel not in ctes}
    if len(amap) < 2:
        return set()

    conditions: list[exp.Expression] = []
    for join in select.args.get("joins") or []:
        on = join.args.get("on")
        if on is not None:
            conditions.extend(flatten_conjuncts(on))
    where = select.args.get("where")
    if where is not None:
        conditions.extend(flatten_conjuncts(where.this))

    edges: set[Edge] = set()
    for cond in conditions:
        # Only same-scope CONJUNCTIVE equalities are join evidence. Descending
        # into the condition would harvest equalities from OR branches, NOT,
        # CASE arms and correlated subqueries — none of which assert that the
        # two tables are joined on those columns.
        for eq in [cond] if isinstance(cond, exp.EQ) else []:
            left, right = eq.this, eq.args.get("expression")
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            rel_l = amap.get(left.table)
            rel_r = amap.get(right.table)
            if not rel_l or not rel_r or rel_l == rel_r:
                continue
            if rel_l.startswith("<derived") or rel_r.startswith("<derived"):
                continue
            edges.add(Edge.make(rel_l, left.name, rel_r, right.name))
    return edges


def record_mined(store: Store, mined: dict[Edge, dict], source: str) -> None:
    observed_at = utcnow()
    for edge, stats in mined.items():
        store.record_relationship_edge(
            edge.edge_id, edge.left_relation, edge.left_column,
            edge.right_relation, edge.right_column,
        )
        store.observe_relationship(edge.edge_id, "mined", source, stats, observed_at)


def record_declared(store: Store, declared: list[tuple[str, str]], source: str) -> None:
    observed_at = utcnow()
    for src, dst in declared:
        edge = Edge.from_qualified(src, dst)
        if edge is None:
            continue
        store.record_relationship_edge(
            edge.edge_id, edge.left_relation, edge.left_column,
            edge.right_relation, edge.right_column,
        )
        store.observe_relationship(edge.edge_id, "declared", source,
                                   {"from": src, "to": dst}, observed_at)


# -------------------------------------------------------------- read model


def edge_status(edge_row: dict) -> str:
    """Validity axis — only a warehouse check can move this off unverified.

    A check that could not RUN is recorded so it supersedes any earlier
    verdict, but it reports `unverified` rather than `failed`: we learned
    nothing about the relationship, only that we can no longer confirm it.

    The same holds for a check that ran and demonstrated nothing — an empty
    table, an all-NULL column, a single distinct value. `failed` tells an
    agent the join is KNOWN broken; saying that about an unloaded table
    blames working code for the absence of evidence.
    """
    check = edge_row.get("check")
    if check is None:
        return "unverified"
    evidence = check.get("evidence") or {}
    if evidence.get("unrunnable") or evidence.get("inconclusive"):
        return "unverified"
    return CHECKS_PASSED if check["passed"] else "failed"


def edge_usage(edge_row: dict) -> str:
    """Declaration-vs-practice axis. Independent of validity."""
    declared = bool(edge_row["declared_sources"])
    used = bool(edge_row["mined_queries"])
    if declared and used:
        return "declared+used"
    if declared:
        return "orphan"
    return "shadow"


def graph_with_status(store: Store, table: str | None = None) -> list[dict]:
    rows = store.relationship_graph()
    if table:
        rows = [r for r in rows
                if table in (r["left_relation"], r["right_relation"])]
    for r in rows:
        r["status"] = edge_status(r)
        r["usage"] = edge_usage(r)
        r["label"] = (f"{r['left_relation']}.{r['left_column']} = "
                      f"{r['right_relation']}.{r['right_column']}")
    # Competing keys: >1 distinct column-pair for the same relation pair is a
    # note for the verifier, not a verdict — composite keys are legitimate.
    pairs: dict[tuple[str, str], int] = {}
    for r in rows:
        pair = (r["left_relation"], r["right_relation"])
        pairs[pair] = pairs.get(pair, 0) + 1
    for r in rows:
        r["competing_keys"] = pairs[(r["left_relation"], r["right_relation"])] > 1
    return rows


# ---------------------------------------------------------- verification


def verify_edges(
    store: Store,
    warehouse,
    top: int | None = None,
    declared_pks: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Run cardinality checks for every known edge (usage-ranked; usage
    prioritizes, never certifies). Records one check row per edge and
    returns the updated read model rows for the checked edges."""
    rows = graph_with_status(store)
    if top:
        rows = rows[:top]

    unbuilt = getattr(warehouse, "failed_relations", {})
    results = []
    for row in rows:
        # An endpoint that never materialized cannot be checked. Querying it
        # anyway either errors or, worse, answers from some pre-existing
        # object of the same name.
        # Endpoint names come from catalog YAML or mined SQL (usually two-part,
        # lower case) while `unbuilt` keys come from artifact.relation, which on
        # a dbt/Snowflake estate is three-part and upper case. An exact string
        # test never matched, so the guard was inert exactly where it is needed
        # and the edge was certified against whatever stale object the seed had
        # left behind under that name.
        broken = [r for r in (row["left_relation"], row["right_relation"])
                  if any(same_relation(r, u) for u in unbuilt)]
        if broken:
            # Carry WHY the build failed when the warehouse recorded it — the
            # append-only check history otherwise never says what broke.
            causes = []
            if isinstance(unbuilt, dict):
                for r in broken:
                    for u, why in unbuilt.items():
                        if same_relation(r, u) and why:
                            causes.append(f"{u}: {why}")
            evidence = {"unrunnable": True,
                        "error": f"relation(s) failed to build: {', '.join(broken)}"
                                 + (f" ({'; '.join(causes)})" if causes else "")}
            # RECORD it. The sibling unrunnable branch below already does, for
            # the same reason: a check that cannot run must still supersede an
            # earlier CERTIFIED, or every later reader — the CLI, the MCP
            # server, relationships_for_agent — keeps reading `certified` from
            # the store and keeps telling agents to join through an edge whose
            # relation did not build. Only the return value said `unverified`.
            store.record_relationship_check(
                row["edge_id"], getattr(warehouse, "label", "?"), False,
                {**evidence, "supersedes_previous_verdict": True},
            )
            results.append({**row, "status": "unverified", "check": None,
                            "unrunnable": evidence})
            continue
        evidence = _check_edge(
            warehouse,
            row["left_relation"], row["left_column"],
            row["right_relation"], row["right_column"],
            declared_pks=declared_pks,
        )
        if evidence.get("unrunnable"):
            # The check did not run, so the edge is not FAILED — but neither
            # can an earlier CERTIFIED stand: the warehouse can no longer
            # answer for it, and continuing to tell agents to join through it
            # would be certifying from a state we can no longer confirm.
            store.record_relationship_check(
                row["edge_id"], getattr(warehouse, "label", "?"), False,
                {**evidence, "supersedes_previous_verdict": True},
            )
            results.append({**row, "status": "unverified", "check": None,
                            "unrunnable": evidence})
            continue
        passed = bool(evidence.get("passed"))
        # Recorded as not-passed either way, so a stale CERTIFIED can never
        # survive a check that failed to demonstrate the key. The STATUS,
        # though, comes from edge_status: an inconclusive check reports
        # `unverified`, not `failed`.
        store.record_relationship_check(
            row["edge_id"], getattr(warehouse, "label", "?"), passed, evidence
        )
        checked = {**row, "check": {"passed": passed, "evidence": evidence,
                                    "warehouse": getattr(warehouse, "label", "?"),
                                    "checked_at": utcnow()}}
        checked["status"] = edge_status(checked)
        results.append(checked)
    return results


def relationships_for_agent(store: Store, table: str | None = None) -> dict[str, Any]:
    """Agent-facing view: edges whose checks passed on observed data first,
    each with what was checked, over what scope and when; everything else
    labeled honestly. An agent must never receive a mined-but-unverified edge
    that looks like a fact — that would be the popularity-ranked context
    layer we exist to audit — nor a passed check that reads as a guarantee."""
    rows = graph_with_status(store, table=table)

    def summarize(r: dict) -> dict:
        ev = (r.get("check") or {}).get("evidence") or {}
        # An UNVERIFIED edge has no measured cardinality or parent key. Over
        # two empty tables the check computes `0 == 0` and reports "1:1" with a
        # named parent — two fabricated measurements on an edge this function's
        # own docstring says must never look like a fact. The unrunnable path
        # already returns None for both; inconclusive must match it.
        measured = r["status"] != "unverified"
        passed = r["status"] == CHECKS_PASSED
        return {
            "join": r["label"],
            "left": f"{r['left_relation']}.{r['left_column']}",
            "right": f"{r['right_relation']}.{r['right_column']}",
            "status": r["status"],
            "usage": r["usage"],
            "queries_observed": r["mined_queries"],
            "cardinality": ev.get("cardinality") if measured else None,
            "parent_key": ev.get("parent") if measured else None,
            "reason": ev.get("reason") or ev.get("error"),
            "competing_keys": r["competing_keys"],
            # What a measured status rests on: as of when, over what scope,
            # which checks with which counts — and what it does not show.
            "as_of": (r.get("check") or {}).get("checked_at") if measured else None,
            "scope": ev.get("scope", _LEGACY_SCOPE) if measured else None,
            "checks": ev.get("checks") or _legacy_checks(ev) if measured else None,
            "snapshot": ev.get("snapshot") if measured else None,
            "limitation": LIMITATION if passed else None,
        }

    grouped: dict[str, list[dict]] = {CHECKS_PASSED: [], "failed": [], "unverified": []}
    for r in rows:
        grouped[r["status"]].append(summarize(r))
    return {
        "table": table,
        CHECKS_PASSED: grouped[CHECKS_PASSED],
        "failed": grouped["failed"],
        "unverified": grouped["unverified"],
        "guidance": (
            f"Prefer edges under `{CHECKS_PASSED}`: their key uniqueness and zero "
            "orphans held on the data checked, as of `as_of`, over `scope`. That is "
            "evidence about those rows, not a statement that the join is the right "
            "business relationship, preserves a metric's meaning, or still holds — "
            "re-check before relying on an old result. `failed` edges are known broken "
            "on the checked data: joining through them multiplied or dropped rows. "
            "`unverified` edges were never checked, or were checked over data that "
            "demonstrated nothing (an empty table, an all-NULL column); treat them as a "
            "hypothesis. Each carries its own `reason`."
        ),
    }


# Checks recorded before round seventeen carry no scope block; what they did
# is known from the method, and they are presented as the historical record
# they are (with their own `as_of`).
_LEGACY_SCOPE = {"method": "full table scan", "sampled": False, "filters": [],
                 "null_handling": "NULL keys excluded from distinct and orphan counts",
                 "recorded": "before scopes were recorded"}


def _legacy_checks(ev: dict) -> list[dict] | None:
    left, right = ev.get("left"), ev.get("right")
    if not (isinstance(left, dict) and isinstance(right, dict)):
        return None
    return _check_list(left, right, ev.get("matched_rows"))


def _check_list(left: dict, right: dict, matched) -> list[dict]:
    return [
        {"check": f"{side['relation']}.{side['column']} is unique",
         "measured": {"non_null": side.get("non_null"), "distinct": side.get("distinct"),
                      "rows": side.get("rows")}, "held": side.get("unique")}
        for side in (left, right)
    ] + [
        {"check": f"rows of {a['relation']} with no match in {b['relation']}",
         "measured": {"orphans": a.get("orphans")}}
        for a, b in ((left, right), (right, left))
    ] + [{"check": "rows that join across the edge", "measured": {"matched": matched}}]


def _check_edge(warehouse, rel_l: str, col_l: str, rel_r: str, col_r: str,
                declared_pks: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Uniqueness both sides, orphans both directions, cardinality class.

    A valid FK-style relationship needs at least one unique side and zero
    orphans flowing from the many side into it. Neither side unique = N:M —
    no key semantics, joins through it multiply rows. Full-table counts in
    v0; sampling windows arrive with the Phase 1 harness."""
    out: dict[str, Any] = {"method": "warehouse-cardinality-check"}
    try:
        l_total, l_distinct, l_rows = warehouse.query(
            f'SELECT COUNT({qcol(col_l)}), COUNT(DISTINCT {qcol(col_l)}), COUNT(*) '
            f'FROM {qident(rel_l)}'
        )[0]
        r_total, r_distinct, r_rows = warehouse.query(
            f'SELECT COUNT({qcol(col_r)}), COUNT(DISTINCT {qcol(col_r)}), COUNT(*) '
            f'FROM {qident(rel_r)}'
        )[0]
        matched = warehouse.query(
            f'SELECT COUNT(*) FROM {qident(rel_l)} l JOIN {qident(rel_r)} r '
            f'ON l.{qcol(col_l)} = r.{qcol(col_r)}'
        )[0][0]
        orphans_l = warehouse.query(
            f'SELECT COUNT(*) FROM {qident(rel_l)} l LEFT JOIN {qident(rel_r)} r '
            f'ON l.{qcol(col_l)} = r.{qcol(col_r)} '
            f'WHERE l.{qcol(col_l)} IS NOT NULL AND r.{qcol(col_r)} IS NULL'
        )[0][0]
        orphans_r = warehouse.query(
            f'SELECT COUNT(*) FROM {qident(rel_r)} r LEFT JOIN {qident(rel_l)} l '
            f'ON r.{qcol(col_r)} = l.{qcol(col_l)} '
            f'WHERE r.{qcol(col_r)} IS NOT NULL AND l.{qcol(col_l)} IS NULL'
        )[0][0]
    except Exception as e:  # noqa: BLE001 — missing relation, perms, etc.
        # "Could not check" is NOT "checked and failed": reporting an
        # unrunnable check as a broken relationship blames working code.
        out.update(unrunnable=True, error=f"{type(e).__name__}: {e}",
                   note="check could not run — the edge remains unverified "
                        "until the warehouse can answer")
        return out

    # Positive-evidence floor. COUNT ignores NULLs, so an empty table or an
    # all-NULL column yields 0 == 0 and would otherwise "prove" a unique key
    # with zero orphans — certifying a relationship on no evidence at all.
    # This gates CERTIFICATION only: a class that is demonstrable without
    # matching rows (N:M, established from distinct counts alone) is still
    # reported, because refusing to fail is its own kind of dishonesty.
    null_fraction_l = 1.0 - (l_total / l_rows) if l_rows else 1.0
    null_fraction_r = 1.0 - (r_total / r_rows) if r_rows else 1.0

    empty_sides = [f"{r}.{c}" for r, c, t in
                   ((rel_l, col_l, l_total), (rel_r, col_r, r_total)) if t == 0]
    # Uniqueness over a single value is trivially true and demonstrates
    # nothing; and a column that is mostly NULL does not participate in the
    # relationship at all, so a join through it silently drops those rows.
    sparse = [f"{r}.{c} ({int(f * 100)}% NULL)" for r, c, t, f in
              ((rel_l, col_l, l_total, null_fraction_l),
               (rel_r, col_r, r_total, null_fraction_r))
              if t and (t < 2 or f > 0.5)]
    if empty_sides:
        no_evidence = "no non-null values in " + ", ".join(empty_sides)
    elif matched == 0:
        no_evidence = "no rows join across this edge"
    elif sparse:
        no_evidence = "too few values to demonstrate a key: " + ", ".join(sparse)
    else:
        no_evidence = None

    unique_l = l_total == l_distinct
    unique_r = r_total == r_distinct
    cardinality = {(True, True): "1:1", (True, False): "1:N",
                   (False, True): "N:1", (False, False): "N:M"}[(unique_l, unique_r)]

    # Orphans that matter flow from the many side into the unique side.
    if unique_l and unique_r:
        blocking_orphans = orphans_l + orphans_r
    elif unique_l:
        blocking_orphans = orphans_r
    elif unique_r:
        blocking_orphans = orphans_l
    else:
        blocking_orphans = None  # N:M fails on cardinality alone

    # Orphans whose DIRECTION rests on non-trivial uniqueness. Uniqueness over
    # a single value is trivially true, and counting orphans INTO such a side
    # produced a false accusation on the most ordinary shape there is: a
    # brand-new fact table with one order made the estate "1:1", after which
    # every customer without an order yet was a "violation" and the edge
    # FAILED — telling agents a healthy join is known broken. Only a side that
    # demonstrated uniqueness over >= 2 values can anchor a violation.
    trusted_orphans = 0
    if unique_l and l_total >= 2:
        trusted_orphans += orphans_r
    if unique_r and r_total >= 2:
        trusted_orphans += orphans_l

    passed = (unique_l or unique_r) and (blocking_orphans == 0) and no_evidence is None
    parent = (f"{rel_l}.{col_l}" if unique_l else f"{rel_r}.{col_r}") \
        if (unique_l or unique_r) else None

    out.update(
        # Scope of what was observed, recorded with every check.
        scope={"method": "full table scan", "sampled": False, "filters": [],
               "null_handling": "NULL keys excluded from distinct and orphan counts; "
                                "each side's NULL fraction is reported"},
        executed_at=utcnow(),
        snapshot=getattr(warehouse, "snapshot", None),
        passed=passed,
        cardinality=cardinality,
        parent=parent,  # verified direction: the unique side
        matched_rows=matched,
        left={"relation": rel_l, "column": col_l, "rows": l_rows, "non_null": l_total,
              "distinct": l_distinct, "unique": unique_l, "orphans": orphans_l,
              "null_fraction": round(null_fraction_l, 4)},
        right={"relation": rel_r, "column": col_r, "rows": r_rows, "non_null": r_total,
               "distinct": r_distinct, "unique": unique_r, "orphans": orphans_r,
               "null_fraction": round(null_fraction_r, 4)},
    )
    # A declared primary key is a claim like any other. If the catalog says a
    # column is the PK and the warehouse says it is not unique, that is a
    # key-level contradiction — report it even when the edge otherwise passes.
    pk_violations = []
    for relation, column, is_unique in ((rel_l, col_l, unique_l), (rel_r, col_r, unique_r)):
        if (declared_pks or {}).get(relation) == [column] and not is_unique:
            pk_violations.append(f"declared primary key {relation}.{column} is NOT unique")
    if pk_violations:
        out["pk_violations"] = pk_violations
        passed = False
        out["passed"] = passed

    if no_evidence:
        out["no_evidence"] = no_evidence
    out["checks"] = _check_list(out["left"], out["right"], matched)

    # Not-certified splits in two, and collapsing them was a false accusation.
    # A DEMONSTRATED violation stands on evidence the floor cannot take away:
    # N:M is established from distinct counts, a non-unique declared PK from
    # duplicates, and orphans only when both sides actually carry values.
    # Everything else — an empty table, an all-NULL column, one distinct
    # value — is the ABSENCE of evidence, and reporting it as `failed` tells
    # an agent the join is known broken when nothing of the kind was shown.
    # An ORPHAN is positive evidence: a row exists whose non-NULL key value has
    # no match. The NULL-ness of OTHER rows cannot unmake it, so `not sparse`
    # discarded a measured violation — the exact shape of a broken backfill on
    # a newly-added nullable FK (mostly NULL, the populated part all orphans),
    # i.e. the most actionable failure this layer can find, reported as a
    # hypothesis with "nothing was demonstrated" printed beside "orphans: 100".
    # Only an EMPTY side makes orphans uninformative: with nothing on one side
    # every value on the other is trivially an orphan, which says nothing about
    # the relationship.
    demonstrated = (
        bool(pk_violations)
        or not (unique_l or unique_r)
        or bool(trusted_orphans and not empty_sides)
    )
    if no_evidence and not demonstrated:
        out["inconclusive"] = True

    if not passed:
        reasons = []
        if not (unique_l or unique_r):
            reasons.append("no unique side (N:M) — join semantics undefined")
        elif trusted_orphans and demonstrated:
            reasons.append(f"{trusted_orphans} orphan row(s) violate referential integrity")
        # Only say "nothing was demonstrated" when nothing WAS. Appending it
        # unconditionally produced a self-contradictory sentence: an orphan
        # count and "nothing was demonstrated" in one breath.
        if no_evidence and not demonstrated:
            reasons.append(f"{no_evidence} — nothing was demonstrated; "
                           "the edge remains unverified")
        elif no_evidence:
            reasons.append(f"note: {no_evidence}")
        reasons.extend(pk_violations)
        out["reason"] = "; ".join(reasons)
    return out


def same_relation(a: str, b: str) -> bool:
    """Do two relation names denote the same object?

    Component-wise with suffix tolerance and case-insensitive, so
    `PROD.ANALYTICS.FCT_ORDERS` from a dbt manifest matches
    `analytics.fct_orders` from a catalog YAML. Never a substring test:
    `raw.orders` must not match `raw.orders_backup`.

    Suffix tolerance is for QUALIFICATION, not for bare names — mined edges
    CAN be bare (an unqualified `orders` in query history over an uncataloged
    registry), and matching that against a failed `analytics.orders` in some
    other schema de-certified a healthy, fully checkable edge and recorded
    "orders failed to build" — a false statement — in the append-only check
    history. Both sides need at least a schema, mirroring execute.py's twin.
    """
    pa = [p for p in a.lower().split(".") if p]
    pb = [p for p in b.lower().split(".") if p]
    n = min(len(pa), len(pb))
    if n < 2:
        return pa == pb
    return pa[-n:] == pb[-n:]


def qcol(column: str) -> str:
    """Quote a column identifier, doubling any embedded quote (see qident)."""
    return '"' + column.replace('"', '""') + '"'
