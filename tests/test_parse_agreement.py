"""The parser-disagreement detector (extraction.parse_agreement).

A DETECTOR, for DuckDB SQL only: it asks whether DuckDB reads the original
statement the way sqlglot does. It catches the confirmed round-seventeen
misreadings; `not_checked` is never agreement; no disagreement found
qualifies nothing — about DuckDB in general, or any other engine — and the
result always names the dialect and both versions.
"""

from __future__ import annotations

import pytest

from touchstone.extraction import parse_agreement as pa
from touchstone.extraction.canonicalize import canonicalize
from touchstone.extraction.registry import SchemaRegistry
from touchstone.models import CanonLevel

pytest.importorskip("duckdb")

CAT = {"raw.orders": {"amount": "DOUBLE", "refund": "DOUBLE", "qty": "INTEGER",
                      "tier": "INTEGER", "status": "VARCHAR", "d": "DATE"}}

CONFIRMED = [
    "SELECT SUM(qty + 1 % 3) AS m FROM raw.orders",           # % at the precedence of +
    "SELECT SUM(7 % 4 * 2) AS m FROM raw.orders",
    "SELECT COUNT(*) AS m FROM raw.orders WHERE tier = qty IS NULL",
    "SELECT COUNT(*) AS m FROM raw.orders WHERE tier = 1 IS NOT TRUE",
    "SELECT COUNT(*) AS m FROM raw.orders WHERE tier = 1 IS DISTINCT FROM TRUE",
    "SELECT COUNT(*) AS m FROM raw.orders WHERE NOT tier IN (1, 2) IN (TRUE, NULL)",
    "SELECT DATE_TRUNC(month, d) AS k, COUNT(*) AS m FROM raw.orders GROUP BY 1",
]
AGREED = [
    "SELECT SUM(amount - refund) AS m FROM raw.orders WHERE status = 'x' AND NOT qty > 1",
    "SELECT COUNT(*) FILTER (WHERE amount BETWEEN 1 AND 2) AS m FROM raw.orders",
    "SELECT SUM(amount) AS m FROM raw.orders WHERE amount IS NOT NULL AND tier NOT IN (1, 2)",
    "SELECT DATE_TRUNC('month', d) AS k, SUM(amount) AS m FROM raw.orders GROUP BY 1",
    "SELECT SUM(CASE WHEN tier = 1 THEN amount END) / NULLIF(COUNT(*), 0) AS m FROM raw.orders",
    "SELECT SUM(-qty ^ 2) AS m FROM raw.orders",       # `^` and POWER() are one function
    "SELECT SUM(CEILING(amount)) AS m FROM raw.orders WHERE status NOT LIKE 'p%'",
]


SEED = ("CREATE SCHEMA raw; CREATE TABLE raw.orders (amount DOUBLE, refund DOUBLE, "
        "qty INTEGER, tier INTEGER, status VARCHAR, d DATE); INSERT INTO raw.orders VALUES "
        "(1.5, 0.5, 2, 1, 'pending', DATE '2026-01-15'), (-2.5, NULL, NULL, 2, NULL, NULL), "
        "(3.0, 1.0, 7, 1, 'done', DATE '2026-03-01'), (0.0, 0.0, 3, NULL, 'x', DATE '2026-02-02')")


@pytest.mark.parametrize("sql", CONFIRMED)
def test_the_confirmed_misreadings_are_detected(sql):
    """The misreadings are sqlglot 30.12.0's (the pinned version); 30.24 parses
    `%` and several IS chains as the engines do. On ANY version, a reading the
    detector accepts must execute as the original — it never hides a
    difference in results here."""
    import duckdb
    import sqlglot

    check = pa.check_parse(sql, "duckdb")
    assert check.engine == "duckdb" and check.engine_version and check.sqlglot_version
    assert check.dialect == "duckdb"
    if sqlglot.__version__ == "30.12.0":
        assert check.status == pa.DISAGREES, check
    if check.status == pa.NO_DISAGREEMENT_FOUND:
        con = duckdb.connect()
        con.execute(SEED)
        reading = pa._fully_parenthesized(sqlglot.parse_one(sql, read="duckdb")).sql("duckdb")
        assert sorted(map(repr, con.execute(sql).fetchall())) == \
            sorted(map(repr, con.execute(reading).fetchall()))


@pytest.mark.parametrize("sql", AGREED)
def test_ordinary_sql_finds_no_disagreement(sql):
    assert pa.check_parse(sql, "duckdb").status == pa.NO_DISAGREEMENT_FOUND


@pytest.mark.parametrize("sql, dialect", [
    ("SELECT SUM(qty + 1 % 3) FROM t", "databricks"),     # no native parser here
    ("SELECT SUM(qty + 1 % 3) FROM t", "snowflake"),
    ("SELECT FROM WHERE", "duckdb"),                       # neither parser accepts it
    ("SELECT 1; SELECT 2", "duckdb"),                      # one statement at a time
    ("SELECT SUM(CAST(amount AS FLOAT(53))) FROM t", "duckdb"),  # DuckDB rejects the reading
])
def test_what_it_cannot_answer_is_not_checked_never_agreement(sql, dialect):
    check = pa.check_parse(sql, dialect)
    assert check.status == pa.NOT_CHECKED, check
    assert check.detail
    if dialect != "duckdb":
        assert f"qualifies nothing about {dialect}" in check.detail


def test_without_duckdb_nothing_is_checked(monkeypatch):
    monkeypatch.setattr(pa, "_versions", lambda: (None, "x"))
    assert pa.check_parse("SELECT 1", "duckdb").status == pa.NOT_CHECKED


def _registry():
    registry = SchemaRegistry(CAT)
    registry.set_estate_depth(list(CAT))
    return registry


def test_a_detected_disagreement_refuses_proof_grade(monkeypatch):
    """Refusal first: whatever the grammar admitted, a statement the engine
    reads differently never reaches proof grade."""
    sql = "SELECT SUM(amount) AS m FROM raw.orders"
    assert canonicalize(sql, _registry(), "duckdb").level == CanonLevel.SUPPORTED
    monkeypatch.setattr(pa, "check_parse", lambda s, d: pa.ParseCheck(
        pa.DISAGREES, d, "duckdb", "0", "0", "forced"))
    canon = canonicalize(sql, _registry(), "duckdb")
    assert canon.level != CanonLevel.SUPPORTED
    assert any("read this statement differently" in g for g in canon.gaps)


def test_proof_grade_forms_record_their_check_from_the_original_text():
    canon = canonicalize("SELECT SUM(amount) AS m FROM raw.orders WHERE amount IS NOT NULL",
                         _registry(), "duckdb")
    assert canon.parse_check["status"] == pa.NO_DISAGREEMENT_FOUND
    assert canon.parse_check["dialect"] == "duckdb"
    other = canonicalize("SELECT SUM(amount) AS m FROM raw.orders", _registry(), "databricks")
    assert other.parse_check["status"] == pa.NOT_CHECKED


def test_experimental_comparisons_carry_the_detector_summary(tmp_path):
    from touchstone.engine import run_scan

    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "m.sql").write_text(
        "-- touchstone: relation=db.main.m\nSELECT SUM(amount) AS m FROM db.main.t\n")
    (tmp_path / "metrics.yml").write_text(
        "version: 1\nmetrics:\n  - name: m\n    model: db.main.t\n    expr: \"sum(amount)\"\n")
    (tmp_path / "catalog.yml").write_text("db:\n  main:\n    t:\n      amount: DOUBLE\n")
    result, store = run_scan(sql_dir=str(tmp_path / "models"),
                             metrics_path=str(tmp_path / "metrics.yml"),
                             catalog_path=str(tmp_path / "catalog.yml"),
                             db_path=str(tmp_path / "s.db"), dialect="duckdb")
    store.close()
    exp_ = [v.evidence["experimental"] for v in result.verdicts
            if "experimental" in v.evidence]
    assert exp_, [v.verdict_type for v in result.verdicts]
    summary = exp_[0]["proof"]["parse_agreement"]
    assert summary["engine"] == "duckdb" and summary["dialects"] == ["duckdb"]
    assert summary["statuses"].get(pa.DISAGREES, 0) == 0
    assert "qualifies nothing" in summary["note"]


def test_the_oracle_counts_the_detector_and_fails_on_a_proof_grade_disagreement():
    from touchstone import oracle

    report = oracle.run_oracle()
    assert report.parse_checks.get(pa.DISAGREES, 0) >= 5   # the refused blind spots
    assert report.parse_disagreements == [] and report.ok


def test_engine_serialization_details_are_not_findings():
    """DuckDB 2.0 added `query_location_length`; treating a new location
    field as structure made every statement 'disagree' under the newer
    engine, refusing the whole grammar during the sqlglot 30.24 run."""
    node = {"class": "OPERATOR", "type": "COMPARE_EQUAL", "query_location": 7,
            "query_location_length": 11, "is_operator": True,
            "children": [{"class": "CONSTANT", "value": 1, "query_location": 9}]}
    assert pa._normalize(node) == {"class": "OPERATOR", "type": "COMPARE_EQUAL",
                                   "children": [{"class": "CONSTANT"}]}
