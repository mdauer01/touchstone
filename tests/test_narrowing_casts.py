"""A narrowing cast is kept as an observation for review — never a verdict.

`SUM(CAST(amount AS INT))` over a DOUBLE column CAN change the result; whether
it does depends on the data. The boundary stays conservative (the use is
undecided, not a suspected difference), and the cast travels with the finding
and into the exported record, worded as what it is.
"""

from __future__ import annotations

import pytest

from touchstone.services.conformance import _narrows, _numeric_type

from .policy import engine_codes, engine_kind
from .test_monitor_conformance import _rows, run

pytest.importorskip("duckdb")

O = "main.finance.orders"


def _one(tmp_path, sql):
    (f,) = run(tmp_path, _rows(sql))["findings"]
    return f


def test_a_narrowing_cast_on_a_column_is_an_observation_not_a_change(tmp_path):
    f = _one(tmp_path, f"SELECT SUM(CAST(amount AS INT)) - SUM(refund_amount) AS net_revenue "
                       f"FROM {O} WHERE status = 'completed'")
    assert engine_kind(f) == "unverifiable" and f["kind"] == "unverifiable"
    assert engine_codes(f) == ["transformation_undecided"]
    (note,) = f["suspected"]
    assert "narrows amount (DOUBLE) to INT" in note
    assert "whether it changes this result is not decided" in note


def test_a_cast_that_cannot_change_a_value_leaves_no_observation(tmp_path):
    f = _one(tmp_path, f"SELECT SUM(CAST(amount AS DOUBLE)) - SUM(refund_amount) AS "
                       f"net_revenue FROM {O} WHERE status = 'completed'")
    assert engine_codes(f) == ["transformation_undecided"]
    assert not f.get("suspected")


def test_a_cast_of_the_aggregate_is_observed_too(tmp_path):
    f = _one(tmp_path, f"SELECT CAST(SUM(amount) - SUM(refund_amount) AS INT) AS net_revenue "
                       f"FROM {O} WHERE status = 'completed'")
    assert engine_codes(f) == ["transformation_undecided"]
    assert any("narrows the value it casts (DOUBLE) to INT" in n for n in f["suspected"])


@pytest.mark.parametrize("source, target, narrows", [
    ("DOUBLE", "INT", True), ("DOUBLE", "DECIMAL(18,2)", True), ("DOUBLE", "FLOAT", True),
    ("DOUBLE", "DOUBLE", False), ("FLOAT", "DOUBLE", False),
    ("DECIMAL(12,2)", "DECIMAL(12,0)", True), ("DECIMAL(12,2)", "DECIMAL(18,4)", False),
    ("DECIMAL(12,2)", "BIGINT", True), ("INT", "BIGINT", False), ("BIGINT", "INT", True),
    ("BIGINT", "DOUBLE", True), ("INT", "DOUBLE", False), ("STRING", "INT", None),
    (None, "INT", None),
    # Exact digit capacities (round-18 review, checked against DuckDB):
    ("BIGINT", "DECIMAL(19,0)", False),    # every BIGINT fits 19 digits
    ("BIGINT", "DECIMAL(18,0)", True),
    ("DECIMAL(19,0)", "BIGINT", True),     # 9999999999999999999 does not fit
    ("DECIMAL(18,0)", "BIGINT", False),
    ("DECIMAL(12,2)", "DOUBLE", True),     # 0.10 is not a binary fraction
    ("DECIMAL(15,0)", "DOUBLE", False),
    ("INT", "DECIMAL(10,0)", False), ("INT", "DECIMAL(9,0)", True),
    ("INT", "FLOAT", True), ("SMALLINT", "FLOAT", False),
])
def test_what_counts_as_narrowing(source, target, narrows):
    assert _narrows(_numeric_type(source), _numeric_type(target)) is narrows


@pytest.mark.parametrize("dialect, expected", [("databricks", ("decimal", 10, 0)),
                                               ("duckdb", ("decimal", 18, 3)),
                                               ("postgres", None)])
def test_a_bare_decimal_means_what_the_dialect_says(dialect, expected):
    assert _numeric_type("DECIMAL", dialect) == expected


def test_a_no_op_cast_around_a_sub_aggregate_is_undecided_not_a_difference(tmp_path):
    f = _one(tmp_path, f"SELECT CAST(SUM(amount) AS DOUBLE) - CAST(SUM(refund_amount) AS "
                       f"DOUBLE) AS net_revenue FROM {O} WHERE status = 'completed'")
    assert f["kind"] == "unverifiable"
    assert engine_codes(f) == ["transformation_undecided"]
    assert not f.get("suspected")


def test_a_narrowing_cast_of_a_sub_aggregate_is_undecided_and_observed(tmp_path):
    f = _one(tmp_path, f"SELECT CAST(SUM(amount) AS INT) - SUM(refund_amount) AS net_revenue "
                       f"FROM {O} WHERE status = 'completed'")
    assert engine_codes(f) == ["transformation_undecided"]
    assert any("narrows the value it casts (DOUBLE) to INT" in n for n in f["suspected"])


@pytest.mark.parametrize("measure, expect", [
    ("SUM(CAST(amount AS INT)) FILTER (WHERE status = 'completed') - SUM(refund_amount) "
     "FILTER (WHERE status = 'completed')", "narrows amount (DOUBLE) to INT"),
    ("SUM(CAST(ROUND(amount) AS INT)) - SUM(refund_amount)",
     "narrows the value it casts (DOUBLE) to INT"),
    ("CAST(SUM(amount) - SUM(refund_amount) AS FLOAT)",
     "narrows the value it casts (DOUBLE) to FLOAT"),
])
def test_the_observation_is_kept_on_every_path(tmp_path, measure, expect):
    where = "" if "FILTER" in measure else " WHERE status = 'completed'"
    f = _one(tmp_path, f"SELECT {measure} AS net_revenue FROM {O}{where}")
    assert f["kind"] == "unverifiable"
    assert any(expect in n for n in f.get("suspected") or []), f.get("suspected")


def test_observations_derive_types_rather_than_guess(tmp_path, monkeypatch):
    from . import test_monitor_conformance as T

    monkeypatch.setattr(T, "CATALOG", T.CATALOG.replace("amount: DOUBLE", "amount: BIGINT")
                        .replace("refund_amount: DOUBLE", "refund_amount: BIGINT"))
    f = _one(tmp_path / "a", f"SELECT CAST(SUM(amount) - SUM(refund_amount) AS BIGINT) AS "
                             f"net_revenue FROM {O} WHERE status = 'completed'")
    assert not f.get("suspected")                     # BIGINT to BIGINT cannot change it
    monkeypatch.setattr(T, "CATALOG", T.CATALOG.replace("amount: BIGINT", "amount: STRING"))
    g = _one(tmp_path / "b", f"SELECT SUM(CAST(amount AS INT)) - SUM(refund_amount) AS "
                             f"net_revenue FROM {O} WHERE status = 'completed'")
    assert any("declared STRING, not a numeric type" in n for n in g.get("suspected") or [])


# ------------------------------------------- round-18 verification

def test_a_cast_of_a_metric_view_measure_is_observed(tmp_path):
    f = _one(tmp_path, "SELECT CAST(MEASURE(net_revenue) AS INT) AS net_revenue "
                       "FROM main.finance.revenue_mv")
    assert f["kind"] != "conforming_use"
    assert any("CAST(MEASURE(net_revenue) AS INT) narrows the value it casts (DOUBLE) to INT"
               in n for n in f.get("suspected") or []), f.get("suspected")


def test_a_cast_is_observed_when_the_source_changed_too(tmp_path, monkeypatch):
    from . import test_monitor_conformance as T

    monkeypatch.setattr(T, "CATALOG", T.CATALOG.replace(
        "    targets:", "    orders_v2:\n      amount: DOUBLE\n      refund_amount: DOUBLE\n"
                       "      status: STRING\n    targets:"))
    f = _one(tmp_path, "SELECT SUM(CAST(amount AS INT)) - SUM(refund_amount) AS net_revenue "
                       "FROM main.finance.orders_v2 WHERE status = 'completed'")
    assert "source_changed" in engine_codes(f)
    assert any("narrows amount (DOUBLE) to INT" in n for n in f.get("suspected") or [])


def test_the_definitions_own_cast_is_not_an_observation_about_the_query(tmp_path):
    from .test_monitor_conformance import VIEW

    view = VIEW.format(extra="").replace("expr: SUM(amount) - SUM(refund_amount)",
                                         "expr: SUM(CAST(amount AS DECIMAL(18,2))) - "
                                         "SUM(refund_amount)")
    (f,) = run(tmp_path, _rows("SELECT SUM(CAST(amount AS DECIMAL(18,2))) - SUM(discount) AS "
                               f"net_revenue FROM {O} WHERE status = 'completed'"),
               view_text=view)["findings"]
    assert not any("DECIMAL(18, 2)" in n for n in f.get("suspected") or []), f.get("suspected")


def test_a_cast_wrapping_an_aggregate_is_described_as_such(tmp_path):
    f = _one(tmp_path, f"SELECT CAST(SUM(amount) AS DOUBLE) - CAST(SUM(refund_amount) AS "
                       f"DOUBLE) AS net_revenue FROM {O} WHERE status = 'completed'")
    detail = f["reasons"][0]["detail"] if f["reasons"][0]["code"] != "positive_suspended" \
        else f["experimental"]["reasons"][0]["detail"]
    assert "around its columns or aggregates" in detail


@pytest.mark.parametrize("expr, expect", [
    ("CAST(amount * 0.5 AS INT)", "is not known"),          # INT × 0.5 is a DECIMAL
    ("CAST(amount + 1 AS BIGINT)", None),                   # INT + INT widens: no change
    ("CAST(CAST(d AS STRING) AS INT)", "the value it casts is STRING"),
    ("CAST(1.5 AS INT)", "changes the constant 1.5"),      # decided by value
    ("COALESCE(SUM(d), CAST(0 AS DOUBLE))", None),
    ("CAST('5' AS INT)", "the value it casts is a string literal"),
])
def test_observation_wording_follows_what_the_sql_states(expr, expect):
    import sqlglot

    from touchstone.services.conformance import narrowing_casts

    notes = narrowing_casts(sqlglot.parse_one(expr, read="databricks"),
                            {"amount": "INT", "d": "DOUBLE"}, "databricks")
    if expect is None:
        assert notes == []
    else:
        (note,) = notes
        assert expect in note


@pytest.mark.parametrize("where, conforms", [
    ("order_date BETWEEN DATE '2026-01-01' AND '2026-02-01'", False),   # bounds of two kinds
    ("order_date BETWEEN '2026-01-01' AND '2026-02-01'", True),
    ("order_date BETWEEN DATE '2026-01-01' AND DATE '2026-02-01'", True),
])
def test_a_metric_view_between_keeps_the_conservative_boundary(tmp_path, where, conforms):
    f = _one(tmp_path, f"SELECT MEASURE(net_revenue) AS net_revenue "
                       f"FROM main.finance.revenue_mv WHERE {where}")
    assert (engine_kind(f) == "conforming_use") is conforms
    if not conforms:
        assert engine_codes(f) == ["predicate_unsupported"]



# ------------------------------------------- round-18 verification, second pass

@pytest.mark.parametrize("expr, types, dialect, expect", [
    # ROUND to cents, then a cast to cents: no value changes.
    ("CAST(ROUND(a, 2) AS DECIMAL(18,2))", {"a": "DECIMAL(18,4)"}, "databricks", None),
    ("CAST(ROUND(a, 2) AS DECIMAL(18,2))", {"a": "DECIMAL(18,4)"}, "duckdb", None),
    ("CAST(ROUND(a, 2) AS DECIMAL(18,1))", {"a": "DECIMAL(18,4)"}, "databricks", "narrows"),
    # FLOAT + an integer is a FLOAT; an integer divided by 1 is a DOUBLE.
    ("CAST(f + 1 AS FLOAT)", {"f": "FLOAT"}, "databricks", None),
    ("CAST(b / 1 AS DOUBLE)", {"b": "BIGINT"}, "databricks", None),
    # Every integer of 7 digits is a FLOAT exactly.
    ("CAST(k AS FLOAT)", {"k": "DECIMAL(7,0)"}, "databricks", None),
    ("CAST(k AS FLOAT)", {"k": "DECIMAL(8,0)"}, "databricks", "narrows"),
    # DuckDB types an integer literal as the column's integer type.
    ("CAST(ti + 1 AS TINYINT)", {"ti": "TINYINT"}, "duckdb", None),
    ("CAST(SUM(i) AS BIGINT)", {"i": "INT"}, "duckdb", "narrows"),       # SUM is HUGEINT
    ("CAST(i AS HUGEINT)", {"i": "INT"}, "duckdb", None),
    ("CAST(i AS UBIGINT)", {"i": "INT"}, "duckdb", "does not model"),
    # Constants are decided by value; NULL and booleans change nothing.
    ("CAST(1.0 AS DOUBLE)", {}, "databricks", None),
    ("CAST(0.1 AS DOUBLE)", {}, "databricks", None),       # reads back as 0.1
    ("CAST(0.10000000000000000001 AS DOUBLE)", {}, "databricks", "changes the constant"),
    ("CAST(-2147483648 AS INT)", {}, "databricks", None),  # the type's minimum
    ("CAST(-2147483649 AS INT)", {}, "databricks", "changes the constant"),
    ("CAST(10000000000 AS DECIMAL(38,18))", {}, "databricks", None),   # once crashed the run
    ("CAST(1e39 AS FLOAT)", {}, "databricks", "changes the constant"),    # overflows
    # DuckDB: AVG of a DECIMAL is a DOUBLE; a literal takes the column's type.
    ("CAST(AVG(price) AS DOUBLE)", {"price": "DECIMAL(12,2)"}, "duckdb", None),
    ("CAST(AVG(price) AS DECIMAL(16,6))", {"price": "DECIMAL(12,2)"}, "duckdb", "narrows"),
    ("CAST(f + 1.5 AS REAL)", {"f": "FLOAT"}, "duckdb", None),
    ("CAST(ti + -1 AS TINYINT)", {"ti": "TINYINT"}, "duckdb", None),
    ("CAST(qty * 100000 AS SMALLINT)", {"qty": "SMALLINT"}, "duckdb", "narrows"),
    ("CAST(i + 3000000000 AS INT)", {"i": "INT"}, "duckdb", "narrows"),
    # Snowflake integers are NUMBER(38,0); BigQuery's are INT64.
    ("CAST(b AS INT)", {"b": "BIGINT"}, "snowflake", None),
    ("CAST(b AS INT64)", {"b": "INT64"}, "bigquery", None),
    ("CAST(f AS FLOAT64)", {"f": "FLOAT64"}, "bigquery", None),
    ("CAST(NULL AS DOUBLE)", {}, "databricks", None),
    ("CAST(TRUE AS INT)", {}, "databricks", None),
    ("CAST(x AS STRING)", {"x": "INT"}, "databricks", None),
])
def test_value_types_follow_the_engines(expr, types, dialect, expect):
    import sqlglot

    from touchstone.services.conformance import narrowing_casts

    notes = narrowing_casts(sqlglot.parse_one(expr, read=dialect), types, dialect)
    if expect is None:
        assert notes == [], notes
    else:
        assert len(notes) == 1 and expect in notes[0], notes


def test_round_then_cast_is_checked_against_duckdb():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    assert con.execute("SELECT typeof(round(CAST(1.23456 AS DECIMAL(18,4)), 2))").fetchone()[0] \
        == "DECIMAL(18,2)"


@pytest.mark.parametrize("measure", [
    "SUM(amount) * 1.0D - SUM(refund_amount)",
    "SUM(amount) * CAST(1 AS DOUBLE) - SUM(refund_amount)",
    "SUM(amount * CAST(1 AS DOUBLE)) - SUM(refund_amount)",
    "SUM(amount + CAST(0 AS DOUBLE)) - SUM(refund_amount)",
])
def test_neutral_arithmetic_with_a_typed_literal_is_undecided(tmp_path, measure):
    f = _one(tmp_path, f"SELECT {measure} AS net_revenue FROM {O} WHERE status = 'completed'")
    assert f["kind"] == "unverifiable"
    assert engine_codes(f) == ["transformation_undecided"]


def test_a_cast_is_typed_by_the_relation_it_reads_in_a_join(tmp_path, monkeypatch):
    from touchstone.services.monitor import _typed_columns

    class Reg:
        def column_types(self, r):
            return {"main.finance.orders": {"amount": "INT", "status": "STRING"},
                    "main.finance.targets": {"amount": "DOUBLE"}}[r]
    types = _typed_columns(Reg(), ["main.finance.orders", "main.finance.targets"])
    assert "amount" not in types and types["status"] == "STRING"
    import sqlglot

    from touchstone.services.conformance import narrowing_casts

    cast = sqlglot.parse_one("CAST(main.finance.orders.amount AS BIGINT)", read="databricks")
    assert narrowing_casts(cast, types, "databricks") == []          # INT to BIGINT widens


def test_the_definitions_cast_is_not_blamed_on_the_optimizer_reading_either():
    import sqlglot

    from touchstone.services.conformance import narrowing_casts

    governed = sqlglot.parse_one("SUM(CAST(amount AS DECIMAL(18,2))) - SUM(refund_amount)")
    hypothesis = sqlglot.parse_one(
        "SUM(CAST(main.finance.orders.amount AS DECIMAL(18,2))) - SUM(refund_amount)")
    assert narrowing_casts(hypothesis, {"amount": "DOUBLE"}, "databricks",
                           governed=governed) == []


def test_repeated_casts_are_observed_once(tmp_path):
    f = _one(tmp_path, f"SELECT SUM(CASE WHEN country = 'US' THEN CAST(amount AS INT) ELSE "
                       f"CAST(amount AS INT) END) - SUM(refund_amount) AS net_revenue "
                       f"FROM {O} WHERE status = 'completed'")
    notes = [n for n in f.get("suspected") or [] if "narrows amount" in n]
    assert len(notes) == 1


@pytest.mark.parametrize("where", ["country IN ('US', 5)", "country IN ('US', 'DE')"])
def test_an_in_list_of_mixed_kinds_is_not_a_slice(tmp_path, where):
    mixed = "5" in where
    for shape, sql in (("A", f"SELECT SUM(amount) - SUM(refund_amount) AS net_revenue FROM {O} "
                             f"WHERE status = 'completed' AND {where}"),
                       ("B", f"SELECT MEASURE(net_revenue) AS net_revenue "
                             f"FROM main.finance.revenue_mv WHERE {where}")):
        f = _one(tmp_path / shape / ("m" if mixed else "s"), sql)
        assert (engine_kind(f) == "conforming_use") is (not mixed), (shape, f["reasons"])


def test_a_projection_combining_measures_keeps_its_cast_observation(tmp_path):
    from .test_monitor_conformance import VIEW

    view = VIEW.format(extra="") + "  - name: gross\n    expr: SUM(amount)\n"
    (f, *_) = [x for x in run(tmp_path, _rows(
        "SELECT CAST(MEASURE(gross) AS INT) - MEASURE(net_revenue) AS diff "
        "FROM main.finance.revenue_mv"), view_text=view)["findings"]]
    assert any("CAST(MEASURE(gross) AS INT) narrows the value it casts (DOUBLE) to INT" in n
               for n in f.get("suspected") or []), f.get("suspected")


def test_a_high_scale_constant_never_aborts_the_monitor_run(tmp_path):
    result = run(tmp_path, _rows(
        f"SELECT SUM(amount) - SUM(refund_amount) AS net_revenue FROM {O}",
        f"SELECT LEAST(SUM(amount), CAST(10000000000 AS DECIMAL(38,18))) - SUM(refund_amount) "
        f"AS net_revenue FROM {O} WHERE status = 'completed'",
        "SELECT MEASURE(net_revenue) * CAST(10000000000 AS DECIMAL(38,18)) AS net_revenue "
        "FROM main.finance.revenue_mv"))
    assert len(result["findings"]) >= 2


def test_a_bd_literal_is_refused_not_read_as_a_bare_decimal(tmp_path):
    """sqlglot renders 1.05BD as CAST(1.05 AS DECIMAL), DECIMAL(10,0) on
    Databricks: the proof form would compute 1."""
    for shape, sql in (("A", f"SELECT SUM(amount * 1.05BD) - SUM(refund_amount) AS net_revenue "
                             f"FROM {O} WHERE status = 'completed'"),
                       ("B", "SELECT MEASURE(net_revenue) * 1.05BD AS net_revenue "
                             "FROM main.finance.revenue_mv")):
        f = _one(tmp_path / shape, sql)
        assert engine_codes(f) == ["shape_unsupported"], (shape, f["reasons"])
        assert "1.05BD" in f["reasons"][0]["detail"]
    from touchstone.extraction.proof import Refusal, _check_tokens

    with pytest.raises(Refusal, match="BD"):
        _check_tokens("SELECT SUM(a * 1.05BD) FROM t", "databricks")
