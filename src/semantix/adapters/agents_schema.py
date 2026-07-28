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
_LOOKML_DSL = re.compile(
    r"""^\s*(?:NOT\s+NULL|NULL|EMPTY|-?\s*$)          # null / empty tests
        |[<>%^,]                                       # ranges, wildcards, lists
        |\b(?:to|before|after|ago|month|year|day|week|quarter|hour|minute|fiscal)\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


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
        if _LOOKML_DSL.search(literal) or not literal:
            untranslatable.append(f"{field_name}: {value}")
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
