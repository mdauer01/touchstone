"""Agents Schema adapter (§2): claims from the warehouse's own AGENTS.* tables.

Agents Schema (opendatainfrastructure.com/agents-schema) publishes declared
semantics — dbt docs, LookML measures, OSI metrics — as queryable tables
inside the warehouse so AI agents can discover business context with plain
SQL. By the spec's own documentation it carries **no verification, no
history, and no conflict resolution**: it is a distribution rail for claims.
That makes it a first-class claims source for touchstone — the schema tells an
agent what a metric means; the scan decides whether that meaning is true.

Two intake modes, zero-infrastructure first:

* a JSON export — `{"osi_metric": [...], "osi_dataset": [...],
  "lookml_measure": [...], "lookml_view": [...]}`, lowercase table names,
  one dict per row (column names per the spec);
* a DuckDB file containing the AGENTS schema (guarded import; the live
  Snowflake connection is the same queries behind a connector, later).

Translation honesty: `OSI_METRIC.expression` and `LOOKML_MEASURE.sql` are
free-form TEXT (the spec calls its own table matching "heuristic"). LookML
`${TABLE}.col` substitution and the common `[field: "value"]` filter form
are handled; anything stranger passes through raw and degrades at
canonicalization — an unparseable claim becomes UNRESOLVED, never a silently
wrong CERTIFIED.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from touchstone.adapters.base import DeclaredSpec
from touchstone.adapters.loadutil import load_json

AGENTS_TABLES = ("osi_metric", "osi_dataset", "osi_field",
                 "lookml_measure", "lookml_view")

_LOOKML_TABLE_REF = re.compile(r"\$\{TABLE\}\.")
_LOOKML_FIELD_REF = re.compile(r"\$\{(\w+)\}")
# The modern form: `[status: "completed", channel: "-internal"]`. The field is
# matched with dots allowed so a VIEW-QUALIFIED reference is SEEN rather than
# resynced past: `\w+` could not match `users.country`, so the regex skipped
# the qualifier and matched `country`, applying a joined view's filter to this
# view's base table as though it were a local column.
_LOOKML_FILTER_RE = re.compile(r'([\w.]+)\s*:\s*"([^"]*)"')

# The legacy form, still valid and ubiquitous in pre-21.12 LookML:
#   filters: { field: status, value: "complete" }
# The modern regex matched the sub-token `value: "complete"` inside it and
# produced a predicate on a column literally named `value` — which exists in
# plenty of fact tables — while dropping the real `status` filter entirely.
_LOOKML_LEGACY_BLOCK = re.compile(
    r"\{\s*field\s*:\s*([\w.]+)\s*,\s*value\s*:\s*\"([^\"]*)\"\s*\}", re.IGNORECASE)
# `{`-anchored: the bare `\bfield\s*:` marker false-positived on a MODERN
# filter over a field literally named `field` (`[field: "webinar"]`) and on
# the token inside an ordinary value (`[category: "field: hockey"]`),
# refusing ordinary declarations as unreadable legacy blocks.
_LOOKML_LEGACY_MARKER = re.compile(r"\{\s*field\s*:", re.IGNORECASE)
# Any brace block the legacy regex did NOT consume — a legacy block in a
# shape we cannot read must be reported, not dropped.
_LOOKML_BRACE_BLOCK = re.compile(r"\{[^{}]*\}")


def _sql_literal(value: str) -> str:
    """A single-quoted SQL literal with embedded quotes doubled.

    Interpolating raw made `Lowe's` produce unparseable SQL — and a
    quote-balanced value like `x' OR TRUE OR '` produce a VALID predicate the
    optimizer then folded away, so a filtered declaration canonicalized to
    `filters=()` at proof grade and would certify against unfiltered code.
    """
    return "'" + value.replace("'", "''") + "'"

# LookML aggregate measure types that wrap `sql` in an aggregate function.
_LOOKML_AGG_TYPES = {"sum", "average", "avg", "min", "max", "count", "count_distinct"}


def _lookml_sql(sql: str) -> str:
    """Resolve LookML substitution operators to bare column references —
    `${TABLE}.amount` → `amount`, `${amount_net}` → `amount_net`."""
    return _LOOKML_FIELD_REF.sub(r"\1", _LOOKML_TABLE_REF.sub("", sql)).strip()


def _lookml_expr(row: dict[str, Any]) -> tuple[str | None, list[str]]:
    """(expression, untranslatable). A LookML measure type this adapter does
    not model must be REFUSED, not passed through.

    Passing the raw `sql` through was the fabrication path: `type: number`
    over other measures turned `${refund_total}` — itself an aggregate — into
    a bare column reference, and `type: median`/`percentile`/`running_total`
    handed a non-aggregate on as if it were a measure. Both reached proof
    grade as definitions nobody wrote.
    """
    raw = row.get("sql")
    if raw is not None and not isinstance(raw, str):
        # str()-coercing a dict produced `sum({'dialect': 'x'})` — a VALID
        # DuckDB struct literal, so an export object canonicalized cleanly at
        # proof grade as a definition nobody wrote.
        return None, [f"sql is not a string ({type(raw).__name__}) — a shape "
                      f"this adapter cannot read"]
    raw_sql = str(raw or "")
    sql = _lookml_sql(raw_sql)
    mtype = str(row.get("type") or "").lower()
    untranslatable: list[str] = []

    # Looker's symmetric aggregate. It de-duplicates by the given key before
    # aggregating; rendering it as a plain SUM asserts the very thing
    # `sql_distinct_key` exists to prevent, and would certify against a naive
    # fan-out-prone model.
    if row.get("sql_distinct_key"):
        untranslatable.append(
            f"sql_distinct_key: {row['sql_distinct_key']} — Looker emits a symmetric "
            f"aggregate here, which is not a plain {mtype or 'aggregate'}()")
        return None, untranslatable

    if mtype == "count":
        # `type: count` counts ROWS and takes no `sql`. Wrapping a column in
        # count() answered a different question, and quietly ignored the
        # Looker semantics when `sql` was absent.
        return "count(*)", untranslatable
    if mtype == "count_distinct":
        if not sql:
            return None, ["type: count_distinct with no sql expression"]
        return f"count(distinct {sql})", untranslatable
    if mtype in _LOOKML_AGG_TYPES:
        if not sql:
            return None, [f"type: {mtype} with no sql expression"]
        return f"{'avg' if mtype == 'average' else mtype}({sql})", untranslatable

    # Everything else — number, median, percentile, running_total,
    # sum_distinct, yesno, string, or an empty/unknown type.
    if not sql:
        return None, [f"measure type {mtype or '(none)'} with no sql expression"]
    return None, [
        f"measure type `{mtype or '(none)'}` is not a modelled aggregate; its sql "
        f"({raw_sql}) may reference other measures and cannot be read as a "
        f"column expression"
    ]


# LookML filter values are a small language, not literals: ranges (`>100`,
# `1 to 10`), wildcards (`%x%`), null tests, inline lists, and relative dates.
# Anything carrying this syntax cannot be rendered as an equality, and
# guessing produces a predicate nobody declared.
#
# Recognizing it takes two rules, not one, because the two halves of the
# language behave differently:

# 1. STRUCTURE. These are operators wherever they appear, whatever the field
#    type: comparisons, list separators, ranges, escapes, and a leading `not`.
#    A comma is always a list (an OR), never a character in a value.
_LOOKML_OPERATOR = re.compile(r"""[<>=!,^\[\]()]|^\s*not\b""", re.IGNORECASE | re.VERBOSE)

#    `to` is the range operator — but only between operands. It is DSL in
#    `1 to 10` and `2024-01-01 to 2024-02-01`, and an ordinary word in
#    `direct to consumer`. The discriminator is a digit on one side of it.
_LOOKML_RANGE = re.compile(r"\d\S*\s+to\b|\bto\s+\S*\d", re.IGNORECASE)

# 2. DATE SHAPE. `to`, `before`, `month` and friends are only date syntax in a
#    value SHAPED like a date expression. Matching the bare words made
#    `channel: "direct to consumer"` and `region: "Tokyo to Osaka"` refuse to
#    translate — the safe direction, but wrong often enough to make the
#    LookML adapter useless. A date expression always leads with a relative
#    keyword, or carries a number+unit or a date literal somewhere in it.
_DATE_UNIT = (r"(?:second|minute|hour|day|week|month|quarter|year|"
              r"fiscal[ _](?:quarter|year))s?")
#    Each keyword is matched with the operand its syntax requires, not bare:
#    `after` is a date operator in `after 2024-01-01` and an ordinary word in
#    `after hours support`.
_DATE_SHAPED = re.compile(
    rf"""^\s*(?:today|yesterday|tomorrow|now)\s*$        # standalone
       # `before`/`after`/`past` take a date operand — a literal, a
       # number+unit, OR another relative keyword. Requiring a DIGIT let
       # `before yesterday` and `after today` through as string equalities
       # against their own text, on a date column.
       | ^\s*(?:before|after|past)\s+
         (?:\S*\d|today|yesterday|tomorrow|now|this|last|next|fy)
       | ^\s*(?:this|last|next)\s+(?:\d+\s*)?{_DATE_UNIT}\b   # this month, last 3 days
       | ^\s*fy\s*\d{{2,4}}\b                            # FY2024
       | ^\s*\d{{4}}\s*$          # a bare year IS a date filter on a date field,
                                  # and nothing here knows the field's type
       | \b\d+\s*{_DATE_UNIT}\b   # 3 days, 12 months ago
       | \d{{4}}[-/]\d{{1,2}}     # a date literal, incl. `2024-01-01 to 2024-02-01`
    """,
    re.IGNORECASE | re.VERBOSE,
)

# 3. NULL / EMPTY tests, which are whole values rather than prefixes. Anchored
#    at both ends: `^NULL` alone also matched the ordinary literal "NULLIFY".
_LOOKML_NULLISH = re.compile(r"^\s*(?:not\s+null|null|empty|blank)\s*$", re.IGNORECASE)

# 4. WILDCARDS. `%` is many characters and `_` is exactly one — Looker string
#    filters are LIKE patterns, not literals. `%` was refused and `_` was not,
#    so `channel: "paid_search"` rendered as `channel = 'paid_search'`: a
#    predicate STRICTER than the declaration, which is the direction that can
#    falsely match code that is also strict. Underscores are common enough in
#    real values that this is not a corner case. Refusing both costs some
#    translations; inventing the wrong one costs a certification.
_LOOKML_WILDCARD = re.compile(r"[%_]")


def _lookml_untranslatable(literal: str) -> str | None:
    """Why this filter value cannot be rendered as a predicate, or None."""
    if not literal.strip():
        return "empty filter value"
    if _LOOKML_NULLISH.match(literal):
        return "null/empty test, not an equality"
    if _LOOKML_OPERATOR.search(literal) or _LOOKML_RANGE.search(literal):
        return "comparison, list or range syntax"
    if _DATE_SHAPED.search(literal):
        return "relative or absolute date expression"
    if _LOOKML_WILDCARD.search(literal):
        return "wildcard pattern (`%` many, `_` one) — not an equality"
    return None


def _lookml_filters(raw: str | None) -> tuple[list[str], list[str]]:
    """`[status: "completed", channel: "-internal"]` → (predicates, untranslatable).

    Only plain literal equality and its negation are translated. A leading `-`
    is LookML negation, and in Looker that INCLUDES rows where the field is
    NULL — rendering it as a bare `<>` silently drops them, making the claim
    stricter than the declaration and able to falsely match a model that also
    drops them.
    """
    if not raw:
        return [], []

    pairs: list[tuple[str, str]] = []
    untranslatable_extra: list[str] = []

    def outside_quotes(position: int) -> bool:
        # The legacy machinery must never fire INSIDE a quoted modern value:
        # `[docs: "use { field: status, value: "x" } form"]` had a legacy-
        # shaped substring extracted from inside the quotes as a real filter,
        # plus the carved-out host mangled into a second predicate — two
        # fabrications, no disclosure.
        return raw.count('"', 0, position) % 2 == 0

    legacy_markers = [m for m in _LOOKML_LEGACY_MARKER.finditer(raw)
                      if outside_quotes(m.start())]
    if legacy_markers:
        # Consume the legacy blocks, then process the REMAINDER with the
        # modern regex. Routing the whole string down one path silently
        # dropped every filter outside the blocks (exporters concatenate a
        # measure's multiple `filters:` parameters).
        consumed = []
        for m in _LOOKML_LEGACY_BLOCK.finditer(raw):
            if outside_quotes(m.start()):
                pairs.append((m.group(1), m.group(2)))
                consumed.append((m.start(), m.end()))
        remainder = raw
        for start, end in reversed(consumed):
            remainder = remainder[:start] + " " + remainder[end:]
        for m in _LOOKML_BRACE_BLOCK.finditer(remainder):
            if m.group(0).strip() and outside_quotes(m.start()):
                untranslatable_extra.append(
                    f"{m.group(0)} (legacy `filters: {{ field: ..., value: ... }}` "
                    f"block in a shape this adapter cannot read)")
        remainder = _LOOKML_BRACE_BLOCK.sub(" ", remainder)
        # An UNBALANCED legacy block — no closing brace, the shape a filters
        # string truncated at an export's VARCHAR limit produces — matched
        # neither regex and fell straight through to the modern parser, which
        # matched the `value: "US"` sub-token: the round-four `value`-column
        # fabrication resurrected through the round-five remainder path, at
        # proof grade, convicting correct code with a top-band CONTRADICTION.
        # A remainder still carrying the marker is unreadable; refuse it.
        if _LOOKML_LEGACY_MARKER.search(remainder):
            untranslatable_extra.append(
                f"{remainder.strip()} (unterminated legacy filters block — "
                f"possibly truncated by the export)")
            remainder = ""
        pairs += _LOOKML_FILTER_RE.findall(remainder)
        if not pairs and not untranslatable_extra:
            return [], [f"{raw} (legacy filters block in a shape this adapter "
                        f"cannot read)"]
    else:
        pairs = _LOOKML_FILTER_RE.findall(raw)
        if not pairs:
            return [], [str(raw)]

    predicates: list[str] = []
    untranslatable: list[str] = list(untranslatable_extra)
    for field_name, value in pairs:
        negated = value.startswith("-")
        literal = value[1:] if negated else value
        if "." in field_name:
            # `users.country` is a field of a JOINED view, not a column of
            # this view's table. Rendering it against the base relation
            # asserts a predicate on a column that is not there.
            untranslatable.append(
                f"{field_name}: {value} (field of another view, not of this measure's table)")
            continue
        reason = _lookml_untranslatable(literal)
        if reason:
            untranslatable.append(f"{field_name}: {value} ({reason})")
        elif negated:
            predicates.append(
                f"({field_name} <> {_sql_literal(literal)} OR {field_name} IS NULL)")
        else:
            predicates.append(f"{field_name} = {_sql_literal(literal)}")
    return predicates, untranslatable


def claims_from_tables(tables: dict[str, list[dict[str, Any]]],
                       path: str | None = None) -> list[DeclaredSpec]:
    """AGENTS.* rows → DeclaredSpecs. Pure function of the table contents so
    every intake mode (JSON, DuckDB, warehouse) produces identical claims."""
    specs: list[DeclaredSpec] = []

    datasets = {str(r.get("name")): r for r in tables.get("osi_dataset", [])}
    for index, row in enumerate(tables.get("osi_metric", [])):
        name = row.get("name")
        if not name or not isinstance(name, str):
            # Silent `continue` dropped falsy-named declarations without a
            # trace, and a dict name became the junk identity "{'nested':...}"
            # — the same silent-drop metrics_yaml was made to refuse.
            raise ValueError(
                f"osi_metric[{index}]: `name` is missing or not a string "
                f"({name!r}) — a nameless declaration cannot be reconciled and "
                f"would be silently dropped")
        dataset = datasets.get(str(row.get("dataset_name") or ""))
        # An OSI metric row may carry qualifiers beyond its expression. Reading
        # only `expression` recorded an UNFILTERED metric for a declaration
        # that filters — a claim narrower than the one written, which will
        # certify against unfiltered code. The LookML branch three lines below
        # already reads its filters; this one silently discarded them.
        osi_untranslatable = [
            f"osi_metric.{field} is declared but not translatable: {row[field]}"
            for field in ("filters", "filter", "dimensions", "time_grain",
                          "window", "non_additive_dimension")
            if row.get(field)
        ]
        # Third-party JSON: `expression`/`description` need not be strings. A
        # dict there reached SQLite as a bind parameter and killed the whole
        # scan mid-write with a raw type error — the exact defect fixed for
        # metrics_yaml scalars, never applied here. A non-string expression is
        # a shape this adapter does not read; refuse it, do not crash on it.
        expr = row.get("expression")
        if expr is not None and not isinstance(expr, str):
            osi_untranslatable.append(
                f"expression is not a string ({type(expr).__name__}) — a shape "
                f"this adapter cannot read")
            expr = None
        description = row.get("description") or row.get("ai_context")
        if description is not None and not isinstance(description, str):
            description = str(description)
        specs.append(DeclaredSpec(
            system="agents_osi",
            name=str(row["name"]),
            description=description,
            # OSI metrics carry no model unless the export resolves the
            # dataset; without one the claim is honestly inexpressible.
            model=str(dataset["source_table"]) if dataset and dataset.get("source_table") else None,
            expr=expr,
            untranslatable=osi_untranslatable,
            path=path,
        ))

    views = {str(r.get("name")): r for r in tables.get("lookml_view", [])}
    for index, row in enumerate(tables.get("lookml_measure", [])):
        m_name = row.get("measure_name")
        if not m_name or not isinstance(m_name, str):
            raise ValueError(
                f"lookml_measure[{index}]: `measure_name` is missing or not a "
                f"string ({m_name!r}) — a nameless declaration cannot be "
                f"reconciled and would be silently dropped")
        view = views.get(str(row.get("view_name") or ""))
        model = str(view["sql_table_name"]) if view and view.get("sql_table_name") else None
        raw_filters = row.get("filters")
        filters, untranslatable = _lookml_filters(
            raw_filters if isinstance(raw_filters, str) or raw_filters is None
            else str(raw_filters)
        )
        expr, expr_untranslatable = _lookml_expr(row)
        specs.append(DeclaredSpec(
            system="agents_lookml",
            name=str(row["measure_name"]),
            description=row.get("description") or row.get("ai_context"),
            model=model,
            expr=expr,
            filters=filters,
            untranslatable=untranslatable + expr_untranslatable,
            path=path,
        ))
    return specs


def load_agents_schema(path: str | Path) -> list[DeclaredSpec]:
    """Load claims from an Agents Schema export: `.json` dump or a DuckDB
    file containing the AGENTS schema."""
    p = Path(path)
    if p.suffix.lower() == ".json":
        data = load_json(p)
        # A KNOWN table whose value is not a list of rows is a malformed
        # export, and filtering it out silently dropped every declaration in
        # it — the estate looked smaller than it is. Unknown keys still pass
        # through quietly (providers publish extras).
        malformed = [k for k, v in data.items()
                     if k.lower() in AGENTS_TABLES and not isinstance(v, list)]
        if malformed:
            raise ValueError(
                f"{p}: table(s) {', '.join(sorted(malformed))} are not lists of "
                f"rows — this export is malformed and its declarations would be "
                f"silently lost")
        tables = {k.lower(): v for k, v in data.items() if isinstance(v, list)}
        return claims_from_tables(tables, path=str(p))

    try:
        import duckdb
    except ImportError as e:
        raise ValueError(
            f"{p}: reading a DuckDB Agents Schema needs `pip install duckdb` "
            "(or pass a .json export)") from e
    con = duckdb.connect(str(p), read_only=True)
    # Fully qualify with the catalog: a file named agents.duckdb makes the bare
    # `agents.<table>` reference ambiguous between catalog and schema.
    catalog = con.execute("SELECT current_database()").fetchone()[0]
    tables: dict[str, list[dict[str, Any]]] = {}
    for table in AGENTS_TABLES:
        try:
            cur = con.execute(f'SELECT * FROM "{catalog}".agents.{table}')
        except Exception:  # noqa: BLE001 — providers publish subsets; absent is normal
            continue
        columns = [d[0].lower() for d in cur.description]
        tables[table] = [dict(zip(columns, r)) for r in cur.fetchall()]
    con.close()
    if not tables:
        # A DuckDB file with none of the AGENTS claim tables yielded 0 claims
        # and exit 0 — every metric then read as a SHADOW_METRIC. A wrong
        # file must never look like an estate with no declarations.
        raise ValueError(
            f"{p}: no AGENTS schema tables found "
            f"({', '.join(AGENTS_TABLES)}) — is this the right warehouse file?")
    return claims_from_tables(tables, path=str(p))
