"""Execution oracle: ground truth that does not depend on the canonicalizer.

The equivalence benchmark asks the canonicalizer about pairs someone thought
to write down, and judges the answers with the canonicalizer's own output.
It passed while two false certifications stood (round eight): SUM(a + b + c)
against SUM(a + c + b) over DOUBLE, and prod.raw.orders against
dev.raw.orders. Neither pair was in the corpus.

This module turns the guarantee into an executable property. It generates
hundreds of single-measure queries across the rewrites the engine performs
(arithmetic, predicates, grouping constructs, joins, relation qualification,
order-sensitive consumers, subqueries), canonicalizes every one, buckets them
by core hash among the proof-grade fingerprints — exactly the set the engine
would certify as equal — and EXECUTES every member of every bucket against
deliberately adversarial DuckDB datasets: NULLs, float cancellation, integer
overflow, ties, empty tables, and separate databases with different data
under the same short table name. Any two bucket members whose results differ
on any dataset are a false certification, found by running the SQL rather
than by trusting the engine's account of it.

Cross-bucket collisions are the point: every query is compared with every
other, not only with the variants it was written beside. A self-test runs
pairs KNOWN to differ and requires the datasets to tell them apart, so a
quiet oracle means the datasets are strong enough to have spoken.

Scope, stated plainly: DuckDB execution of DuckDB-dialect SQL. The other
dialects in the benchmark matrix are not executed here.
"""

from __future__ import annotations

import decimal
import math
from dataclasses import dataclass, field
from itertools import product

from semantix.extraction.canonicalize import canonicalize_sql
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import PROOF_LEVELS, Provenance

DIALECT = "duckdb"

# --------------------------------------------------------------- estates ----

_ORDER_COLS = ("order_id INTEGER, customer_id INTEGER, amount DOUBLE, refund_amount DOUBLE, "
               "a DOUBLE, b DOUBLE, c DOUBLE, qty INTEGER, price DECIMAL(12,2), "
               "status VARCHAR, channel VARCHAR, order_date DATE")
_CUSTOMER_COLS = "customer_id INTEGER, segment VARCHAR, region VARCHAR"

MAIN_CATALOG = {
    "db.main.orders": {
        "order_id": "INTEGER", "customer_id": "INTEGER", "amount": "DOUBLE",
        "refund_amount": "DOUBLE", "a": "DOUBLE", "b": "DOUBLE", "c": "DOUBLE",
        "qty": "INTEGER", "price": "DECIMAL(12,2)", "status": "VARCHAR",
        "channel": "VARCHAR", "order_date": "DATE"},
    "db.main.customers": {"customer_id": "INTEGER", "segment": "VARCHAR",
                          "region": "VARCHAR"},
}

# A deliberately SHORT catalog entry, the shape that let prod and dev collapse.
MULTIDB_CATALOG = {"raw.orders": {"amount": "DOUBLE"}}


def _rows(table: str, rows: list[tuple]) -> list[str]:
    def lit(v):
        if v is None:
            return "NULL"
        if isinstance(v, str):
            return "'" + v.replace("'", "''") + "'"
        return repr(v) if isinstance(v, float) else str(v)
    return [f"INSERT INTO {table} VALUES ({', '.join(lit(v) for v in r)})" for r in rows]


_TYPICAL_ORDERS = [
    (1, 10, 100.0, 10.0, 1.5, 2.5, 3.5, 2, 9.99, "completed", "web", "2026-01-05"),
    (2, 11, 50.0, 0.0, 0.1, 0.2, 0.3, 1, 19.5, "pending", "app", "2026-01-20"),
    (3, 10, 75.0, 5.0, 2.0, -1.0, 4.0, 3, 5.0, "completed", "app", "2026-02-02"),
    (4, 12, 20.0, 20.0, 1.0, 1.0, 1.0, 1, 1.25, "cancelled", "web", "2026-02-15"),
    (5, 13, 300.0, 0.0, 10.0, 20.0, 30.0, 5, 100.0, "completed", "internal", "2026-03-01"),
]
_TYPICAL_CUSTOMERS = [(10, "smb", "eu"), (11, "ent", "us"), (12, "smb", "us"), (14, "ent", "eu")]

DATASETS: dict[str, tuple[list[tuple], list[tuple]]] = {
    "typical": (_TYPICAL_ORDERS, _TYPICAL_CUSTOMERS),
    "nulls": (
        [(1, 10, None, 10.0, None, 2.0, 3.0, None, None, "completed", "web", "2026-01-05"),
         (2, None, 50.0, None, 0.5, None, 1.0, 1, 2.5, None, "app", "2026-01-20"),
         (3, 10, 75.0, 5.0, 1.0, 1.0, None, 3, 5.0, "completed", None, None),
         (4, 12, 20.0, 20.0, 1.0, 1.0, 1.0, 1, 1.25, "pending", "web", "2026-02-15")],
        [(10, "smb", "eu"), (None, "smb", "us"), (12, None, "us")]),
    "float_cancellation": (
        [(1, 10, 1e16, 1.0, 1e16, -1e16, 1.0, 1, 1.0, "completed", "web", "2026-01-05"),
         (2, 11, -1e16, 1e16, 0.1, 0.2, 0.3, 1, 1.0, "completed", "app", "2026-01-06"),
         (3, 12, 1.0, -1e16, 1.0, 1e16, 1e16, 1, 1.0, "pending", "web", "2026-02-01")],
        [(10, "smb", "eu"), (11, "smb", "us"), (12, "ent", "us")]),
    # Kept apart: an overflow to infinity swamps every other row's cancellation.
    "float_overflow": (
        [(1, 10, 1e308, 0.0, 1e308, 1e308, -1e308, 1, 1.0, "completed", "web", "2026-01-05")],
        [(10, "smb", "eu")]),
    "int_extremes": (
        # qty + order_id overflows INT32; qty + customer_id + order_id does not.
        [(1000, -1000, 1.0, 0.0, 1.0, 1.0, 1.0, 2147483000, 1.0, "completed", "web", "2026-01-05")],
        [(-1000, "smb", "eu")]),
    "duplicates_ties": (
        [(1, 10, 50.0, 0.0, 1.0, 1.0, 1.0, 1, 1.0, "completed", "web", "2026-01-05"),
         (1, 10, 50.0, 0.0, 1.0, 1.0, 1.0, 1, 1.0, "completed", "web", "2026-01-05"),
         (2, 10, 50.0, 5.0, 2.0, 2.0, 2.0, 2, 2.0, "completed", "app", "2026-01-05"),
         (3, 11, 10.0, 0.0, 3.0, 3.0, 3.0, 3, 3.0, "pending", "app", "2026-02-05")],
        [(10, "smb", "eu"), (10, "ent", "eu"), (11, "smb", "us")]),
    "negatives_zero": (
        [(1, 10, -40.0, 10.0, -1.0, 0.0, 1.0, 0, 0.0, "completed", "web", "2026-01-05"),
         (2, 11, 0.0, 0.0, 0.0, -0.0, 0.0, -1, -2.5, "completed", "app", "2026-01-20"),
         (3, 12, 30.0, 45.0, 2.0, -2.0, 0.5, 4, 3.0, "refunded", "web", "2026-03-02")],
        [(10, "smb", "eu"), (11, "ent", "us"), (12, "smb", "us")]),
    "empty": ([], []),
}


def _main_setup(orders: list[tuple], customers: list[tuple]) -> list[str]:
    return ["ATTACH ':memory:' AS db",
            f"CREATE TABLE db.main.orders ({_ORDER_COLS})",
            f"CREATE TABLE db.main.customers ({_CUSTOMER_COLS})",
            *_rows("db.main.orders", orders), *_rows("db.main.customers", customers)]


def _multidb_setup() -> list[str]:
    stmts = []
    for db, values in (("prod", [100.0, 1.0]), ("dev", [900.0, 2.0])):
        stmts += [f"ATTACH ':memory:' AS {db}", f"CREATE SCHEMA {db}.raw",
                  f"CREATE TABLE {db}.raw.orders (amount DOUBLE)",
                  *_rows(f"{db}.raw.orders", [(v,) for v in values])]
    stmts += ["CREATE SCHEMA raw", "CREATE TABLE raw.orders (amount DOUBLE)",
              *_rows("raw.orders", [(5.0,)]),
              "CREATE TABLE main.orders (amount DOUBLE)", *_rows("main.orders", [(7.0,)])]
    return stmts


# --------------------------------------------------------------- queries ----

MEASURES = [
    "SUM(amount)", "SUM(amount - refund_amount)", "SUM(refund_amount - amount)",
    "SUM(a + b + c)", "SUM(a + c + b)", "SUM(b + a + c)", "SUM(c + b + a)",
    "SUM((a + b) + c)", "SUM(a + (b + c))", "SUM(b + c + a)",
    "SUM(a + b)", "SUM(b + a)", "SUM(a * b * c)", "SUM(a * c * b)", "SUM(c * (a * b))",
    "SUM(a + b - c)", "SUM(a + (b - c))", "SUM(a - c + b)", "SUM(a * b / c)", "SUM(a * (b / c))",
    "SUM(a - b - c)", "SUM(a - (b + c))",
    # Column operands, not literals: DuckDB constant-folds `qty + 1000 + -1000`
    # itself, which hid the overflow ordering from execution.
    "SUM(qty + order_id + customer_id)", "SUM(qty + customer_id + order_id)",
    "SUM(qty + (order_id + customer_id))",
    "SUM(qty * price)", "SUM(price * qty)",
    "COUNT(*)", "COUNT(1)", "COUNT(amount)", "COUNT(customer_id)", "COUNT(DISTINCT customer_id)",
    "AVG(amount)", "SUM(amount) / COUNT(*)", "SUM(amount) / NULLIF(COUNT(*), 0)",
    "SUM(amount) / COUNT(amount)", "MIN(amount)", "MAX(amount)",
    "SUM(CASE WHEN status = 'completed' THEN amount ELSE 0 END)",
    "SUM(CASE WHEN status = 'completed' THEN amount END)",
    "SUM(COALESCE(amount, 0))",
]
FILTERS = [
    "", "status = 'completed'", "'completed' = status", "NOT status <> 'completed'",
    "status IN ('completed', 'pending')", "status IN ('pending', 'completed')",
    "status = 'completed' OR status = 'pending'",
    "amount > 0", "NOT amount <= 0", "0 < amount", "amount >= 0",
    "amount > 0 AND status = 'completed'", "status = 'completed' AND amount > 0",
    "amount IS NOT NULL", "NOT amount IS NULL",
    "status <> 'cancelled'", "NOT status = 'cancelled'",
    "amount BETWEEN 1 AND 100", "amount >= 1 AND amount <= 100",
]
GRAINS = [
    ("", ""),
    ("status, ", "GROUP BY status"), ("status, ", "GROUP BY 1"),
    ("channel, ", "GROUP BY channel"),
    ("status, channel, ", "GROUP BY status, channel"), ("status, channel, ", "GROUP BY channel, status"),
    ("status, channel, ", "GROUP BY 1, 2"), ("status, channel, ", "GROUP BY 2, 1"),
    ("status, channel, ", "GROUP BY ALL"),
    ("status, channel, ", "GROUP BY ROLLUP (status, channel)"),
    ("status, channel, ", "GROUP BY ROLLUP (channel, status)"),
    ("status, channel, ", "GROUP BY ROLLUP (1, 2)"), ("status, channel, ", "GROUP BY ROLLUP (2, 1)"),
    ("status, channel, ", "GROUP BY CUBE (status, channel)"),
    ("status, channel, ", "GROUP BY CUBE (channel, status)"),
    ("status, channel, ", "GROUP BY GROUPING SETS ((status), (channel))"),
    ("status, channel, ", "GROUP BY GROUPING SETS ((1), (2))"),
    ("status, channel, ", "GROUP BY GROUPING SETS ((status, channel), ())"),
    ("DATE_TRUNC('month', order_date), ", "GROUP BY 1"),
    ("DATE_TRUNC('month', order_date), ", "GROUP BY DATE_TRUNC('month', order_date)"),
]

_O = "db.main.orders"
_C = "db.main.customers"
SPECIALS = [
    # joins: order, predicate placement, kind, direction, cartesian, self-join
    f"SELECT SUM(o.amount) AS m FROM {_O} o JOIN {_C} c ON o.customer_id = c.customer_id WHERE c.segment = 'smb'",
    f"SELECT SUM(o.amount) AS m FROM {_C} c JOIN {_O} o ON c.customer_id = o.customer_id WHERE c.segment = 'smb'",
    f"SELECT SUM(o.amount) AS m FROM {_O} o JOIN {_C} c ON o.customer_id = c.customer_id AND c.segment = 'smb'",
    f"SELECT SUM(o.amount) AS m FROM {_O} o, {_C} c WHERE o.customer_id = c.customer_id AND c.segment = 'smb'",
    f"SELECT SUM(o.amount) AS m FROM {_O} o LEFT JOIN {_C} c ON o.customer_id = c.customer_id AND c.segment = 'smb'",
    f"SELECT SUM(o.amount) AS m FROM {_O} o LEFT JOIN {_C} c ON o.customer_id = c.customer_id WHERE c.segment = 'smb'",
    f"SELECT SUM(o.amount) AS m FROM {_C} c LEFT JOIN {_O} o ON o.customer_id = c.customer_id",
    f"SELECT SUM(o.amount) AS m FROM {_O} o LEFT JOIN {_C} c ON o.customer_id = c.customer_id",
    f"SELECT SUM(o.amount) AS m FROM {_O} o JOIN {_C} c ON o.customer_id = c.customer_id",
    f"SELECT SUM(o.amount) AS m FROM {_O} o CROSS JOIN {_C} c",
    f"SELECT SUM(o.amount) AS m FROM {_O} o, {_C} c",
    f"SELECT SUM(o1.amount - o2.amount) AS m FROM {_O} o1 JOIN {_O} o2 ON o1.order_id = o2.order_id + 1",
    f"SELECT SUM(o2.amount - o1.amount) AS m FROM {_O} o1 JOIN {_O} o2 ON o1.order_id = o2.order_id + 1",
    # subqueries: IN / EXISTS / NOT IN / NOT EXISTS (NULL semantics differ)
    f"SELECT SUM(amount) AS m FROM {_O} WHERE customer_id IN (SELECT customer_id FROM {_C} WHERE segment = 'smb')",
    f"SELECT SUM(o.amount) AS m FROM {_O} o WHERE EXISTS (SELECT 1 FROM {_C} c WHERE c.customer_id = o.customer_id AND c.segment = 'smb')",
    f"SELECT SUM(amount) AS m FROM {_O} WHERE customer_id NOT IN (SELECT customer_id FROM {_C} WHERE segment = 'smb')",
    f"SELECT SUM(o.amount) AS m FROM {_O} o WHERE NOT EXISTS (SELECT 1 FROM {_C} c WHERE c.customer_id = o.customer_id AND c.segment = 'smb')",
    # order-sensitive consumers
    f"WITH s AS (SELECT amount FROM {_O} ORDER BY amount DESC) SELECT first(amount) AS m FROM s",
    f"WITH s AS (SELECT amount FROM {_O} ORDER BY amount ASC) SELECT first(amount) AS m FROM s",
    f"WITH S AS (SELECT amount FROM {_O} ORDER BY amount DESC) SELECT first(amount) AS m FROM s",
    f"WITH S AS (SELECT amount FROM {_O} ORDER BY amount ASC) SELECT first(amount) AS m FROM s",
    f"WITH d AS (SELECT DISTINCT ON (customer_id) customer_id, amount FROM {_O} ORDER BY customer_id, amount DESC) SELECT SUM(amount) AS m FROM d",
    f"WITH d AS (SELECT DISTINCT ON (customer_id) customer_id, amount FROM {_O} ORDER BY customer_id, amount ASC) SELECT SUM(amount) AS m FROM d",
    f"SELECT string_agg(status, ',' ORDER BY order_id) AS m FROM {_O}",
    f"SELECT string_agg(status, ',' ORDER BY order_id DESC) AS m FROM {_O}",
    # bounded measures
    f"SELECT SUM(amount) AS m FROM (SELECT amount FROM {_O} ORDER BY amount DESC LIMIT 2) t",
    f"SELECT SUM(amount) AS m FROM (SELECT amount FROM {_O} ORDER BY amount ASC LIMIT 2) t",
    f"SELECT SUM(amount) AS m FROM (SELECT amount FROM {_O} ORDER BY amount DESC LIMIT 2 OFFSET 1) t",
    f"SELECT SUM(amount) AS m FROM (SELECT amount FROM {_O} ORDER BY amount DESC NULLS LAST LIMIT 2) t",
    # distinct
    f"SELECT SUM(DISTINCT amount) AS m FROM {_O}",
    f"SELECT SUM(amount) AS m FROM (SELECT DISTINCT amount FROM {_O}) t",
    f"SELECT SUM(amount) AS m FROM (SELECT DISTINCT order_id, amount FROM {_O}) t",
    # under-qualified references (gapped without context; never bucketed)
    "SELECT SUM(amount) AS m FROM main.orders",
    "SELECT SUM(amount) AS m FROM orders",
]

MULTIDB_QUERIES = [
    "SELECT SUM(amount) AS m FROM prod.raw.orders",
    "SELECT SUM(amount) AS m FROM dev.raw.orders",
    "SELECT SUM(amount) AS m FROM PROD.RAW.ORDERS",
    "SELECT SUM(amount) AS m FROM raw.orders",
    "SELECT SUM(amount) AS m FROM orders",
]

# Pairs that MUST execute differently on at least one dataset — proof the
# datasets can expose the failure classes the oracle exists to catch.
SELF_TEST_PAIRS = [
    ("main", f"SELECT SUM(a + b + c) AS m FROM {_O}", f"SELECT SUM(a + c + b) AS m FROM {_O}"),
    ("main", f"SELECT SUM(qty + order_id + customer_id) AS m FROM {_O}",
             f"SELECT SUM(qty + customer_id + order_id) AS m FROM {_O}"),
    ("main", f"SELECT SUM(a + b - c) AS m FROM {_O}", f"SELECT SUM(a + (b - c)) AS m FROM {_O}"),
    ("main", f"SELECT status, channel, SUM(amount) AS m FROM {_O} GROUP BY ROLLUP (status, channel)",
             f"SELECT status, channel, SUM(amount) AS m FROM {_O} GROUP BY ROLLUP (channel, status)"),
    ("main", f"SELECT SUM(amount) AS m FROM {_O} WHERE customer_id NOT IN (SELECT customer_id FROM {_C} WHERE segment = 'smb')",
             f"SELECT SUM(o.amount) AS m FROM {_O} o WHERE NOT EXISTS (SELECT 1 FROM {_C} c WHERE c.customer_id = o.customer_id AND c.segment = 'smb')"),
    ("main", f"SELECT SUM(o.amount) AS m FROM {_O} o JOIN {_C} c ON o.customer_id = c.customer_id",
             f"SELECT SUM(o.amount) AS m FROM {_O} o LEFT JOIN {_C} c ON o.customer_id = c.customer_id"),
    ("main", f"SELECT SUM(amount) AS m FROM {_O}", f"SELECT SUM(DISTINCT amount) AS m FROM {_O}"),
    ("multidb", "SELECT SUM(amount) AS m FROM prod.raw.orders", "SELECT SUM(amount) AS m FROM dev.raw.orders"),
]


def generate_queries(full: bool = False) -> list[str]:
    """Single-measure DuckDB queries over the main estate. The default set
    varies one dimension at a time from a few anchors; `full` takes the
    whole cross product (slow — for qualification runs)."""
    def q(grain_sel, measure, flt, group):
        where = f" WHERE {flt}" if flt else ""
        return f"SELECT {grain_sel}{measure} AS m FROM {_O}{where}{(' ' + group) if group else ''}"

    out: list[str] = []
    if full:
        for (gs, gc), m, f in product(GRAINS, MEASURES, FILTERS):
            out.append(q(gs, m, f, gc))
    else:
        for m, f in product(MEASURES, ["", "status = 'completed'", "amount > 0 AND status = 'completed'"]):
            out.append(q("", m, f, ""))
        for m, f in product(["SUM(amount)", "COUNT(*)", "SUM(a + b + c)", "COUNT(DISTINCT customer_id)"], FILTERS):
            out.append(q("", m, f, ""))
        for (gs, gc), m, f in product(GRAINS, ["SUM(amount)", "COUNT(DISTINCT customer_id)", "AVG(amount)"],
                                      ["", "status = 'completed'"]):
            out.append(q(gs, m, f, gc))
    out += SPECIALS
    return list(dict.fromkeys(out))


# -------------------------------------------------------------- execution ---

def _connect(estate: str, dataset: str | None):
    import duckdb

    con = duckdb.connect()
    con.execute("SET threads TO 1")
    stmts = _multidb_setup() if estate == "multidb" else _main_setup(*DATASETS[dataset])
    for stmt in stmts:
        con.execute(stmt)
    return con


def _norm(v):
    """Exact normal form for comparison. Integers, decimals AND floats become
    their exact Decimal value (a float's exact binary expansion), so
    1000000001 and 1000000002 differ, 0.1 as DOUBLE differs from 0.1 as
    DECIMAL, and 5 equals 5.0. The first comparator rounded every number to
    nine significant digits and treated 1,000,000,001 and 1,000,000,002 as
    equal — a harness that can call different results the same cannot vouch
    for anything. NaN and infinities are named, since they have no Decimal
    order."""
    if v is None or isinstance(v, (bool, str)):
        return v
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "+inf" if v > 0 else "-inf"
        return decimal.Decimal(v).normalize()
    if isinstance(v, int):
        return decimal.Decimal(v)
    if isinstance(v, decimal.Decimal):
        if v.is_nan():
            return "NaN"
        if v.is_infinite():
            return "+inf" if v > 0 else "-inf"
        return v.normalize()
    return str(v)


def _key(row: tuple) -> str:
    return repr(tuple(str(v) if isinstance(v, decimal.Decimal) else v for v in row))


def _run(con, sql: str):
    try:
        rows = con.execute(sql).fetchall()
    except Exception as e:  # noqa: BLE001 — an error IS a result to compare
        return ("error", type(e).__name__)
    norm = [tuple(_norm(v) for v in row) for row in rows]
    return ("rows", sorted(norm, key=_key))


def _same(r1, r2) -> bool:
    """EXACT agreement. No numeric tolerance of any kind: execution runs
    single-threaded, so summation order is the scan order and equivalent
    queries are expected to agree bit-for-bit."""
    if r1[0] == "error" or r2[0] == "error":
        return r1[0] == r2[0]
    rows1, rows2 = r1[1], r2[1]
    if rows1 and rows2 and len(rows1[0]) != len(rows2[0]):
        # Different projection widths: compare the measure (last column).
        return (sorted((r[-1] for r in rows1), key=lambda v: _key((v,)))
                == sorted((r[-1] for r in rows2), key=lambda v: _key((v,))))
    return rows1 == rows2


def _datasets_for(estate: str) -> list[str | None]:
    return [None] if estate == "multidb" else list(DATASETS)


# ----------------------------------------------------------------- oracle ---

@dataclass
class OracleReport:
    queries: int = 0
    proof_grade: int = 0
    buckets: int = 0
    pairs_executed: int = 0
    datasets: int = 0
    mismatches: list[dict] = field(default_factory=list)
    self_test_failures: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.self_test_failures


def _registry(estate: str, queries: list[str]) -> SchemaRegistry:
    import sqlglot
    from sqlglot import exp

    from semantix.extraction.sqlutil import table_fqname

    catalog = MULTIDB_CATALOG if estate == "multidb" else MAIN_CATALOG
    registry = SchemaRegistry(catalog)
    refs = []
    for sql in queries:
        try:
            refs += [table_fqname(t) for t in sqlglot.parse_one(sql, read=DIALECT).find_all(exp.Table)]
        except Exception:  # noqa: BLE001
            continue
    registry.set_estate_depth(refs)
    return registry


def _buckets(estate: str, queries: list[str]) -> tuple[dict[str, list[str]], int]:
    registry = _registry(estate, queries)
    schema = registry.sqlglot_schema(DIALECT)
    by_hash: dict[str, list[str]] = {}
    proof = 0
    for i, sql in enumerate(queries):
        canon = canonicalize_sql(sql, schema, DIALECT)
        fps = extract_fingerprints(canon, "oracle.q", Provenance("oracle", f"q{i}"),
                                   DIALECT, registry)
        if len(fps) != 1 or fps[0].canon_level not in PROOF_LEVELS or fps[0].gaps:
            continue
        proof += 1
        by_hash.setdefault(fps[0].core.hash(), []).append(sql)
    return by_hash, proof


def run_oracle(full: bool = False) -> OracleReport:
    report = OracleReport()
    for estate, queries in (("main", generate_queries(full)), ("multidb", MULTIDB_QUERIES)):
        by_hash, proof = _buckets(estate, queries)
        report.queries += len(queries)
        report.proof_grade += proof
        shared = {h: qs for h, qs in by_hash.items() if len(qs) > 1}
        report.buckets += len(shared)
        datasets = _datasets_for(estate)
        report.datasets += len(datasets)
        for dataset in datasets:
            con = _connect(estate, dataset)
            try:
                for h, qs in shared.items():
                    first = _run(con, qs[0])
                    for other in qs[1:]:
                        report.pairs_executed += 1
                        result = _run(con, other)
                        if not _same(first, result):
                            report.mismatches.append({
                                "hash": h[:12], "estate": estate, "dataset": dataset,
                                "a": qs[0], "b": other,
                                "result_a": first, "result_b": result})
            finally:
                con.close()
    for estate, a, b in SELF_TEST_PAIRS:
        differs = False
        for dataset in _datasets_for(estate):
            con = _connect(estate, dataset)
            try:
                if not _same(_run(con, a), _run(con, b)):
                    differs = True
                    break
            finally:
                con.close()
        if not differs:
            report.self_test_failures.append({"estate": estate, "a": a, "b": b,
                                              "why": "no dataset tells these apart"})
    return report
