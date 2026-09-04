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
        # Output columns that are AGGREGATE-derived, per registered model. A
        # declared metric that re-aggregates one of these (`sum(mart.revenue)`
        # where the mart already sums) is a cross-LEVEL comparison the static
        # core cannot make honestly — without this record, it produced a
        # sev-96 CONTRADICTION blaming the mart's own internals.
        self._agg_outputs: dict[str, set[str]] = {}
        # Prefixes already seen by the incremental schema path, so a NEW head
        # (first model in a second database) invalidates the cache.
        self._known_prefixes: dict[int, set[tuple[str, ...]]] = {}
        # Cached per dialect: identifier normalization is dialect-specific
        # (Snowflake folds unquoted names to UPPER, most others to lower), so
        # one schema object cannot serve two dialects.
        self._schema_cache: dict[str | None, MappingSchema] = {}
        for rel, cols in catalog.items():
            self._types[rel] = dict(cols)
            self._terminals[rel] = {c: (f"{rel}.{c}",) for c in cols}

    def sqlglot_schema(self, dialect: str) -> MappingSchema:
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
           failed to resolve. Short entries are therefore REPLICATED under
           every head the full-depth entries actually use (see _nested) —
           suffix matching alone handles a short REFERENCE against a deep
           entry, but a fully-qualified reference never matches a
           placeholder-padded entry, which left hand catalogs at 40%
           coverage on three-part estates.

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
        and most other dialects fold to lower. `dialect` is REQUIRED rather
        than defaulted for exactly that reason — omitting it silently builds a
        schema that matches nothing, which reads as "this estate has no
        catalog" rather than as a bug.
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
            # Carried on the schema object so canonicalize_sql — whose
            # signature takes only (sql, schema, dialect) — can pin short
            # references to the `_` home without a registry parameter.
            schema._semantix_short_entries = self.short_entries()
            self._schema_cache[dialect] = schema
        return self._schema_cache[dialect]

    def _nested(self) -> dict:
        known = {rel: cols for rel, cols in self._types.items() if cols}
        if not known:
            return {}
        depth = max(len(rel.split(".")) for rel in known)
        # Heads observed on FULL-depth entries, e.g. every database name the
        # estate actually uses. A short entry is REPLICATED under each of
        # them, not only under the `_` placeholder: sqlglot matches a
        # fully-qualified reference (`ANALYTICS.RAW.ORDERS`) against the
        # entry's head part, and `_` never equals `ANALYTICS` — so a
        # two-part hand catalog against three-part SQL bound nothing and
        # coverage sat at 40% while the docstring claimed "and vice versa".
        # A short entry means "wherever this lives", which is exactly what
        # replication expresses; setdefault keeps real entries authoritative.
        prefixes: dict[int, set[tuple[str, ...]]] = {}
        for rel in known:
            parts = rel.split(".")
            if len(parts) == depth:
                for length in range(1, depth):
                    prefixes.setdefault(length, set()).add(tuple(parts[:length]))
        nested: dict[str, dict] = {}

        def insert(parts: list[str], cols: dict[str, str]) -> None:
            node = nested
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node.setdefault(parts[-1], {c: (t or "text") for c, t in cols.items()})

        for rel, cols in known.items():
            parts = rel.split(".")
            pad = depth - len(parts)
            if pad == 0:
                insert(parts, cols)
                continue
            # BOTH the `_` home and a replica under EVERY observed head.
            #
            # The two failure modes bracketing this: replicas alone make a
            # SHORT reference ambiguous (two candidate homes) the moment two
            # databases exist — sqlglot rightly refuses, and `from
            # raw.orders` collapsed to `parsed`; the `_` pad alone never
            # matches a FULLY-QUALIFIED reference, which left hand catalogs
            # at 40% coverage; and first-seen-head replication silently bound
            # only one database (70% on a PROD+DEV estate, per-column errors
            # that never name the cause).
            #
            # The resolution is that short references never race the
            # replicas: canonicalize_sql PINS any reference matching a short
            # entry to the `_` home before optimizing (see
            # _short_entry_homes), so short refs match `_` exactly and
            # qualified refs match their database's replica. The replicas are
            # copies of ONE physical table, so pinning loses nothing.
            insert(["_"] * pad + parts, cols)
            for prefix in prefixes.get(pad, set()):
                insert([*prefix, *parts], cols)
        return nested

    def short_entries(self) -> dict[tuple[str, ...], int]:
        """Lowercased part-tuples of catalog entries SHORTER than the mapping
        depth, mapped to how many parts `_`-padding adds. canonicalize_sql
        uses this to pin matching references to the `_` home."""
        known = {rel for rel, cols in self._types.items() if cols}
        if not known:
            return {}
        depth = max(len(rel.split(".")) for rel in known)
        out: dict[tuple[str, ...], int] = {}
        for rel in known:
            parts = rel.lower().split(".")
            if len(parts) < depth:
                out[tuple(parts)] = depth - len(parts)
        return out

    def knows(self, relation: str) -> bool:
        return relation in self._types

    def known_models(self) -> set[str]:
        """Relations registered from computed SQL (as opposed to catalog
        tables): reading one of these is reading THROUGH a definition."""
        return set(self._terminals)

    def columns(self, relation: str) -> list[str]:
        return list(self._types.get(relation, {}))

    def canonical_relation(self, relation: str) -> str:
        """The registry's own name for a relation referenced in any form.

        A short reference (`orders`, `raw.orders`) and a fully qualified one
        (`PROD.raw.orders`) to a table the catalog holds once are the same
        physical table; sqlglot binds both (via `_` padding and replicas),
        but keying lineage and join nodes on the reference AS WRITTEN made
        them different cores — a byte-identical dashboard query was accused
        of reading a different table. Exact key first, then a UNIQUE suffix
        match in either direction; ambiguous or unknown names pass through
        unchanged (an unknown raw table is still honestly named by what the
        query wrote). Non-relation markers (`cte:`, `<derived:`) pass through.
        """
        if not relation or relation in self._types or relation.startswith(("cte:", "<")):
            return relation
        target = relation.lower()
        matches = [k for k in self._types
                   if k.lower().endswith("." + target) or target.endswith("." + k.lower())
                   or k.lower() == target]
        return matches[0] if len(matches) == 1 else relation

    def terminals_for(self, relation: str, column: str) -> tuple[str, ...]:
        relation = self.canonical_relation(relation)
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
        agg_outputs: set[str] = set()
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
                if any(a.find_ancestor(exp.Window) is None
                       for a in inner.find_all(exp.AggFunc)):
                    agg_outputs.add(name)
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
        self._agg_outputs[relation] = agg_outputs
        self._add_to_cached_schemas(relation, cols, replaced=known_before)

    def aggregate_outputs(self, relation: str) -> set[str]:
        """Output columns of a registered model that are aggregate-derived.

        Resolved the way qualification resolves relations — case-insensitive,
        suffix-tolerant — not by exact string. The exact-key lookup meant any
        spelling variant of the model (`ANALYTICS.MARTS.MART_ORDERS` on
        Snowflake, database-omitted `marts.mart_orders` in hand YAML)
        bypassed the re-aggregation refusal, and the sev-96 metric-over-mart
        noise the refusal exists to prevent came straight back.
        """
        if relation in self._agg_outputs:
            return self._agg_outputs[relation]
        want = [p for p in relation.lower().split(".") if p]
        matches = []
        for key, cols in self._agg_outputs.items():
            have = key.lower().split(".")
            n = min(len(want), len(have))
            if n and want[-n:] == have[-n:]:
                matches.append(cols)
        # Ambiguous suffix (two different marts match) → refuse to guess.
        return matches[0] if len(matches) == 1 else set()

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
        # A full-depth registration that introduces a NEW head changes what
        # _nested would build (single-head replication may flip to `_`
        # padding, or vice versa) — the incremental path cannot reproduce
        # that, so resolution became a function of cache lifetime: identical
        # scans produced different canon levels depending on whether the
        # cache had been built before or after the new head appeared.
        new_head = False
        if len(parts) > 1:
            for length in range(1, len(parts)):
                if tuple(parts[:length]) not in self._known_prefixes.get(length, set()):
                    new_head = True
                self._known_prefixes.setdefault(length, set()).add(tuple(parts[:length]))
        for dialect, schema in list(self._schema_cache.items()):
            depth = schema.depth() or len(parts)
            if len(parts) != depth or new_head:
                # A short entry pads by REPLICATION under observed heads
                # (_nested), which the incremental path cannot reproduce —
                # and a deeper entry re-pads everything. Both rebuild.
                del self._schema_cache[dialect]
                continue
            try:
                schema.add_table(
                    relation, {c: (t or "text") for c, t in cols.items()},
                    dialect=dialect,
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
