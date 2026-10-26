"""Round seventeen: confirmed intake and interpretation defects, kept as
regressions (refusal first — the grammar is frozen).

Three independent reviews of 1dd041d reproduced, by DuckDB execution, false
equalities and proof forms that did not mean what the original meant. Some
were errors in the PARSE itself (sqlglot reads `%` at the precedence of `+`,
and binds IS / IS DISTINCT FROM / NOT across an unparenthesized comparison
differently from DuckDB and Spark); the others were implementation errors in
the proof path. tests/data/engine_counterexamples.json replays every recorded
case; the parametrized lists below pin each class by its own repro.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from semantix.extraction.canonicalize import canonicalize
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import PROOF_LEVELS, CanonLevel, Provenance

duckdb = pytest.importorskip("duckdb")

DATA = json.loads((Path(__file__).parent / "data" / "engine_counterexamples.json")
                  .read_text(encoding="utf-8"))
CAT = {"raw.orders": {"amount": "DOUBLE", "refund": "DOUBLE", "qty": "INTEGER",
                      "price": "DECIMAL(12,2)", "country": "VARCHAR", "status": "VARCHAR",
                      "tier": "INTEGER", "d": "DATE", "month": "VARCHAR"}}


def _registry(catalog: dict) -> SchemaRegistry:
    registry = SchemaRegistry(catalog)
    registry.set_estate_depth(list(catalog))
    return registry


def _fps(sql: str, catalog: dict, dialect: str = "duckdb"):
    registry = _registry(catalog)
    canon = canonicalize(sql, registry, dialect)
    return canon, extract_fingerprints(canon, "x", Provenance("q", "x"), dialect, registry)


def _proof(fp) -> bool:
    return fp.canon_level in PROOF_LEVELS and not fp.gaps


def _rows(con, sql):
    try:
        return sorted(map(repr, con.execute(sql).fetchall()))
    except Exception as e:  # noqa: BLE001 — an error is a result too
        return f"ERR {type(e).__name__}"


# ------------------------------------------------- the recorded counterexamples

@pytest.mark.parametrize("case", DATA["cases"], ids=[f"{c['kind']}-{i}" for i, c in
                                                     enumerate(DATA["cases"])])
def test_engine_counterexample_is_refused_or_distinguished(case):
    con = duckdb.connect()
    con.execute(DATA["seeds"][case["seed"]])
    catalog = DATA["catalogs"][case["catalog"]]
    if case["kind"] == "pair":
        assert _rows(con, case["a"]) != _rows(con, case["b"]), "not a counterexample"
        _, fa = _fps(case["a"], catalog, case["dialect"])
        _, fb = _fps(case["b"], catalog, case["dialect"])
        for x in fa:
            for y in fb:
                if x.concept_candidate == y.concept_candidate:
                    assert not (_proof(x) and _proof(y) and x.core.hash() == y.core.hash())
    else:
        canon, _ = _fps(case["sql"], catalog, case["dialect"])
        if canon.level == CanonLevel.SUPPORTED:
            # Inside the grammar, the proof form must mean what the original means.
            assert _rows(con, case["sql"]) == _rows(con, canon.ast.sql(case["dialect"]))


# ------------------------------------------------------- refused, by class

REFUSED = {
    # The parse: `%` bound like `+`; IS / IS DISTINCT FROM / NOT across a comparison.
    "SELECT SUM(qty + 1 % 3) AS m FROM raw.orders": "%",
    "SELECT COUNT(*) FILTER (WHERE qty % 3 * 2 = 2) AS m FROM raw.orders": "%",
    # Star modifiers, and a star the aggregating query's GROUP BY could not see.
    "WITH c AS (SELECT * REPLACE (refund AS amount) FROM raw.orders) "
    "SELECT SUM(amount) AS m FROM c": "modifier",
    "WITH c AS (SELECT * RENAME (refund AS amount, amount AS refund) FROM raw.orders) "
    "SELECT SUM(amount) AS m FROM c": "modifier",
    "SELECT * EXCLUDE (amount, refund, qty, price, status, tier, d, month), SUM(amount) AS m "
    "FROM raw.orders GROUP BY ALL": "modifier",
    "WITH c AS (SELECT * ILIKE '%mount%' FROM raw.orders) SELECT SUM(refund) AS m FROM c":
        "modifier",
    "SELECT *, COUNT(*) AS n FROM raw.orders GROUP BY ALL": "SELECT *",
    "WITH c AS (SELECT country FROM raw.orders) SELECT *, UPPER(country) AS u, COUNT(*) AS m "
    "FROM c GROUP BY 1": "SELECT *",
    # Constant GROUP BY keys that render as ordinals; parenthesized items; tuples.
    "SELECT country, SUM(amount) AS m FROM raw.orders GROUP BY (1)": "parenthesized",
    "SELECT country, tier, COUNT(*) AS m FROM raw.orders GROUP BY (1, 2)": "parenthesized",
    "SELECT SUM(amount) AS m, 1 AS k FROM raw.orders GROUP BY k": "constant",
    "SELECT 5 AS k, SUM(amount) AS m FROM raw.orders GROUP BY 1": "constant",
    "WITH c AS (SELECT 2 AS k, amount FROM raw.orders) SELECT SUM(amount) AS m FROM c "
    "GROUP BY k": "constant",
    # A unit written unquoted: the engines read the column of that name.
    "WITH c AS (SELECT 'year' AS month, d, amount FROM raw.orders) "
    "SELECT DATE_TRUNC(month, d) AS k, SUM(amount) AS m FROM c GROUP BY 1": "unquoted",
    "SELECT DATE_TRUNC(month, d) AS k, SUM(amount) AS m FROM raw.orders GROUP BY 1":
        "unquoted",
    # Function-form LIKE: the parser swaps its arguments.
    "SELECT COUNT(*) FILTER (WHERE like(status, 'pend%')) AS m FROM raw.orders": "LIKE",
    # The order rows are summed in changes a float SUM / AVG.
    "WITH c AS (SELECT amount FROM raw.orders ORDER BY amount) SELECT SUM(amount) AS m FROM c":
        "ORDER BY",
    "SELECT AVG(amount) AS m FROM (SELECT amount FROM raw.orders ORDER BY qty) t": "ORDER BY",
    # BETWEEN bounds of different kinds are typed differently from the expansion.
    "SELECT COUNT(*) AS m FROM raw.orders WHERE qty BETWEEN 2.5 AND '2.5'": "BETWEEN",
    "SELECT SUM(amount) AS m FROM raw.orders WHERE qty BETWEEN amount AND '2.5'": "BETWEEN",
    # CTE scope: a nested WITH; a forward reference; a later CTE's name.
    "WITH c AS (SELECT amount FROM raw.orders) SELECT SUM(amount) AS m FROM "
    "(WITH c AS (SELECT refund AS amount FROM raw.orders) SELECT amount FROM c) d": "WITH",
    "WITH a AS (SELECT amount FROM b), b AS (SELECT refund AS amount FROM raw.orders) "
    "SELECT SUM(amount) AS m FROM a": "before its CTE",
    # Qualifiers: exactly the name the source is visible under.
    "SELECT SUM(orders.amount) AS m FROM raw.orders AS o": "unknown source",
    "WITH orders AS (SELECT refund AS amount FROM raw.orders) "
    "SELECT SUM(raw.orders.amount) AS m FROM orders": "unknown source",
    "WITH orders AS (SELECT refund AS amount FROM raw.orders) "
    "SELECT SUM(x.orders.amount) AS m FROM orders": "unknown source",
    "SELECT SUM(raw.amount) AS m FROM raw.orders": "unknown source",
    "WITH c AS (SELECT refund AS amount FROM raw.orders) "
    "SELECT SUM(c.amount) AS m FROM c AS orders": "unknown source",
    # A dotted single identifier, or a string, is not raw.orders.
    'SELECT SUM(amount) AS m FROM "raw.orders"': "single identifier",
    "SELECT SUM(amount) AS m FROM 'raw.orders'": "single identifier",
    # A rendering that is not faithful.
    "SELECT SUM(CAST(amount AS FLOAT(53))) AS m FROM raw.orders": "FLOAT(p)",
}


@pytest.mark.parametrize("sql", list(REFUSED))
def test_refused_with_its_reason(sql):
    canon, fps = _fps(sql, CAT)
    assert canon.level != CanonLevel.SUPPORTED, sql
    assert not any(_proof(f) for f in fps)
    assert any(REFUSED[sql] in g for g in canon.gaps), canon.gaps


# Chains whose READING depends on the parser version: sqlglot 30.12.0 binds
# IS / IS DISTINCT FROM / NOT across the comparison (refused, as a chain or by
# the detector); 30.24 inserts the parentheses DuckDB implies. On any version
# the requirement is the same: refused, or inside the grammar with a proof
# form that executes as the original and no disagreement the detector finds.
PARSE_DEPENDENT = [
    "SELECT COUNT(*) AS m FROM raw.orders WHERE tier = 1 IS NOT FALSE",
    "SELECT COUNT(*) FILTER (WHERE amount > 0 IS NOT TRUE) AS m FROM raw.orders",
    "SELECT COUNT(*) AS m FROM raw.orders WHERE tier = 1 IS DISTINCT FROM TRUE",
    "SELECT COUNT(*) FILTER (WHERE tier = qty IS NULL) AS m FROM raw.orders",
    "SELECT COUNT(*) AS m FROM raw.orders WHERE NOT tier IN (1, 2) IN (TRUE, NULL)",
]
SEED = ("CREATE SCHEMA raw; CREATE TABLE raw.orders (amount DOUBLE, refund DOUBLE, "
        "qty INTEGER, price DECIMAL(12,2), country VARCHAR, status VARCHAR, tier INTEGER, "
        "d DATE, month VARCHAR); INSERT INTO raw.orders VALUES "
        "(1.5, 0.5, 2, 3.00, 'us', 'pending', 1, DATE '2026-01-15', 'x'), "
        "(-2.5, NULL, NULL, 1.00, 'de', NULL, 2, DATE '2026-03-01', 'y'), "
        "(0.0, 0.0, 1, 2.00, 'fr', 'done', NULL, NULL, NULL), "
        "(4.0, 1.0, 2, 2.00, 'us', 'done', 1, DATE '2026-02-01', 'z')")


@pytest.mark.parametrize("sql", PARSE_DEPENDENT)
def test_a_chain_is_refused_or_read_as_the_engine_reads_it(sql):
    import sqlglot

    from semantix.extraction.parse_agreement import NO_DISAGREEMENT_FOUND, check_parse

    canon, _ = _fps(sql, CAT)
    if sqlglot.__version__ == "30.12.0":
        assert canon.level != CanonLevel.SUPPORTED
        assert any("chain" in g or "read this statement differently" in g
                   for g in canon.gaps), canon.gaps
    if canon.level == CanonLevel.SUPPORTED:
        assert check_parse(sql, "duckdb").status == NO_DISAGREEMENT_FOUND
        con = duckdb.connect()
        con.execute(SEED)
        assert _rows(con, sql) == _rows(con, canon.ast.sql("duckdb"))


# Still inside the grammar: the refusals are not blanket.
ACCEPTED = [
    "SELECT SUM(amount) AS m FROM raw.orders WHERE NOT status IS NULL",
    "SELECT SUM(amount) AS m FROM raw.orders WHERE status IS NOT NULL AND tier = 1",
    "SELECT SUM(amount) AS m FROM raw.orders WHERE (tier = 1) IS NOT FALSE",
    "SELECT SUM(amount) AS m FROM raw.orders WHERE d BETWEEN '2026-01-01' AND '2026-02-01'",
    "SELECT SUM(amount) AS m FROM raw.orders WHERE tier BETWEEN -1 AND 5",
    "SELECT DATE_TRUNC('month', d) AS k, SUM(amount) AS m FROM raw.orders GROUP BY 1",
    "SELECT COUNT(*) FILTER (WHERE status LIKE 'pend%') AS m FROM raw.orders",
    "SELECT COUNT(*) FILTER (WHERE status NOT LIKE ('pend%')) AS m FROM raw.orders",
    "SELECT SUM(o.amount) AS m FROM raw.orders AS o",
    "SELECT SUM(orders.amount) AS m FROM raw.orders",
    "SELECT SUM(raw.orders.amount) AS m FROM raw.orders",
    "WITH a AS (SELECT amount FROM raw.orders), b AS (SELECT amount FROM a) "
    "SELECT SUM(amount) AS m FROM b",
    "SELECT SUM(amount) AS m FROM raw.orders ORDER BY 1",
]


@pytest.mark.parametrize("sql", ACCEPTED)
def test_still_inside_the_grammar(sql):
    canon, _ = _fps(sql, CAT)
    assert canon.level == CanonLevel.SUPPORTED, canon.gaps
    con = duckdb.connect()
    con.execute("CREATE SCHEMA raw; CREATE TABLE raw.orders (amount DOUBLE, refund DOUBLE, "
                "qty INTEGER, price DECIMAL(12,2), country VARCHAR, status VARCHAR, "
                "tier INTEGER, d DATE, month VARCHAR); INSERT INTO raw.orders VALUES "
                "(1.5, 0.5, 2, 3.00, 'us', 'pending', 1, DATE '2026-01-15', 'x'), "
                "(-2.5, NULL, NULL, 1.00, 'de', NULL, 2, DATE '2026-03-01', 'y')")
    assert _rows(con, sql) == _rows(con, canon.ast.sql("duckdb"))


def test_a_quoted_cte_name_is_matched_exactly_as_the_dialect_folds_it():
    """Snowflake folds unquoted names to upper case: a quoted "orders" CTE is
    not the table ORDERS a later unquoted `orders` reads."""
    catalog = {"main.finance.orders": {"amount": "DOUBLE", "status": "VARCHAR"}}
    sql = ('WITH "orders" AS (SELECT * FROM main.finance.orders WHERE status = \'x\') '
           'SELECT SUM(amount) AS m FROM orders')
    registry = SchemaRegistry(catalog)
    registry.set_estate_depth(list(catalog))
    from semantix.extraction.proof import certify

    result = certify(sql, registry, "snowflake")
    # The CTE is unused; `orders` is a bare table name, not the CTE.
    assert result.ast is None or "x" not in result.ast.sql("snowflake")


# ----------------------------------------- what ran, not the generator's rewrite

HOISTED = ("WITH t AS (SELECT * FROM orders) SELECT SUM(amount) AS m FROM ("
           "WITH orders AS (SELECT * FROM main.finance.orders WHERE status = 'completed') "
           "SELECT * FROM t) d")


def test_the_spark_generator_rewrite_is_never_judged_as_the_statement():
    import sqlglot

    from semantix.extraction.sqlutil import faithful_render

    tree = sqlglot.parse_one(HOISTED, read="databricks")
    assert faithful_render(tree, "databricks") is None          # it hoists the WITH
    assert faithful_render(tree, "duckdb") is not None


def test_a_view_dump_body_the_generator_rewrites_is_reported_not_scanned(tmp_path):
    from semantix.adapters.sql_dir import load_views_file

    dump = tmp_path / "views.sql"
    dump.write_text(f"CREATE VIEW main.finance.v AS {HOISTED};\n"
                    "CREATE VIEW main.finance.w AS SELECT SUM(amount) AS m "
                    "FROM main.finance.orders;\n")
    artifacts, skipped = load_views_file(dump, "databricks")
    assert [a.relation for a in artifacts] == ["main.finance.w"]
    assert any("re-rendered faithfully" in s["reason"] for s in skipped)


def test_execution_never_sends_the_generator_rewrite():
    from semantix.services.execute import _single_statement

    with pytest.raises(ValueError, match="faithfully"):
        _single_statement(HOISTED, "databricks")


def test_a_rename_touches_the_name_it_makes_readable():
    """exp.RenameTable is not in sqlglot 30.x — it is exp.AlterRename — so the
    renamed-to name was never recorded and a later read of it looked
    unaffected by the session."""
    import sqlglot

    from semantix.services.session import affects, classify

    for sql in ("ALTER VIEW main.finance.staging RENAME TO main.finance.orders",
                "ALTER TABLE main.finance.staging RENAME TO orders"):
        e = classify(sqlglot.parse_one(sql, read="databricks"), 0, 0, "s1", None)
        assert e is not None and "main.finance.orders" in e.names, (sql, e)
        assert affects(e, ["main.finance.orders"], 3)


# ------------------------------------------------------ the monitor's rules

O = "main.finance.orders"


def _monitor(tmp_path, sql, **kw):
    from .test_monitor_conformance import _rows, run

    return run(tmp_path, _rows(sql), **kw)


def _codes(result):
    return [[r["code"] for r in f["reasons"]] for f in result["findings"]]


@pytest.mark.parametrize("name", ["or_duplicate", "cast_noop_in_cte", "cast_noop_raw",
                                  "times_one_in_cte", "double_rounding", "double_underflow",
                                  "backslash_escape"])
def test_a_value_the_definition_may_compute_is_never_a_suspected_difference(tmp_path, name):
    from .test_monitor_conformance import GOVERNED as G

    sql = {
        "or_duplicate": f"SELECT {G} AS net_revenue FROM {O} WHERE status = 'completed' "
                        f"OR status = 'completed'",
        "cast_noop_in_cte": f"WITH c AS (SELECT CAST(amount AS DOUBLE) AS amount, "
                            f"refund_amount, status FROM {O}) SELECT {G} AS net_revenue "
                            f"FROM c WHERE status = 'completed'",
        "cast_noop_raw": f"SELECT SUM(CAST(amount AS DOUBLE)) - SUM(refund_amount) AS "
                         f"net_revenue FROM {O} WHERE status = 'completed'",
        "times_one_in_cte": f"WITH c AS (SELECT amount * 1 AS amount, refund_amount, status "
                            f"FROM {O}) SELECT {G} AS net_revenue FROM c "
                            f"WHERE status = 'completed'",
        "double_rounding": f"SELECT ({G}) * 1.00000000000000000001 AS net_revenue FROM {O} "
                           f"WHERE status = 'completed'",
        "double_underflow": f"SELECT ({G}) + 1e-400 AS net_revenue FROM {O} "
                            f"WHERE status = 'completed'",
        "backslash_escape": f"SELECT {G} AS net_revenue FROM {O} WHERE status = 'complet\\ed'",
    }[name]
    result = _monitor(tmp_path, sql)
    assert [f["kind"] for f in result["findings"]] == ["unverifiable"], _codes(result)
    allowed = ("implication_undecided", "transformation_undecided")
    if name == "backslash_escape":
        # sqlglot 30.24 unescapes it to 'completed', as Spark does: then it IS
        # the required filter (an experimental match, still unverifiable).
        allowed += ("positive_suspended",)
    assert _codes(result)[0][0] in allowed


def test_a_literal_on_a_collated_column_is_not_decided(tmp_path, monkeypatch):
    from . import test_monitor_conformance as T

    monkeypatch.setattr(T, "CATALOG", T.CATALOG.replace(
        "status: STRING", "status: STRING COLLATE UTF8_LCASE"))
    for i, where in enumerate(("status = 'Completed'",
                               "status IN ('completed', 'COMPLETED')")):
        result = _monitor(tmp_path / str(i),
                          f"SELECT {T.GOVERNED} AS net_revenue FROM {O} WHERE {where}")
        assert [f["kind"] for f in result["findings"]] == ["unverifiable"], _codes(result)


def test_a_grouping_key_computed_by_an_unknown_function_is_not_a_measure(tmp_path):
    from .test_monitor_conformance import GOVERNED as G

    result = _monitor(tmp_path, f"SELECT initcap_custom(channel) AS ch, {G} AS net_revenue "
                                f"FROM {O} WHERE status = 'completed' "
                                f"GROUP BY initcap_custom(channel)")
    assert result["funnel"]["measure_uses"] == 1
    assert [f["concept"] for f in result["findings"]] == ["net_revenue"]


def test_the_exclusion_list_matches_what_the_rules_judge():
    from semantix.services.conformance import EXCLUDED

    assert "CTEs, subqueries and derived tables around a variation" not in EXCLUDED
    assert any("outside the supported grammar" in e for e in EXCLUDED)


def test_a_statement_the_generator_rewrites_is_unreadable_to_the_monitor(tmp_path):
    from .test_monitor_conformance import GOVERNED as G

    sql = (f"WITH t AS (SELECT * FROM {O} WHERE status = 'completed') "
           f"SELECT {G} AS net_revenue FROM (WITH orders AS (SELECT amount * 2 AS amount, "
           f"refund_amount, status FROM {O}) SELECT * FROM t) d")
    result = _monitor(tmp_path, sql)
    assert result["unreadable_statements"] == 1 and result["findings"] == []
