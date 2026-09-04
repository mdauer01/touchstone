"""Catalog generation from information_schema (§2): the estate describes
its own physical shape.

Hand-maintained catalog YAML is the first thing to drift on a real estate —
and a stale catalog quietly degrades qualification, which degrades proof
grade, which shrinks what a scan can say. `information_schema.columns` is
ANSI-standard, so the catalog can be regenerated from the warehouse itself
and diffed like any other artifact. The shipped driver is DuckDB; on
Databricks, Snowflake and BigQuery the same query applies per catalog /
database / dataset once a driver exists (`semantix.warehouse` names the
seam) — this module does not claim to reach them today.

System namespaces are excluded; an empty result raises rather than writing
an empty catalog, because the likeliest cause is a wrong URL — and a scan
run against an empty catalog would report a wall of unresolvable columns
blaming the estate for the operator's typo.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# Namespaces that describe the warehouse rather than the estate.
_SYSTEM_SCHEMAS = ("information_schema", "pg_catalog")
_SYSTEM_CATALOGS = ("system", "temp")


def _sql_list(values: tuple[str, ...]) -> str:
    # Not `repr(tuple)`: a one-element tuple renders as ('x',), which
    # Snowflake and BigQuery reject.
    return "(" + ", ".join("'" + v.replace("'", "''") + "'" for v in values) + ")"


_COLUMNS_QUERY = f"""
SELECT table_catalog, table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE lower(table_schema) NOT IN {_sql_list(_SYSTEM_SCHEMAS)}
  AND lower(table_catalog) NOT IN {_sql_list(_SYSTEM_CATALOGS)}
ORDER BY table_catalog, table_schema, table_name, ordinal_position
""".strip()


def generate_catalog(warehouse) -> dict:
    """catalog.schema.table -> {column: type}, nested the way
    adapters.catalog.load_catalog reads it back."""
    rows = warehouse.query(_COLUMNS_QUERY)
    catalog: dict = {}
    dotted: set[str] = set()
    for cat, schema, table, column, data_type in rows:
        parts = (str(cat), str(schema), str(table))
        if any("." in p for p in parts):
            # Legal in DuckDB, unrepresentable in a dot-nested YAML catalog:
            # loaded back, `tbl_1.5` split into a phantom namespace and every
            # fingerprint in the scan degraded to `parsed`.
            dotted.add(".".join(parts))
            continue
        (catalog.setdefault(parts[0], {})
                .setdefault(parts[1], {})
                .setdefault(parts[2], {}))[str(column)] = str(data_type)
    if dotted:
        raise ValueError(
            "relation name(s) containing a dot cannot be represented in the "
            "catalog YAML: " + ", ".join(sorted(dotted)) + " — rename or "
            "exclude them; refusing to write a catalog that would misparse")
    if not catalog:
        raise ValueError(
            "information_schema returned no user tables — likely a wrong "
            "warehouse URL or an empty database; refusing to write an empty "
            "catalog that would blame the estate for it")
    return catalog


def write_catalog_yaml(catalog: dict, path: str | Path) -> tuple[int, int]:
    """Write the catalog; returns (tables, columns) for the caller to report."""
    tables = sum(len(t) for c in catalog.values() for t in c.values())
    columns = sum(len(cols) for c in catalog.values()
                  for t in c.values() for cols in t.values())
    Path(path).write_text(
        "# Generated from information_schema — regenerate rather than edit.\n"
        + yaml.safe_dump(catalog, sort_keys=True),
        encoding="utf-8")
    return tables, columns
