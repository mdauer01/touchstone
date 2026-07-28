"""Agents Schema adapter (§2): claims from the warehouse's own AGENTS.* tables.

Agents Schema (opendatainfrastructure.com/agents-schema) publishes declared
semantics — dbt docs, LookML measures, OSI metrics — as queryable tables
inside the warehouse so AI agents can discover business context with plain
SQL. By the spec's own documentation it carries **no verification, no
history, and no conflict resolution**: it is a distribution rail for claims.
That makes it a first-class claims source for semantix — the schema tells an
agent what a metric means; the scan decides whether that meaning is true.

Two intake modes, zero-infrastructure first:

* a JSON export — `{"osi_metric": [...], "osi_dataset": [...],
  "lookml_measure": [...], "lookml_view": [...]}`, lowercase table names,
  one dict per row (column names per the spec);
* a DuckDB file containing the AGENTS schema (guarded import; the live
  Snowflake connection is the same queries behind a connector, later).

Translation honesty: `OSI_METRIC.expression` and `LOOKML_MEASURE.sql` are
free-form TEXT (the spec calls its own table matching "heuristic"). LookML
`${TABLE}.col` substitution and the common `[field: "value"]` filter form
are handled; anything stranger passes through raw and degrades at
canonicalization — an unparseable claim becomes UNRESOLVED, never a silently
wrong CERTIFIED.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from semantix.adapters.base import DeclaredSpec

AGENTS_TABLES = ("osi_metric", "osi_dataset", "osi_field",
                 "lookml_measure", "lookml_view")

_LOOKML_TABLE_REF = re.compile(r"\$\{TABLE\}\.")
_LOOKML_FIELD_REF = re.compile(r"\$\{(\w+)\}")
_LOOKML_FILTER_RE = re.compile(r'(\w+)\s*:\s*"([^"]*)"')

# LookML aggregate measure types that wrap `sql` in an aggregate function.
_LOOKML_AGG_TYPES = {"sum", "average", "avg", "min", "max", "count", "count_distinct"}


def _lookml_sql(sql: str) -> str:
    """Resolve LookML substitution operators to bare column references —
    `${TABLE}.amount` → `amount`, `${amount_net}` → `amount_net`."""
    return _LOOKML_FIELD_REF.sub(r"\1", _LOOKML_TABLE_REF.sub("", sql)).strip()


def _lookml_expr(row: dict[str, Any]) -> str | None:
    sql = _lookml_sql(str(row.get("sql") or ""))
    mtype = str(row.get("type") or "").lower()
    if not sql:
        return None
    if mtype == "count_distinct":
        return f"count(distinct {sql})"
    if mtype in _LOOKML_AGG_TYPES:
        return f"{'avg' if mtype == 'average' else mtype}({sql})"
    return sql  # type: number / pre-aggregated expression — pass through


# LookML filter values are a small language, not literals: ranges (`>100`,
# `1 to 10`), wildcards (`%x%`), null tests, inline lists, and relative dates.
# Anything carrying this syntax cannot be rendered as an equality, and
# guessing produces a predicate nobody declared.
#
# Recognizing it takes two rules, not one, because the two halves of the
# language behave differently:

# 1. STRUCTURE. These are operators wherever they appear, whatever the field
#    type: comparisons, list separators, ranges, escapes, and a leading `not`.
#    A comma is always a list (an OR), never a character in a value.
_LOOKML_OPERATOR = re.compile(r"""[<>=!,^\[\]()]|^\s*not\b""", re.IGNORECASE | re.VERBOSE)

#    `to` is the range operator — but only between operands. It is DSL in
#    `1 to 10` and `2024-01-01 to 2024-02-01`, and an ordinary word in
#    `direct to consumer`. The discriminator is a digit on one side of it.
_LOOKML_RANGE = re.compile(r"\d\S*\s+to\b|\bto\s+\S*\d", re.IGNORECASE)

# 2. DATE SHAPE. `to`, `before`, `month` and friends are only date syntax in a
#    value SHAPED like a date expression. Matching the bare words made
#    `channel: "direct to consumer"` and `region: "Tokyo to Osaka"` refuse to
#    translate — the safe direction, but wrong often enough to make the
#    LookML adapter useless. A date expression always leads with a relative
#    keyword, or carries a number+unit or a date literal somewhere in it.
_DATE_UNIT = (r"(?:second|minute|hour|day|week|month|quarter|year|"
              r"fiscal[ _](?:quarter|year))s?")
#    Each keyword is matched with the operand its syntax requires, not bare:
#    `after` is a date operator in `after 2024-01-01` and an ordinary word in
#    `after hours support`.
_DATE_SHAPED = re.compile(
    rf"""^\s*(?:today|yesterday|tomorrow|now)\s*$        # standalone
       | ^\s*(?:before|after|past)\s+\S*\d               # before 2024-01-01
       | ^\s*(?:this|last|next)\s+(?:\d+\s*)?{_DATE_UNIT}\b   # this month, last 3 days
       | ^\s*fy\s*\d{{2,4}}\b                            # FY2024
       | ^\s*\d{{4}}\s*$          # a bare year IS a date filter on a date field,
                                  # and nothing here knows the field's type
       | \b\d+\s*{_DATE_UNIT}\b   # 3 days, 12 months ago
       | \d{{4}}[-/]\d{{1,2}}     # a date literal, incl. `2024-01-01 to 2024-02-01`
    """,
    re.IGNORECASE | re.VERBOSE,
)

# 3. NULL / EMPTY tests, which are whole values rather than prefixes. Anchored
#    at both ends: `^NULL` alone also matched the ordinary literal "NULLIFY".
_LOOKML_NULLISH = re.compile(r"^\s*(?:not\s+null|null|empty|blank)\s*$", re.IGNORECASE)

# 4. WILDCARDS. `%` is many characters and `_` is exactly one — Looker string
#    filters are LIKE patterns, not literals. `%` was refused and `_` was not,
#    so `channel: "paid_search"` rendered as `channel = 'paid_search'`: a
#    predicate STRICTER than the declaration, which is the direction that can
#    falsely match code that is also strict. Underscores are common enough in
#    real values that this is not a corner case. Refusing both costs some
#    translations; inventing the wrong one costs a certification.
_LOOKML_WILDCARD = re.compile(r"[%_]")


def _lookml_untranslatable(literal: str) -> str | None:
    """Why this filter value cannot be rendered as a predicate, or None."""
    if not literal.strip():
        return "empty filter value"
    if _LOOKML_NULLISH.match(literal):
        return "null/empty test, not an equality"
    if _LOOKML_OPERATOR.search(literal) or _LOOKML_RANGE.search(literal):
        return "comparison, list or range syntax"
    if _DATE_SHAPED.search(literal):
        return "relative or absolute date expression"
    if _LOOKML_WILDCARD.search(literal):
        return "wildcard pattern (`%` many, `_` one) — not an equality"
    return None


def _lookml_filters(raw: str | None) -> tuple[list[str], list[str]]:
    """`[status: "completed", channel: "-internal"]` → (predicates, untranslatable).

    Only plain literal equality and its negation are translated. A leading `-`
    is LookML negation, and in Looker that INCLUDES rows where the field is
    NULL — rendering it as a bare `<>` silently drops them, making the claim
    stricter than the declaration and able to falsely match a model that also
    drops them.
    """
    if not raw:
        return [], []
    pairs = _LOOKML_FILTER_RE.findall(raw)
    if not pairs:
        return [], [str(raw)]

    predicates: list[str] = []
    untranslatable: list[str] = []
    for field_name, value in pairs:
        negated = value.startswith("-")
        literal = value[1:] if negated else value
        reason = _lookml_untranslatable(literal)
        if reason:
            untranslatable.append(f"{field_name}: {value} ({reason})")
        elif negated:
            predicates.append(f"({field_name} <> '{literal}' OR {field_name} IS NULL)")
        else:
            predicates.append(f"{field_name} = '{literal}'")
    return predicates, untranslatable


def claims_from_tables(tables: dict[str, list[dict[str, Any]]],
                       path: str | None = None) -> list[DeclaredSpec]:
    """AGENTS.* rows → DeclaredSpecs. Pure function of the table contents so
    every intake mode (JSON, DuckDB, warehouse) produces identical claims."""
    specs: list[DeclaredSpec] = []

    datasets = {str(r.get("name")): r for r in tables.get("osi_dataset", [])}
    for row in tables.get("osi_metric", []):
        if not row.get("name"):
            continue
        dataset = datasets.get(str(row.get("dataset_name") or ""))
        specs.append(DeclaredSpec(
            system="agents_osi",
            name=str(row["name"]),
            description=row.get("description") or row.get("ai_context"),
            # OSI metrics carry no model unless the export resolves the
            # dataset; without one the claim is honestly inexpressible.
            model=str(dataset["source_table"]) if dataset and dataset.get("source_table") else None,
            expr=row.get("expression"),
            path=path,
        ))

    views = {str(r.get("name")): r for r in tables.get("lookml_view", [])}
    for row in tables.get("lookml_measure", []):
        if not row.get("measure_name"):
            continue
        view = views.get(str(row.get("view_name") or ""))
        model = str(view["sql_table_name"]) if view and view.get("sql_table_name") else None
        raw_filters = row.get("filters")
        filters, untranslatable = _lookml_filters(
            raw_filters if isinstance(raw_filters, str) or raw_filters is None
            else str(raw_filters)
        )
        specs.append(DeclaredSpec(
            system="agents_lookml",
            name=str(row["measure_name"]),
            description=row.get("description") or row.get("ai_context"),
            model=model,
            expr=_lookml_expr(row),
            filters=filters,
            untranslatable=untranslatable,
            path=path,
        ))
    return specs


def load_agents_schema(path: str | Path) -> list[DeclaredSpec]:
    """Load claims from an Agents Schema export: `.json` dump or a DuckDB
    file containing the AGENTS schema."""
    p = Path(path)
    if p.suffix.lower() == ".json":
        data = json.loads(p.read_text())
        tables = {k.lower(): v for k, v in data.items() if isinstance(v, list)}
        return claims_from_tables(tables, path=str(p))

    try:
        import duckdb
    except ImportError as e:
        raise ValueError(
            f"{p}: reading a DuckDB Agents Schema needs `pip install duckdb` "
            "(or pass a .json export)") from e
    con = duckdb.connect(str(p), read_only=True)
    # Fully qualify with the catalog: a file named agents.duckdb makes the bare
    # `agents.<table>` reference ambiguous between catalog and schema.
    catalog = con.execute("SELECT current_database()").fetchone()[0]
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in AGENTS_TABLES:
        try:
            cur = con.execute(f'SELECT * FROM "{catalog}".agents.{table}')
        except Exception:  # noqa: BLE001 — providers publish subsets; absent is normal
            continue
        columns = [d[0].lower() for d in cur.description]
        tables[table] = [dict(zip(columns, r)) for r in cur.fetchall()]
    con.close()
    return claims_from_tables(tables, path=str(p))
