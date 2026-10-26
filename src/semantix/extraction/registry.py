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

import copy

from sqlglot import exp
from sqlglot.schema import MappingSchema

from semantix.extraction.sqlutil import table_map, unwrap_select


class SchemaRegistry:
    def __init__(self, catalog: dict[str, dict[str, str]],
                 default_database: str | None = None,
                 default_schema: str | None = None):
        # EXECUTION CONTEXT: the session's default database and schema, as
        # supplied by the operator. It is the ONLY thing allowed to fill in a
        # qualifier the SQL omitted. A short catalog entry is not evidence
        # that `prod.raw.orders` and `dev.raw.orders` are one table — the
        # round-seven suffix match treated it as such and certified two
        # queries returning 100 and 900 as canonically identical.
        self._context = (default_database or None, default_schema or None)
        # Fixed before any fingerprint is taken (set_estate_depth), so whether
        # a reference counts as under-qualified never depends on scan order.
        self._estate_depth: int | None = None
        # relation -> {column: type}
        self._types: dict[str, dict[str, str]] = {}
        # relation -> {column: physical terminal columns}
        self._terminals: dict[str, dict[str, tuple[str, ...]]] = {}
        # Relations registered from computed SQL — distinct from catalog
        # tables, which also carry terminals (their own columns).
        self._models: set[str] = set()
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
            schema._semantix_context = self._context
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
        # Replication supplies COLUMN TYPES to qualification and nothing
        # more: a short entry does not mean prod.raw.orders and
        # dev.raw.orders are one table (round eight certified exactly that
        # mistake). Identity is canonical_relation's job, which never infers
        # sameness from a shared short entry. setdefault keeps real entries
        # authoritative.
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
            # qualified refs match their database's replica. Both bindings
            # are for column types only; neither decides relation identity.
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

    def shorter_spellings(self, relation: str) -> list[str]:
        """Described relations whose name is a proper suffix of `relation`
        (`raw.orders` for `prod.raw.orders`). For explaining a refusal only:
        a suffix is never identity."""
        parts = tuple(relation.lower().split("."))
        return sorted(r for r, cols in self._types.items() if cols
                      and len(r.split(".")) < len(parts)
                      and parts[-len(r.split(".")):] == tuple(r.lower().split(".")))

    def known_models(self) -> set[str]:
        """Relations registered from computed SQL (as opposed to catalog
        tables): reading one of these is reading THROUGH a definition."""
        return set(self._models)

    def columns(self, relation: str) -> list[str]:
        return list(self._types.get(relation, {}))

    def column_types(self, relation: str) -> dict[str, str]:
        """{column: declared type text} for one relation (empty if unknown)."""
        return {c: str(t) for c, t in (self._types.get(relation) or {}).items()}

    @property
    def execution_context(self) -> dict[str, str | None]:
        return {"default_database": self._context[0], "default_schema": self._context[1]}

    def without_context(self) -> "SchemaRegistry":
        """The same catalog and models with NO session defaults. A query-history
        export mixes sessions: the context the operator supplied for the
        estate's models says nothing about which table a dashboard's short
        name read, so history statements resolve through this view and get
        context only from their own row (or an explicit assertion)."""
        clone = copy.copy(self)
        clone._context = (None, None)
        clone._schema_cache = {}
        clone._known_prefixes = {k: set(v) for k, v in self._known_prefixes.items()}
        return clone

    def expand(self, relation: str) -> str:
        """Fill omitted LEADING qualifiers from the supplied execution context
        only. Never drops a part and never guesses one."""
        if not relation or relation.startswith(("cte:", "<")):
            return relation
        database, schema = self._context
        parts = relation.split(".")
        if len(parts) == 1 and schema:
            parts = [schema, *parts]
        if len(parts) == 2 and database:
            parts = [database, *parts]
        return ".".join(parts)

    def set_estate_depth(self, relations) -> None:
        """Fix the estate's full name depth from everything the scan will read
        (catalog, model relations, referenced relations) BEFORE fingerprinting,
        so under-qualification is a property of the estate, not of the order
        models happened to be registered in."""
        lengths = [len(r.split(".")) for r in [*self._types, *relations]
                   if r and not r.startswith(("cte:", "<"))]
        self._estate_depth = max(lengths) if lengths else 0

    def estate_depth(self) -> int:
        if self._estate_depth is not None:
            return self._estate_depth
        known = [r for r, cols in self._types.items() if cols]
        return max((len(r.split(".")) for r in known), default=0)

    def under_qualified(self, relation: str) -> bool:
        """True when a relation name, after the supplied context, still has
        fewer parts than the estate's names: which physical table it reads
        depends on the session's defaults, which the SQL does not carry."""
        if not relation or relation.startswith(("cte:", "<")):
            return False
        return len(self.expand(relation).split(".")) < self.estate_depth()

    def canonical_relation(self, relation: str) -> str:
        """The identity of a referenced relation.

        Exactly what the SQL wrote, with omitted leading qualifiers filled
        ONLY from the operator-supplied execution context, then mapped to the
        registry's own spelling when that is the same name. Qualifiers the
        SQL wrote are never dropped, and nothing is matched by suffix: the
        round-seven version mapped `prod.raw.orders` and `dev.raw.orders`
        onto a shared short catalog entry `raw.orders` and certified their
        sums as identical while they returned 100 and 900. Two references
        are the same relation only when their context-expanded names are
        equal; a short reference that stays short is flagged by
        under_qualified, never resolved by guessing.
        """
        if not relation or relation in self._types or relation.startswith(("cte:", "<")):
            return relation
        expanded = self.expand(relation)
        target = expanded.lower()
        matches = [k for k in self._types if self.expand(k).lower() == target]
        return matches[0] if len(matches) == 1 else expanded

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
        self._models.add(relation)
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
