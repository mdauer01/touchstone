"""Parser-disagreement detector: does sqlglot read the SQL the way DuckDB does?

Round seventeen found the PARSE itself disagreeing with the engines: sqlglot
gives `%` the precedence of `+`, and binds IS / IS DISTINCT FROM / NOT across
an unparenthesized comparison differently from DuckDB and Spark. The proof
path's own round-trip check compares sqlglot only with itself, so it cannot
see that. This module asks the engine.

How: starting from the ORIGINAL SQL, before any optimization or
transformation, sqlglot's parse tree is rendered FULLY parenthesized (every
operator wrapped, post-order), so the rendering states sqlglot's binding
explicitly. DuckDB serializes both the original and that rendering
(`json_serialize_sql`); if the two trees differ, DuckDB reads the original
differently from sqlglot — a disagreement.

What it is, and is not:

* a DETECTOR of disagreement, for the DuckDB dialect only. Agreement says
  nothing about name binding, types, the transformations applied after
  parsing, or any other warehouse's grammar; a DuckDB result is no
  qualification for Databricks or Snowflake;
* `not_checked` whenever it cannot answer — another dialect, DuckDB not
  installed, input either parser rejects, more than one statement. Never
  agreement by default;
* comparisons are structural: query locations and operator-vs-call spelling
  (`%` / MOD(), `^` / POWER()) are ignored, constants are
  compared as constants (sqlglot's rendering normalizes literals, e.g.
  'month' to 'MONTH'), and DuckDB's spellings of one negated predicate
  (`NOT x IS NULL` / `x IS NOT NULL`) are identified. DuckDB's serialization
  itself can normalize syntax, so a disagreement it hides stays hidden;
* a disagreement is evidence to look at, not a verdict: a function the
  rendering renames, or a literal it rewrites (INTERVAL '1 day'), can be
  flagged although both readings agree. It errs toward `disagrees`;
* passing it cannot re-enable positive verdicts (docs/proof-policy.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

ENGINE = "duckdb"

DISAGREES = "disagrees"
NO_DISAGREEMENT_FOUND = "no_disagreement_found"
NOT_CHECKED = "not_checked"


@dataclass(frozen=True)
class ParseCheck:
    status: str
    dialect: str
    engine: str
    engine_version: str | None
    sqlglot_version: str
    detail: str | None = None

    def as_dict(self) -> dict:
        return {"status": self.status, "dialect": self.dialect, "engine": self.engine,
                "engine_version": self.engine_version,
                "sqlglot_version": self.sqlglot_version, "detail": self.detail}


def _versions() -> tuple[str | None, str]:
    try:
        import duckdb
        engine = duckdb.__version__
    except ImportError:
        engine = None
    return engine, sqlglot.__version__


def check_parse(sql: str, dialect: str) -> ParseCheck:
    """Whether DuckDB and sqlglot read `sql` (in `dialect`) the same way."""
    engine_version, sg_version = _versions()

    def result(status: str, detail: str | None = None) -> ParseCheck:
        return ParseCheck(status, dialect, ENGINE, engine_version, sg_version, detail)

    if dialect != ENGINE:
        return result(NOT_CHECKED, f"no native parser for {dialect} in this check — a "
                                   f"DuckDB result qualifies nothing about {dialect}")
    if engine_version is None:
        return result(NOT_CHECKED, "duckdb is not installed")
    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except Exception as e:  # noqa: BLE001 — arbitrary input
        return result(NOT_CHECKED, f"sqlglot could not parse it: {e}")
    if len(trees) != 1:
        return result(NOT_CHECKED, f"{len(trees)} statements — one is checked at a time")
    try:
        rendered = _fully_parenthesized(trees[0].copy()).sql(dialect=dialect)
    except Exception as e:  # noqa: BLE001
        return result(NOT_CHECKED, f"sqlglot could not render it: {e}")
    original, err = _engine_tree(sql)
    if original is None:
        return result(NOT_CHECKED, f"DuckDB could not parse it: {err}")
    explicit, err = _engine_tree(rendered)
    if explicit is None:
        return result(NOT_CHECKED, f"DuckDB could not parse sqlglot's reading ({rendered}): "
                                   f"{err}")
    if original != explicit:
        return result(DISAGREES, f"DuckDB reads it differently from sqlglot's reading "
                                 f"{rendered}")
    return result(NO_DISAGREEMENT_FOUND)


# Operators whose binding a full parenthesization makes explicit. A predicate
# directly under NOT is left to the NOT (`x IS NOT NULL` renders as one).
_OPERATORS = (exp.Binary, exp.Unary, exp.Predicate, exp.Not, exp.Neg, exp.Between,
              exp.In, exp.Is)
_LEAVE = (exp.Paren, exp.Alias, exp.Column, exp.Dot, exp.Escape)


def _fully_parenthesized(tree: exp.Expression) -> exp.Expression:
    def walk(node: exp.Expression) -> exp.Expression:
        for key, value in list(node.args.items()):
            if isinstance(value, exp.Expression):
                node.set(key, walk(value))
            elif isinstance(value, list):
                node.set(key, [walk(v) if isinstance(v, exp.Expression) else v
                               for v in value])
        if isinstance(node, _OPERATORS) and not isinstance(node, _LEAVE) \
                and not isinstance(node.parent, exp.Not):
            return exp.Paren(this=node)
        return node
    return walk(tree)


_CON = None


def _connection():
    global _CON
    if _CON is None:
        import duckdb
        _CON = duckdb.connect()
    return _CON


# One negated predicate, two DuckDB spellings: NOT over the predicate, or the
# negated operator itself. Identified so a rendering choice is not a finding.
_NEGATED = {("OPERATOR", "OPERATOR_IS_NULL"): "OPERATOR_IS_NOT_NULL",
            ("OPERATOR", "OPERATOR_IS_NOT_NULL"): "OPERATOR_IS_NULL",
            ("OPERATOR", "COMPARE_IN"): "COMPARE_NOT_IN",
            ("OPERATOR", "COMPARE_NOT_IN"): "COMPARE_IN",
            ("COMPARISON", "COMPARE_DISTINCT_FROM"): "COMPARE_NOT_DISTINCT_FROM",
            ("COMPARISON", "COMPARE_NOT_DISTINCT_FROM"): "COMPARE_DISTINCT_FROM"}
_NEGATED_FUNCTIONS = {"~~": "!~~", "!~~": "~~", "~~*": "!~~*", "!~~*": "~~*",
                      "~~~": "!~~~", "!~~~": "~~~"}


def _engine_tree(sql: str) -> tuple[object | None, str | None]:
    try:
        raw = _connection().execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001 — the engine refused it
        return None, str(e).splitlines()[0][:200]
    if data.get("error"):
        return None, str(data.get("error_message", "error"))[:200]
    return _normalize(data), None


# One function, several DuckDB spellings (sqlglot renders `^` as POWER and
# MOD() as `%`): identified, so only the STRUCTURE of the reading is compared.
_FUNCTION_ALIASES = {"^": "power", "**": "power", "pow": "power", "mod": "%",
                     "ceiling": "ceil", "len": "length", "char_length": "length",
                     "character_length": "length", "substr": "substring",
                     "ucase": "upper", "lcase": "lower"}


def _normalize(node):
    if isinstance(node, list):
        return [_normalize(v) for v in node]
    if not isinstance(node, dict):
        return node
    if node.get("class") == "CONSTANT":
        return {"class": "CONSTANT"}
    if node.get("class") == "FUNCTION" and isinstance(node.get("function_name"), str):
        name = node["function_name"].lower()
        # DuckDB 1.x lists operands as `children`; 2.0 as `arguments` of
        # {name, expression}, and repeats the name in `qualified_name`.
        operands = node.get("children") or [a.get("expression")
                                             for a in node.get("arguments") or []
                                             if isinstance(a, dict)]
        if name == "+" and len(operands) == 1:
            return _normalize(operands[0])             # unary plus is the operand
        node = {k: v for k, v in node.items() if k != "qualified_name"}
        node["function_name"] = _FUNCTION_ALIASES.get(name, name)
        # TRIM(LEADING …) resolves into the main schema / system catalog.
        if node.get("schema") in ("", "main"):
            node.pop("schema")
        if node.get("catalog") in ("", "system"):
            node.pop("catalog")
    if node.get("type") == "OPERATOR_NOT" and len(node.get("children") or []) == 1:
        child = node["children"][0]
        negated = _NEGATED.get((child.get("class"), child.get("type")))
        if negated is not None:
            return _normalize({**child, "type": negated})
        if child.get("class") == "FUNCTION" and child.get("function_name") in _NEGATED_FUNCTIONS:
            return _normalize({**child,
                               "function_name": _NEGATED_FUNCTIONS[child["function_name"]]})
    # Where it was written (query_location, and DuckDB 2.0's
    # query_location_length), and whether as an operator or a call, is spelling.
    return {k: _normalize(v) for k, v in node.items()
            if not k.startswith("query_location") and k != "is_operator"}
