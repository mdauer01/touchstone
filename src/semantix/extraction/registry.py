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

from semantix.extraction.sqlutil import table_map, unwrap_select


class SchemaRegistry:
    def __init__(self, catalog: dict[str, dict[str, str]]):
        # relation -> {column: type}
        self._types: dict[str, dict[str, str]] = {}
        # relation -> {column: physical terminal columns}
        self._terminals: dict[str, dict[str, tuple[str, ...]]] = {}
        self.physical: set[str] = set(catalog)
        for rel, cols in catalog.items():
            self._types[rel] = dict(cols)
            self._terminals[rel] = {c: (f"{rel}.{c}",) for c in cols}

    def sqlglot_schema(self) -> dict:
        """Nested schema for sqlglot — containing only relations we actually
        know the columns of.

        A columnless entry is poison. sqlglot infers the schema's nesting
        depth from its contents, so a single `{}` table (or a bare relation
        name with no schema part) makes the whole mapping unreadable and
        every qualification in the scan fails — one unparseable model in a
        real estate dropped every OTHER model from `optimized` to `parsed`
        and coverage from 100% to 0%. An unknown relation must be ABSENT
        here, which degrades only the models that reference it.
        """
        nested: dict[str, dict] = {}
        for rel, cols in self._types.items():
            if not cols:
                continue
            db, _, table = rel.rpartition(".")
            if not db:
                # A bare relation has a different depth from `schema.table`;
                # mixing the two corrupts sqlglot's depth detection.
                continue
            nested.setdefault(db, {})[table] = {c: (t or "text") for c, t in cols.items()}
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
        self._types[relation] = cols
        self._terminals[relation] = terms

    @staticmethod
    def _projection_type(proj: exp.Expression) -> str:
        dtype = getattr(proj, "type", None)
        if isinstance(dtype, exp.DataType) and dtype.this not in (
            exp.DataType.Type.UNKNOWN,
            exp.DataType.Type.NULL,
        ):
            return dtype.sql()
        return "text"
