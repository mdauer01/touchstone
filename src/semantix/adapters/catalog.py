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

    def descend(prefix: str, value: dict, parent_siblings: dict | None = None) -> None:
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
        # All-dict children: namespace or a table whose EVERY column is a
        # struct? The shape alone is ambiguous — but a SIBLING that is
        # unambiguously a table proves this level is a level of TABLES, so an
        # all-struct node here is a table too. Without this, the all-struct
        # table was deleted and each struct column became a phantom relation,
        # silently reverting to the exact pre-fix behavior for the one shape
        # the fix's own comment promised was covered.
        sibling_tables = any(
            not isinstance(sv, dict)
            for sibling in (parent_siblings or {}).values()
            if isinstance(sibling, dict)
            for sv in sibling.values()
        )
        if sibling_tables:
            catalog[prefix] = {str(col): "struct" for col in nested}
            return
        for key, child in nested.items():
            descend(f"{prefix}.{key}" if prefix else str(key), child,
                    parent_siblings=nested)

    for key, value in data.items():
        if key in RESERVED_SECTIONS or not isinstance(value, dict):
            continue
        descend(str(key), value)
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
