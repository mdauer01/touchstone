"""Parameterized warehouse access — the §5 execution seam, made explicit.

Everything the relationship layer needs from a warehouse is two methods:
run a read-only query, close. v0 ships the DuckDB driver (zero-infra,
fully tested); a Snowflake/BigQuery/Databricks driver is those same two
methods behind their SDK, not a redesign — `connect_warehouse` names the
seam when asked for one that isn't shipped yet.

Accepted URLs:
    path/to/file.duckdb            local DuckDB file (read-only)
    duckdb://path/to/file.duckdb   same, explicit scheme, relative path
    duckdb:///abs/path/file.duckdb absolute path (extra leading slashes collapse)
    :memory:                       ephemeral in-memory DuckDB
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence


class UnsupportedWarehouseError(ValueError):
    pass


class DuckDBWarehouse:
    """Reference driver. Verification is read-only by construction: file-backed
    connections open read_only, and nothing in the layer issues DDL/DML."""

    dialect = "duckdb"

    def __init__(self, connection, label: str):
        self._con = connection
        self.label = label
        # relation -> why it could not be built (ephemeral warehouses only)
        self.failed_relations: dict[str, str] = {}

    @classmethod
    def from_path(cls, path: str | Path) -> "DuckDBWarehouse":
        import duckdb

        return cls(duckdb.connect(str(path), read_only=True), label=str(path))

    @classmethod
    def ephemeral(cls, seed_sql: str | None = None, artifacts: Sequence | None = None,
                  label: str = ":memory:") -> "DuckDBWarehouse":
        """In-memory warehouse built from a seed script, with the estate's
        models materialized as views (same pattern as the execution slice) —
        the zero-infra demo/CI path."""
        import duckdb

        import sqlglot

        con = duckdb.connect()
        if seed_sql:
            con.execute(seed_sql)
        failed: dict[str, str] = {}
        for artifact in artifacts or []:
            schema, _, _ = artifact.relation.rpartition(".")
            try:
                # One parsed statement, re-rendered — never the raw file text.
                statements = [s for s in sqlglot.parse(artifact.sql, read=cls.dialect)
                              if s is not None]
                if len(statements) != 1:
                    raise ValueError(f"expected one statement, found {len(statements)}")
                if schema:
                    con.execute(f"CREATE SCHEMA IF NOT EXISTS {qident(schema)}")
                con.execute(
                    f"CREATE OR REPLACE VIEW {qident(artifact.relation)} AS "
                    f"({statements[0].sql(dialect=cls.dialect)})"
                )
            except Exception as e:  # noqa: BLE001
                # Remembered, not swallowed: a relation that never built must
                # not have join keys "verified" against whatever else answers
                # to that name.
                failed[artifact.relation] = str(e)
        warehouse = cls(con, label=label)
        warehouse.failed_relations = failed
        return warehouse

    def query(self, sql: str) -> list[tuple]:
        return self._con.execute(sql).fetchall()

    def close(self) -> None:
        self._con.close()


def qident(relation: str) -> str:
    """schema.table -> "schema"."table".

    Identifiers reach us from parsed estate SQL and from catalog YAML, i.e.
    from files we do not control, and they are interpolated into checks that
    run against a customer warehouse. Embedded double quotes are doubled so a
    crafted identifier cannot break out of the quoting and append SQL.
    """
    return ".".join(f'"{part.replace(chr(34), chr(34) * 2)}"' for part in relation.split("."))


def connect_warehouse(url: str) -> DuckDBWarehouse:
    scheme, sep, rest = url.partition("://")
    if not sep:
        if url == ":memory:":
            return DuckDBWarehouse.ephemeral()
        return DuckDBWarehouse.from_path(url)
    if scheme == "duckdb":
        # One grammar, documented in the module docstring: `duckdb://rel/path`
        # is relative, `duckdb:///abs/path` is absolute (extra leading slashes
        # collapse). The previous rule stripped ALL leading slashes when there
        # were two or more, so the documented `duckdb:///wh.db` opened `/wh.db`
        # at the filesystem root on one path and `wh.db` on another.
        path = "/" + rest.lstrip("/") if rest.startswith("/") else rest
        if not path.strip("/"):
            raise UnsupportedWarehouseError(
                "duckdb:// carries no path — pass duckdb://<file> or duckdb:///<abs path>")
        return DuckDBWarehouse.from_path(path)
    raise UnsupportedWarehouseError(
        f"no {scheme}:// driver shipped yet — a driver is query(sql)->rows, "
        "close(), and a dialect attribute; see semantix/warehouse.py"
    )
