"""Catalog loader: physical schema metadata used for identifier qualification,
`SELECT *` expansion, and lineage terminal resolution — plus optional declared
key/relationship metadata for the relationship layer.

Format (nested schema -> table -> column: type, with reserved top-level
sections that are NOT schemas):

    raw:
      orders:
        order_id: bigint
        amount: "decimal(18,2)"
    primary_keys:
      raw.orders: [order_id]
    relationships:
      - from: raw.payments.order_id
        to: raw.orders.order_id
"""

from __future__ import annotations

from pathlib import Path

from semantix.adapters.loadutil import load_yaml

RESERVED_SECTIONS = {"primary_keys", "relationships"}


def load_catalog(path: str | Path) -> dict[str, dict[str, str]]:
    data = load_yaml(path)
    catalog: dict[str, dict[str, str]] = {}

    def descend(prefix: str, value: dict) -> None:
        """Recurse to arbitrary depth so any warehouse naming works.

        A node is a TABLE when all of its values are scalars; otherwise it is
        a namespace and we keep descending, joining the key path with dots.
        Fixing the depth at two levels meant a three-part warehouse
        (`database.schema.table` — Snowflake, BigQuery, Databricks) either
        could not be expressed or was silently mangled into a column named
        after a dict.
        """
        if not value:
            return
        scalars = {k: v for k, v in value.items() if not isinstance(v, dict)}
        nested = {k: v for k, v in value.items() if isinstance(v, dict)}
        if not nested:
            catalog[prefix] = {str(col): str(dtype) for col, dtype in scalars.items()}
            return
        if scalars:
            # A node holding BOTH scalars and dicts is a table with a STRUCT /
            # VARIANT / RECORD column — routine on BigQuery, Snowflake and
            # Databricks. Treating it as a pure namespace deleted the table
            # outright (its scalar columns unrecoverable) and invented a
            # phantom relation named after the struct. Keep the table, and
            # record the nested column by name so it is not silently absent.
            catalog[prefix] = {
                **{str(col): str(dtype) for col, dtype in scalars.items()},
                **{str(col): "struct" for col in nested},
            }
            return
        for key, child in nested.items():
            descend(f"{prefix}.{key}" if prefix else str(key), child)

    for key, value in data.items():
        if key in RESERVED_SECTIONS or not isinstance(value, dict):
            continue
        descend(str(key), value)
    return catalog


def load_primary_keys(path: str | Path) -> dict[str, list[str]]:
    data = load_yaml(path)
    return {
        str(rel): [str(c) for c in cols or []]
        for rel, cols in (data.get("primary_keys") or {}).items()
    }


def load_declared_relationships(path: str | Path) -> list[tuple[str, str]]:
    """[(from, to)] as fully qualified schema.table.column strings. Declared
    direction is child -> parent, but edge identity downstream is
    direction-free; direction is re-derived from verified uniqueness."""
    data = load_yaml(path)
    out = []
    for entry in data.get("relationships") or []:
        src, dst = entry.get("from"), entry.get("to")
        if src and dst:
            out.append((str(src), str(dst)))
    return out
