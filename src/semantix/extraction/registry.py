"""Schema registry: catalog metadata plus model output schemas, built up in
dependency order during a scan.

Serves two jobs:
  * feeds sqlglot a nested schema so qualification and `SELECT *` expansion
    work for models that read other models;
  * resolves column lineage to physical terminals (raw catalog columns) by
    walking model projections transitively — the `source_columns` field of
    every fingerprint.
"""

from __future__ import annotations

from sqlglot import exp
from sqlglot.schema import MappingSchema

from semantix.extraction.sqlutil import table_map, unwrap_select


class SchemaRegistry:
    def __init__(self, catalog: dict[str, dict[str, str]]):
        # relation -> {column: type}
        self._types: dict[str, dict[str, str]] = {}
        # relation -> {column: physical terminal columns}
        self._terminals: dict[str, dict[str, tuple[str, ...]]] = {}
        self.physical: set[str] = set(catalog)
        # Cached per dialect: identifier normalization is dialect-specific
        # (Snowflake folds unquoted names to UPPER, most others to lower), so
        # one schema object cannot serve two dialects.
        self._schema_cache: dict[str | None, MappingSchema] = {}
        for rel, cols in catalog.items():
            self._types[rel] = dict(cols)
            self._terminals[rel] = {c: (f"{rel}.{c}",) for c in cols}

    def sqlglot_schema(self, dialect: str | None = None) -> MappingSchema:
        """Schema object for sqlglot — only relations we know the columns of.

        Three constraints, each learned the hard way:

        1. A columnless entry is poison. sqlglot infers nesting depth from
           the contents, so one `{}` table made the whole mapping unreadable
           and every qualification in the scan failed — one unparseable model
           dropped every OTHER model from `optimized` to `parsed`.

        2. The mapping must be UNIFORMLY deep. sqlglot refuses a mixed one
           outright ("Table X must match the schema's nesting level"), and a
           real estate always mixes: dbt emits `database.schema.model` while a
           hand-written catalog is usually `schema.table`. Whichever arity was
           seen first won and every relation at the other arity silently
           failed to resolve. Short names are therefore PADDED to the deepest
           arity present. Padding is safe because sqlglot resolves references
           by SUFFIX — a two-part `raw.orders` still matches a three-part
           entry, and vice versa — so nothing is lost and nothing is invented.

        3. `database.schema.table` is THREE levels, not a two-level dict with
           a dotted key. Splitting on the last dot only produced
           `{"RAW_PROD.landing": {"orders": ...}}`, which sqlglot can never
           match — so on Snowflake, Databricks and BigQuery, where every
           relation is three-part, proof-grade coverage was ~0% and nothing
           in the estate could ever be certified.

        Returns a built MappingSchema rather than a raw dict: sqlglot
        re-normalizes a dict on every optimize() call, which is O(models x
        relations x columns) across a scan — 40% of total runtime and the
        whole superlinearity at 2,000 models. It must be built with the SAME
        dialect the query is optimized under, or normalization disagrees and
        nothing resolves: Snowflake folds unquoted identifiers to upper case
        and most other dialects fold to lower.
        """
        if dialect not in self._schema_cache:
            try:
                schema = MappingSchema(self._nested(), dialect=dialect)
            except Exception:  # noqa: BLE001 — sqlglot rejects unknown dialects
                # A dialect we cannot honor must degrade, not raise: parsing
                # will fail for the same reason a moment later and the scan
                # will record FAILED, which is the honest answer. Building the
                # schema is not the place to end a run with a traceback.
                schema = MappingSchema(self._nested())
            self._schema_cache[dialect] = schema
        return self._schema_cache[dialect]

    def _nested(self) -> dict:
        known = {rel: cols for rel, cols in self._types.items() if cols}
        if not known:
            return {}
        depth = max(len(rel.split(".")) for rel in known)
        nested: dict[str, dict] = {}
        for rel, cols in known.items():
            parts = rel.split(".")
            # `_` is not a legal warehouse identifier in the position it
            # occupies here, so a padded entry can never collide with a real
            # one; and because lookup is by suffix, the placeholder is never
            # what a query has to match.
            parts = ["_"] * (depth - len(parts)) + parts
            node = nested
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = {c: (t or "text") for c, t in cols.items()}
        return nested

    def knows(self, relation: str) -> bool:
        return relation in self._types

    def columns(self, relation: str) -> list[str]:
        return list(self._types.get(relation, {}))

    def terminals_for(self, relation: str, column: str) -> tuple[str, ...]:
        table = self._terminals.get(relation)
        if table and column in table:
            return table[column]
        return (f"{relation}.{column}",)

    def register_model(self, relation: str, canon_ast: exp.Expression | None) -> None:
        """Derive a model's output columns (types + physical terminals) from
        its canonical AST. Called in topological order so upstream models are
        already resolvable."""
        cols: dict[str, str] = {}
        terms: dict[str, tuple[str, ...]] = {}
        select = unwrap_select(canon_ast)
        if select is not None:
            amap = table_map(select)
            single = next(iter(amap.values())) if len(amap) == 1 else None
            for proj in select.expressions:
                name = proj.alias_or_name
                if not name or name == "*":
                    continue
                cols[name] = self._projection_type(proj)
                inner = proj.this if isinstance(proj, exp.Alias) else proj
                terminal_set: set[str] = set()
                for col in inner.find_all(exp.Column):
                    src = amap.get(col.table) or single or col.table or "<unknown>"
                    terminal_set.update(self.terminals_for(src, col.name))
                terms[name] = tuple(sorted(terminal_set))
        # Never replace real knowledge with nothing. Writing `{}` for a model
        # we could not resolve erased a genuine catalog entry, after which
        # every downstream model over that table failed to qualify — and two
        # identical definitions then produced a false CONTRADICTION.
        if not cols and relation in self._types and self._types[relation]:
            return
        known_before = relation in self._types and bool(self._types[relation])
        self._types[relation] = cols
        self._terminals[relation] = terms
        self._add_to_cached_schemas(relation, cols, replaced=known_before)

    def _add_to_cached_schemas(self, relation: str, cols: dict[str, str],
                               replaced: bool) -> None:
        """Extend each cached MappingSchema in place instead of discarding it.

        Rebuilding the whole mapping per model is O(models x relations x
        columns) across a scan — the superlinearity that made 2,000 models
        take 100s. Incremental `add_table` keeps it near-linear. The cache is
        only discarded when the shape genuinely changes: a relation DEEPER
        than the mapping (which would re-pad every existing entry) or a
        replacement of an entry already in it.
        """
        if not cols or replaced:
            self._schema_cache.clear()
            return
        parts = relation.split(".")
        for dialect, schema in list(self._schema_cache.items()):
            depth = schema.depth() or len(parts)
            if len(parts) > depth:
                del self._schema_cache[dialect]  # re-pad everything on next read
                continue
            padded = ".".join(["_"] * (depth - len(parts)) + parts)
            try:
                schema.add_table(
                    padded, {c: (t or "text") for c, t in cols.items()}, dialect=dialect
                )
            except Exception:  # noqa: BLE001 — fall back to a full rebuild
                del self._schema_cache[dialect]

    @staticmethod
    def _projection_type(proj: exp.Expression) -> str:
        dtype = getattr(proj, "type", None)
        if isinstance(dtype, exp.DataType) and dtype.this not in (
            exp.DataType.Type.UNKNOWN,
            exp.DataType.Type.NULL,
        ):
            return dtype.sql()
        return "text"
