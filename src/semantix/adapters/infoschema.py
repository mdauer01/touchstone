"""Catalog generation from information_schema (§2): the estate describes
its own physical shape.

Hand-maintained catalog YAML is the first thing to drift on a real estate —
and a stale catalog quietly degrades qualification, which degrades proof
grade, which shrinks what a scan can say. `information_schema.columns` is
ANSI-standard and served by DuckDB, Databricks (per catalog), Snowflake and
BigQuery alike, so the catalog can be regenerated from the warehouse itself
and diffed like any other artifact.

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

_COLUMNS_QUERY = f"""
SELECT table_catalog, table_schema, table_name, column_name, data_type
FROM information_schema.columns
WHERE lower(table_schema) NOT IN {_SYSTEM_SCHEMAS!r}
  AND lower(table_catalog) NOT IN {_SYSTEM_CATALOGS!r}
ORDER BY table_catalog, table_schema, table_name, ordinal_position
""".strip()


def generate_catalog(warehouse) -> dict:
    """catalog.schema.table -> {column: type}, nested the way
    adapters.catalog.load_catalog reads it back."""
    rows = warehouse.query(_COLUMNS_QUERY)
    catalog: dict = {}
    for cat, schema, table, column, data_type in rows:
        (catalog.setdefault(str(cat), {})
                .setdefault(str(schema), {})
                .setdefault(str(table), {}))[str(column)] = str(data_type)
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
