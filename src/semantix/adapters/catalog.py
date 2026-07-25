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

import yaml

RESERVED_SECTIONS = {"primary_keys", "relationships"}


def load_catalog(path: str | Path) -> dict[str, dict[str, str]]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    catalog: dict[str, dict[str, str]] = {}
    for schema_name, tables in data.items():
        if schema_name in RESERVED_SECTIONS:
            continue
        for table_name, columns in (tables or {}).items():
            catalog[f"{schema_name}.{table_name}"] = {
                str(col): str(dtype) for col, dtype in (columns or {}).items()
            }
    return catalog


def load_primary_keys(path: str | Path) -> dict[str, list[str]]:
    data = yaml.safe_load(Path(path).read_text()) or {}
    return {
        str(rel): [str(c) for c in cols or []]
        for rel, cols in (data.get("primary_keys") or {}).items()
    }


def load_declared_relationships(path: str | Path) -> list[tuple[str, str]]:
    """[(from, to)] as fully qualified schema.table.column strings. Declared
    direction is child -> parent, but edge identity downstream is
    direction-free; direction is re-derived from verified uniqueness."""
    data = yaml.safe_load(Path(path).read_text()) or {}
    out = []
    for entry in data.get("relationships") or []:
        src, dst = entry.get("from"), entry.get("to")
        if src and dst:
            out.append((str(src), str(dst)))
    return out
