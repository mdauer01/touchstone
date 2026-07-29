"""Physical catalog YAML: `schema.table.column: type` at any nesting depth.

Reserved top-level sections:

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
    """Two passes, because the shape alone is ambiguous.

    A node whose values are all scalars is a table; a node holding both
    scalars and dicts is a table with STRUCT/VARIANT/RECORD columns (routine
    on BigQuery, Snowflake and Databricks). But a node whose values are ALL
    dicts could be a namespace OR a table whose every column is a struct —
    and the single-parent sibling rule missed table evidence one branch over:
    `prod.raw.events` (all-struct) stayed a namespace even though
    `prod.analytics.orders` proved tables live at depth three, minting a
    four-part phantom relation inconsistent with the estate's own naming.

    Pass one records the DEPTHS at which unambiguous tables live; pass two
    classifies all-dict nodes at a known table depth as tables of structs.
    Two all-struct tables with no scalar table anywhere in the file remain
    genuinely undecidable and stay namespaces.

    `None`-valued keys are stubs (an empty-table placeholder, a YAML slip) —
    counting one as a scalar COLUMN converted the whole namespace into a
    fabricated relation with a column typed "None" and deleted every sibling
    table.
    """
    data = load_yaml(path)
    catalog: dict[str, dict[str, str]] = {}

    def split(value: dict) -> tuple[dict, dict]:
        scalars = {k: v for k, v in value.items()
                   if not isinstance(v, dict) and v is not None}
        nested = {k: v for k, v in value.items() if isinstance(v, dict)}
        return scalars, nested

    table_depths: set[int] = set()

    def scout(value: dict, depth: int) -> None:
        if not value:
            return
        scalars, nested = split(value)
        if scalars:
            table_depths.add(depth)
            return
        for child in nested.values():
            scout(child, depth + 1)

    for key, value in data.items():
        if key in RESERVED_SECTIONS or not isinstance(value, dict):
            continue
        scout(value, 1)

    def descend(prefix: str, value: dict, depth: int) -> None:
        if not value:
            return
        scalars, nested = split(value)
        if not nested:
            catalog[prefix] = {str(col): str(dtype) for col, dtype in scalars.items()}
            return
        if scalars:
            catalog[prefix] = {
                **{str(col): str(dtype) for col, dtype in scalars.items()},
                **{str(col): "struct" for col in nested},
            }
            return
        if depth in table_depths:
            catalog[prefix] = {str(col): "struct" for col in nested}
            return
        for key, child in nested.items():
            descend(f"{prefix}.{key}" if prefix else str(key), child, depth + 1)

    for key, value in data.items():
        if key in RESERVED_SECTIONS or not isinstance(value, dict):
            continue
        descend(str(key), value, 1)
    return catalog


def load_primary_keys(path: str | Path) -> dict[str, list[str]]:
    data = load_yaml(path)
    section = data.get("primary_keys") or {}
    # Wrong shapes below the top level crashed with raw AttributeErrors,
    # despite loadutil's named-error contract one call up.
    if not isinstance(section, dict):
        raise ValueError(
            f"{path}: `primary_keys` must be a mapping of relation -> column "
            f"list, found {type(section).__name__}")
    out = {}
    for rel, cols in section.items():
        if cols is not None and not isinstance(cols, list):
            raise ValueError(
                f"{path}: primary_keys.{rel} must be a list of columns, "
                f"found {type(cols).__name__}")
        out[str(rel)] = [str(c) for c in cols or []]
    return out


def load_declared_relationships(path: str | Path) -> list[tuple[str, str]]:
    """[(from, to)] as fully qualified schema.table.column strings. Declared
    direction is child -> parent, but edge identity downstream is
    direction-free; direction is re-derived from verified uniqueness."""
    data = load_yaml(path)
    section = data.get("relationships") or []
    if not isinstance(section, list):
        raise ValueError(
            f"{path}: `relationships` must be a list of {{from, to}} entries, "
            f"found {type(section).__name__}")
    out = []
    for entry in section:
        if not isinstance(entry, dict):
            raise ValueError(
                f"{path}: each relationships entry must be a {{from, to}} "
                f"mapping, found {type(entry).__name__}: {entry!r}")
        src, dst = entry.get("from"), entry.get("to")
        if src and dst:
            out.append((str(src), str(dst)))
    return out
