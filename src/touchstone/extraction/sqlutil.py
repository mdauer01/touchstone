"""Shared AST helpers used by canonicalization, fingerprinting and the registry."""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp


_GROUPING_KINDS: tuple[tuple[type, str], ...] = (
    (exp.Rollup, "rollup"), (exp.Cube, "cube"), (exp.GroupingSets, "grouping_sets"))


def split_group(group: exp.Group) -> tuple[list[exp.Expression], list[tuple[str, exp.Expression]]]:
    """(plain grouping items, [(kind, construct node)]) for one GROUP BY.

    The AST shape changed between sqlglot releases: up to 30.12 ROLLUP, CUBE
    and GROUPING SETS lived in dedicated args (`group.args["rollup"]`); from
    the 30.2x line they are nodes inside `group.expressions`. Every site that
    read the dedicated args directly went blind under the newer parser, and
    ordinals inside ROLLUP(1, 2) stopped resolving — two different grouping
    definitions then hashed identically at proof grade. This is the single
    reader every site must use; it accepts both shapes.
    """
    plain: list[exp.Expression] = []
    constructs: list[tuple[str, exp.Expression]] = []
    for item in group.expressions:
        kind = next((k for cls, k in _GROUPING_KINDS if isinstance(item, cls)), None)
        if kind is None:
            plain.append(item)
        else:
            constructs.append((kind, item))
    for _, kind in _GROUPING_KINDS:
        for node in group.args.get(kind) or []:
            if isinstance(node, exp.Expression):
                constructs.append((kind, node))
    return plain, constructs


def render(node: exp.Expression, dialect: str) -> str:
    """The one true canonical text form. Every comparison in the system goes
    through this function so that equality is equality of this rendering."""
    return node.sql(dialect=dialect, comments=False, normalize_functions="upper")


def unwrap_select(ast: exp.Expression | None) -> exp.Select | None:
    node = ast
    while isinstance(node, exp.Subquery):
        node = node.this
    return node if isinstance(node, exp.Select) else None


def table_fqname(table: exp.Table) -> str:
    """The relation a Table node names, WITHOUT the `_` placeholder that
    canonicalize._pin_short_references adds for qualification. The pin is a
    resolution device; letting it leak into join nodes and lineage made two
    byte-identical models — one canonicalized before a full-depth model was
    registered, one after — read as `raw.orders` vs `_.raw.orders` and
    CONTRADICT each other over a relation that does not exist."""
    parts = [table.args.get("catalog"), table.args.get("db"), table.this]
    return ".".join(p.name for p in parts if p is not None and p.name != "_")


def scope_sources(select: exp.Select) -> list[exp.Expression]:
    """FROM + JOIN sources of this select only (not nested scopes)."""
    sources: list[exp.Expression] = []
    # sqlglot renamed the arg key "from" -> "from_" around v30; accept both.
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is not None:
        sources.append(from_.this)
    for join in select.args.get("joins") or []:
        sources.append(join.this)
    return sources


def table_map(select: exp.Select) -> dict[str, str]:
    """Map of alias-or-name -> fully qualified relation for this select's scope.

    Derived tables map to a pseudo relation so lineage degrades visibly
    instead of silently.
    """
    mapping: dict[str, str] = {}
    for source in scope_sources(select):
        if isinstance(source, exp.Table):
            mapping[source.alias_or_name] = table_fqname(source)
        elif isinstance(source, (exp.Subquery, exp.CTE)):
            name = source.alias_or_name or "_subquery"
            mapping[name] = f"<derived:{name}>"
    return mapping


def requalify_columns(node: exp.Expression, table: str | None,
                      only_qualified: bool = True) -> None:
    """Rewrite column qualifiers in place to `table` (or strip them when
    None). Adapters use it to bind a declaration's own naming (`source.`, a
    dataset alias) to the relation the synthesized claim reads FROM; the
    emitter uses it in reverse. With only_qualified, bare columns are left
    alone."""
    for col in node.find_all(exp.Column):
        if only_qualified and not col.table:
            continue
        col.set("catalog", None)
        col.set("db", None)
        col.set("table", exp.to_identifier(table) if table else None)


def literal_kind(node: exp.Expression) -> str:
    """'number' | 'string' | 'other' — what the engines type a BETWEEN bound
    as: DuckDB binds all three operands to ONE common type, so bounds of
    different kinds are cast differently than the pairwise expansion."""
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal) \
            and not node.this.is_string:
        return "number"
    if isinstance(node, exp.Literal):
        return "string" if node.is_string else "number"
    return "other"


def flatten_conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    if isinstance(node, exp.And):
        return list(node.flatten())
    return [node]


_TRUNCATIONS = (exp.DateTrunc, exp.TimestampTrunc, exp.DatetimeTrunc)

# The unit spellings each engine's truncation ACCEPTS, as written (compared
# without case, never trimmed), and the grain each one is. An engine returns
# NULL for, or rejects, any other spelling — so the round-eighteen union of
# every engine's synonyms read `DATE_TRUNC('WEEKS', d)` (NULL on Databricks:
# one grand-total group) as the governed weekly grain. Sources: the
# Databricks SQL reference for date_trunc and trunc (Spark's are the same
# lists); DuckDB 1.5.4 by execution (tests/test_time_groupings.py re-checks
# it); Snowflake's "supported date and time parts" (its documented form is a
# quoted part); BigQuery's DATE_TRUNC/TIMESTAMP_TRUNC granularities, which
# are unquoted KEYWORDS (its WEEK starts on Sunday — the engine's week). A
# dialect with no table here gets only the full singular names, quoted.
UNITS = ("YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "HOUR", "MINUTE", "SECOND",
         "MILLISECOND", "MICROSECOND")
_SPARK_DATE_TRUNC = {"YEAR": "YEAR", "YYYY": "YEAR", "YY": "YEAR", "QUARTER": "QUARTER",
                     "MONTH": "MONTH", "MM": "MONTH", "MON": "MONTH", "WEEK": "WEEK",
                     "DAY": "DAY", "DD": "DAY", "HOUR": "HOUR", "MINUTE": "MINUTE",
                     "SECOND": "SECOND", "MILLISECOND": "MILLISECOND",
                     "MICROSECOND": "MICROSECOND"}
# trunc(date, unit) stops at WEEK: TRUNC(d, 'DD') is NULL.
_SPARK_TRUNC = {k: v for k, v in _SPARK_DATE_TRUNC.items()
                if v in ("YEAR", "QUARTER", "MONTH", "WEEK")}
_DUCKDB_DATE_TRUNC = {
    **{s: "YEAR" for s in ("YEAR", "YEARS", "Y", "YR", "YRS")},
    **{s: "QUARTER" for s in ("QUARTER", "QUARTERS")},
    **{s: "MONTH" for s in ("MONTH", "MONTHS", "MON", "MONS")},
    **{s: "WEEK" for s in ("WEEK", "WEEKS", "W", "WEEKOFYEAR", "YEARWEEK")},
    **{s: "DAY" for s in ("DAY", "DAYS", "D", "DAYOFMONTH", "DOW", "DOY", "JULIAN", "ISODOW",
                          "WEEKDAY")},
    **{s: "HOUR" for s in ("HOUR", "HOURS", "H", "HR", "HRS")},
    **{s: "MINUTE" for s in ("MINUTE", "MINUTES", "MIN", "MINS", "M")},
    **{s: "SECOND" for s in ("SECOND", "SECONDS", "S", "SEC", "SECS")},
    **{s: "MILLISECOND" for s in ("MILLISECOND", "MILLISECONDS", "MS", "MSEC", "MSECS")},
    **{s: "MICROSECOND" for s in ("MICROSECOND", "MICROSECONDS", "US", "USEC", "USECS")}}
_SNOWFLAKE_DATE_TRUNC = {
    **{s: "YEAR" for s in ("YEAR", "Y", "YY", "YYY", "YYYY", "YR", "YEARS", "YRS")},
    **{s: "QUARTER" for s in ("QUARTER", "Q", "QTR", "QTRS", "QUARTERS")},
    **{s: "MONTH" for s in ("MONTH", "MM", "MON", "MONS", "MONTHS")},
    **{s: "WEEK" for s in ("WEEK", "W", "WK", "WEEKOFYEAR", "WOY", "WY")},
    **{s: "DAY" for s in ("DAY", "D", "DD", "DAYS", "DAYOFMONTH")},
    **{s: "HOUR" for s in ("HOUR", "H", "HH", "HR", "HOURS", "HRS")},
    **{s: "MINUTE" for s in ("MINUTE", "M", "MI", "MIN", "MINUTES", "MINS")},
    **{s: "SECOND" for s in ("SECOND", "S", "SEC", "SECONDS", "SECS")},
    **{s: "MILLISECOND" for s in ("MILLISECOND", "MS", "MSEC", "MILLISECONDS")},
    **{s: "MICROSECOND" for s in ("MICROSECOND", "US", "USEC", "MICROSECONDS")}}
_BIGQUERY_TRUNC = {u: u for u in ("YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "HOUR", "MINUTE",
                                  "SECOND", "MILLISECOND", "MICROSECOND")}
_DEFAULT_UNITS = {u: u for u in UNITS[:8]}

def _token_types():
    from sqlglot.tokens import TokenType

    # How a unit argument is written: a parameter marker, or one word.
    return ({TokenType.PLACEHOLDER, TokenType.PARAMETER, TokenType.COLON},
            {TokenType.VAR, TokenType.IDENTIFIER})


_PARAMETER_TOKENS, _WORD_TOKENS = _token_types()

# Units the engine documents (or accepts) that are no grain this rule set
# models — refused as that, not as invalid SQL.
_UNMODELLED = {"bigquery": frozenset({"ISOWEEK", "ISOYEAR", "WEEK"}),   # WEEK(<weekday>)
               "duckdb": frozenset({"DECADE", "CENTURY", "MILLENNIUM", "ISOYEAR", "EPOCH",
                                    "DAYOFWEEK", "DAYOFYEAR"}),
               "snowflake": frozenset({"NANOSECOND", "NS", "NSEC", "NANOSEC", "NSECOND",
                                       "NANOSECONDS", "NANOSECS", "NSECONDS"})}
# dialect -> (DATE_TRUNC-family table, TRUNC(d, unit) table, how a unit is written)
_UNIT_TABLES = {"databricks": (_SPARK_DATE_TRUNC, _SPARK_TRUNC, "string"),
                "spark": (_SPARK_DATE_TRUNC, _SPARK_TRUNC, "string"),
                "duckdb": (_DUCKDB_DATE_TRUNC, {}, "string"),
                # TRUNC(date, part) takes the same date parts
                "snowflake": (_SNOWFLAKE_DATE_TRUNC, _SNOWFLAKE_DATE_TRUNC, "string"),
                "bigquery": (_BIGQUERY_TRUNC, {}, "keyword")}


def canonical_unit(unit: str, dialect: str, function: str = "DATE_TRUNC") -> str | None:
    """The grain `unit` is, as `function` reads it in `dialect`; None when
    that engine does not accept this spelling. Not trimmed: `' month '` is
    no unit to DuckDB, nor (NULL) to Databricks."""
    date_trunc, trunc, _ = _UNIT_TABLES.get(dialect, (_DEFAULT_UNITS, {}, "string"))
    table = trunc if function.upper() == "TRUNC" else date_trunc
    return table.get(unit.upper())


def annotate_truncations(tree: exp.Expression | None, sql: str, dialect: str) -> None:
    """Record, on every truncation in `tree`, the grain its unit is AS WRITTEN
    in `sql` (the text `tree` was parsed from). The parser rewrites units —
    'W' and 'M' become WEEK and MINUTE, an unquoted `month` becomes the
    MONTH keyword although the engines read a column of that name — so the
    tree alone cannot say what the engine truncates to. A unit that is not
    one quoted string this engine accepts is recorded as no grain at all."""
    if tree is None:
        return
    nodes = [n for n in tree.find_all(*_TRUNCATIONS) if "start" in n.meta]
    if not nodes:
        return
    import sqlglot
    from sqlglot.tokens import TokenType

    try:
        tokens = sqlglot.tokenize(sql, read=dialect)
    except Exception:  # noqa: BLE001 — the parser accepted it; record nothing as known
        tokens = []
    at = {t.start: i for i, t in enumerate(tokens)}
    for node in nodes:
        node.meta["touchstone_unit"] = None
        target = node.this.find(exp.Identifier) if isinstance(node.this, exp.Column) else None
        where = target.meta.get("start") if target is not None else None
        i = at.get(node.meta["start"])
        if i is None or i + 1 >= len(tokens) or tokens[i + 1].token_type != TokenType.L_PAREN:
            continue
        args = call_arguments(tokens, i + 1)
        if len(args) != 2:
            continue
        if where is not None:
            unit_args = [a for a in args if not any(t.start == where for t in a)]
        else:
            # Not a bare column (`DATE_TRUNC('Y', CURRENT_DATE())`): the unit
            # is where the function puts it — first for DATE_TRUNC, second
            # for TRUNC and for BigQuery's (value, granularity) order.
            second = tokens[i].text.upper() == "TRUNC" or dialect == "bigquery"
            unit_args = [args[1] if second else args[0]]
        if len(unit_args) != 1:
            continue
        unit = unit_args[0]
        node.meta["touchstone_unit_written"] = sql[unit[0].start:unit[-1].end + 1]
        node.meta["touchstone_function"] = tokens[i].text.upper()
        types = {t.token_type for t in unit}
        node.meta["touchstone_unit_form"] = (
            "parameter" if types & _PARAMETER_TOKENS else
            "string" if len(unit) == 1 and unit[0].token_type == TokenType.STRING else
            "keyword" if len(unit) == 1 and unit[0].token_type in _WORD_TOKENS else "expression")
        form = _UNIT_TABLES.get(dialect, (None, None, "string"))[2]
        if node.meta["touchstone_unit_form"] == form:
            node.meta["touchstone_unit"] = canonical_unit(unit[0].text, dialect, tokens[i].text)


def unaccepted_truncation(tree: exp.Expression, dialect: str) -> str | None:
    """Why a truncation in `tree` (annotated) is no grain this rule set
    reads, or None: its unit, as written, is not one spelling this engine's
    function accepts for a modelled grain. The canonical form re-renders the
    parser's unit, so a judgment made on it would read 'W' as WEEK. The
    reason says only what is so — a parameter, a documented grain this rule
    set does not model, the wrong form for the dialect, an unknown spelling."""
    for node in tree.find_all(*_TRUNCATIONS):
        if "touchstone_unit" not in node.meta or node.meta["touchstone_unit"] is not None:
            continue
        what = f"truncates {node.this.sql(dialect)}"
        written = node.meta.get("touchstone_unit_written")
        if written is None:
            return f"{what} with a unit that could not be read as written"
        form = node.meta.get("touchstone_unit_form")
        if form == "parameter":
            return f"{what} by a parameter ({written}): which grain it truncates to is not known"
        if dialect not in _UNIT_TABLES:
            return (f"{what} by {written}, a unit spelling this rule set has no verified "
                    f"reading of for {dialect}")
        expected = _UNIT_TABLES[dialect][2]
        bare = written.strip("'\"").upper()
        if bare.split("(")[0].strip() in _UNMODELLED.get(dialect, ()):
            return (f"{what} by {written}, a {dialect} unit that is not one of the grains this "
                    f"rule set models")
        if form != expected:
            return f"{what} by {written}: " + {
                ("databricks", "keyword"): "an unquoted unit names a column there",
                ("spark", "keyword"): "an unquoted unit names a column there",
                ("duckdb", "keyword"): "an unquoted unit names a column there",
                ("snowflake", "keyword"): "snowflake documents date parts as quoted strings, and "
                                          "this rule set reads only that form",
                ("bigquery", "string"): "bigquery writes granularities as unquoted keywords",
            }.get((dialect, form), "a form this rule set does not read for " + dialect)
        return f"{what} by {written}, " + {
            "databricks": "which databricks does not accept for this function: it returns NULL",
            "spark": "which spark does not accept for this function: it returns NULL",
            "duckdb": "which duckdb does not accept",
        }.get(dialect, f"which is not a {dialect} unit spelling this rule set reads")
    return None


def misread_literal(sql: str, dialect: str) -> str | None:
    """Why the parser's reading of a typed literal in `sql` is not the
    engine's, or None. sqlglot reads Databricks' `1.05BD` as CAST(1.05 AS
    DECIMAL) and renders it so — and Databricks reads a bare DECIMAL as
    DECIMAL(10,0): the rendering computes 1, the statement 1.05."""
    import sqlglot
    from sqlglot.tokens import TokenType

    if dialect not in ("databricks", "spark"):
        return None
    try:
        tokens = sqlglot.tokenize(sql, read=dialect)
    except Exception:  # noqa: BLE001
        return None
    for i, tok in enumerate(tokens[2:], start=2):
        if tok.text.upper() == "BD" and tokens[i - 1].token_type == TokenType.DCOLON \
                and tokens[i - 2].token_type == TokenType.NUMBER:
            return (f"uses the DECIMAL literal {tokens[i - 2].text}BD, which the parser reads "
                    f"as CAST({tokens[i - 2].text} AS DECIMAL) — DECIMAL(10,0) on {dialect}")
    return None


def call_arguments(tokens, open_index: int) -> list[list]:
    """The top-level argument token lists of the call opening at `open_index`."""
    from sqlglot.tokens import TokenType

    args: list[list] = [[]]
    depth = 0
    for tok in tokens[open_index:]:
        if tok.token_type in (TokenType.L_PAREN, TokenType.L_BRACKET):
            depth += 1
            if depth == 1:
                continue
        elif tok.token_type in (TokenType.R_PAREN, TokenType.R_BRACKET):
            depth -= 1
            if depth == 0:
                break
        elif tok.token_type == TokenType.COMMA and depth == 1:
            args.append([])
            continue
        args[-1].append(tok)
    return [a for a in args if a]


def truncation_of(node: exp.Expression) -> tuple[str, exp.Column] | None:
    """(UNIT, column) when `node` is DATE_TRUNC('<unit>', <bare column>);
    None for anything else. A truncation of an expression is not one. The
    unit is the grain the engine reads from the unit AS WRITTEN
    (`annotate_truncations`; `MM` is MONTH on Databricks, NULL on DuckDB);
    a node parsed without its text falls back to the parsed unit, and only
    to a full singular unit name."""
    if not isinstance(node, _TRUNCATIONS):
        return None
    target = node.this
    if not isinstance(target, exp.Column):
        return None
    if "touchstone_unit" in node.meta:
        unit = node.meta["touchstone_unit"]
        return (unit, target) if unit else None
    unit = node.args.get("unit")
    if isinstance(unit, (exp.Var, exp.Literal)) and unit.name.upper() in UNITS:
        return unit.name.upper(), target
    return None


def faithful_render(node: exp.Expression, dialect: str) -> str | None:
    """`node` rendered in `dialect`, or None when the rendering does not parse
    back to the same tree. The Spark/Databricks generator HOISTS a derived
    table's own WITH to the top of the statement, which changes what its names
    resolve to; round seventeen found the monitor and the view-dump intake
    judging that rewritten text instead of what ran."""
    import sqlglot

    text = node.sql(dialect=dialect)
    try:
        return text if sqlglot.parse_one(text, read=dialect) == node else None
    except Exception:  # noqa: BLE001 — a rendering that does not parse is not faithful
        return None


def statement_chunks(text: str, dialect: str) -> list[tuple[str, exp.Expression | None]]:
    """(text, tree) for every statement in a history blob, in order; the tree
    None for one that does not parse, so what came before what is never lost.

    Statements are split on semicolons OUTSIDE strings and comments, using
    the dialect's tokenizer, and each is parsed on its own. The previous
    fallback split the raw text on `;`, so a semicolon inside a literal or a
    comment split one statement into fragments — a valid statement was lost
    and double-counted as unparseable, a comment fragment parsed as a
    "non-query statement", and a SELECT embedded in an INSERT's string
    literal was counted as an EXECUTION. Counts here are per statement the
    operator actually ran.

    A comment on the SAME line after a statement's semicolon belongs to that
    statement (`SELECT …; -- touchstone: metric=gross_revenue`): attached to the
    next one, it named the wrong query.

    A dollar-quoted body (`CREATE VIEW … WITH METRICS LANGUAGE YAML AS $$ … $$`,
    a SQL function's body) is one string to the engine: its semicolons, quotes
    and dashes do not end or start anything. It is masked before the semicolons
    are found, and the statement keeps its original text. A body that is never
    closed is one statement that cannot be read.
    """
    from sqlglot.tokens import TokenType

    masked, unclosed = _mask_dollar_bodies(text)
    try:
        tokens = sqlglot.tokenize(masked, read=dialect)
    except Exception:  # noqa: BLE001 — a blob the tokenizer rejects outright
        whole, bad = _parse_whole(text, dialect)
        return [(t.meta.get("source_text") or text.strip(), t) for t in whole] + \
            [(text.strip(), None)] * bad
    # (as it ran, as parsed): the same statement, its dollar-quoted body blanked
    # for the parser, which would otherwise read the body's quotes as SQL.
    chunks: list[tuple[str, str]] = []
    start = 0
    for tok in tokens:
        if tok.token_type == TokenType.SEMICOLON:
            if tok.start < start:
                continue   # consumed with a trailing comment
            chunk, shown = text[start:tok.start], masked[start:tok.start]
            after = tok.end + 1
            eol = text.find("\n", after)
            line = text[after: eol if eol >= 0 else len(text)]
            tail = line.strip()
            if tail and (tail.startswith("--") or (tail.startswith("/*") and tail.endswith("*/")
                                                  and "*/" not in tail[2:-2])):
                chunk, shown = f"{chunk} {tail}", f"{shown} {tail}"
                after = eol if eol >= 0 else len(text)
            chunks.append((chunk, shown))
            start = after
    chunks.append((text[start:], masked[start:]))

    statements: list[tuple[str, exp.Expression | None]] = []
    for n, (chunk, shown) in enumerate(chunks):
        if unclosed is not None and n == len(chunks) - 1:
            statements.append((chunk.strip(), None))   # a body that is never closed
            continue
        if not _has_code(shown, dialect):
            continue  # whitespace or comments only — nothing was executed
        try:
            parsed = sqlglot.parse_one(shown, read=dialect)
        except Exception:  # noqa: BLE001
            statements.append((chunk.strip(), None))
            continue
        if parsed is None or isinstance(parsed, exp.Semicolon):
            continue
        annotate_truncations(parsed, shown, dialect)
        # The text as it ran, for evidence records (assess.record): the tree
        # is qualified in place later, and a re-render is not always faithful.
        parsed.meta["source_text"] = chunk.strip()
        statements.append((chunk.strip(), parsed))
    return statements


_DOLLAR = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def _mask_dollar_bodies(text: str) -> tuple[str, int | None]:
    """`text` with every dollar-quoted body blanked (newlines kept, so offsets
    and lines do not move), and the offset of a body that is never closed.
    A `$$` inside a quoted string or a comment opens nothing."""
    out, i, n = list(text), 0, len(text)
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            i = j + 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == "$" and (m := _DOLLAR.match(text, i)):
            close = text.find(m.group(0), m.end())
            end = n if close < 0 else close
            for k in range(m.end(), end):
                if out[k] != "\n":
                    out[k] = " "
            if close < 0:
                return "".join(out), i
            i = close + len(m.group(0))
        else:
            i += 1
    return "".join(out), None


def _has_code(chunk: str, dialect: str) -> bool:
    """True when a chunk holds anything besides whitespace and comments."""
    if not chunk.strip():
        return False
    try:
        from sqlglot.tokens import TokenType
        return any(t.token_type != TokenType.SEMICOLON
                   for t in sqlglot.tokenize(chunk, read=dialect))
    except Exception:  # noqa: BLE001 — let the parser report it
        return True


def _parse_whole(text: str, dialect: str) -> tuple[list[exp.Expression], int]:
    try:
        trees = [s for s in sqlglot.parse(text, read=dialect) if s is not None]
    except Exception:  # noqa: BLE001
        return [], 1
    for tree in trees:
        annotate_truncations(tree, text, dialect)
    return trees, 0
