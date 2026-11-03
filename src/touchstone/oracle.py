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

from touchstone.extraction.canonicalize import canonicalize
from touchstone.extraction.fingerprint import extract_fingerprints
from touchstone.extraction.registry import SchemaRegistry
from touchstone.models import PROOF_LEVELS, Provenance

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
    # ordering through another projection (alias / ordinal) under a bound:
    # equivalent spellings must bucket together and execute identically
    f"SELECT channel AS k, SUM(amount) AS m FROM {_O} GROUP BY channel ORDER BY k LIMIT 1",
    f"SELECT channel AS k, SUM(amount) AS m FROM {_O} GROUP BY channel ORDER BY channel LIMIT 1",
    f"SELECT channel AS k, SUM(amount) AS m FROM {_O} GROUP BY channel ORDER BY 1 LIMIT 1",
    f"SELECT channel AS k, SUM(amount) AS m FROM {_O} GROUP BY channel ORDER BY k DESC LIMIT 1",
    # plain DISTINCT over different projection tuples
    f"SELECT DISTINCT SUM(amount) AS m FROM {_O} GROUP BY status",
    f"SELECT DISTINCT status, SUM(amount) AS m FROM {_O} GROUP BY status",
    # distinct
    f"SELECT SUM(DISTINCT amount) AS m FROM {_O}",
    f"SELECT SUM(amount) AS m FROM (SELECT DISTINCT amount FROM {_O}) t",
    f"SELECT SUM(amount) AS m FROM (SELECT DISTINCT order_id, amount FROM {_O}) t",
    # under-qualified references (gapped without context; never bucketed)
    "SELECT SUM(amount) AS m FROM main.orders",
    "SELECT SUM(amount) AS m FROM orders",
]

# The proof path's `inline` rule substitutes select-project-filter CTEs and
# derived tables into their consumer. Each spelling below is executed against
# the direct query it claims to equal, and the traps — renamed and SWAPPED
# columns, a computed projection under a tighter operator, filters on both
# sides, a chain of two CTEs — must bucket apart from what they do not equal.
INLINE_VARIANTS = [
    f"WITH s AS (SELECT * FROM {_O}) SELECT SUM(amount) AS m FROM s WHERE status = 'completed'",
    f"WITH s AS (SELECT * FROM {_O} WHERE status = 'completed') SELECT SUM(amount) AS m FROM s",
    f"SELECT SUM(amount) AS m FROM (SELECT * FROM {_O} WHERE status = 'completed') t",
    f"SELECT SUM(t.amount) AS m FROM (SELECT amount, status FROM {_O}) AS t WHERE t.status = 'completed'",
    f"WITH s AS (SELECT amount AS amt, refund_amount AS r, status AS st FROM {_O}) "
    f"SELECT SUM(amt - r) AS m FROM s WHERE st = 'completed'",
    f"WITH s AS (SELECT amount - refund_amount AS net, status FROM {_O}) "
    f"SELECT SUM(net) AS m FROM s WHERE status = 'completed'",
    # swapped names: computes refund - amount
    f"WITH s AS (SELECT amount AS refund_amount, refund_amount AS amount, status FROM {_O}) "
    f"SELECT SUM(amount - refund_amount) AS m FROM s WHERE status = 'completed'",
    # precedence: (a + b) * c, never a + b * c
    f"WITH s AS (SELECT a + b AS ab, c FROM {_O}) SELECT SUM(ab * c) AS m FROM s",
    f"SELECT SUM((a + b) * c) AS m FROM {_O}",
    f"SELECT SUM(a + b * c) AS m FROM {_O}",
    f"WITH s AS (SELECT a - b AS d, c FROM {_O}) SELECT SUM(c - d) AS m FROM s",
    f"SELECT SUM(c - (a - b)) AS m FROM {_O}",
    f"SELECT SUM(c - a - b) AS m FROM {_O}",
    # NOT over a substituted comparison
    f"WITH s AS (SELECT amount, status = 'completed' AS done FROM {_O}) "
    f"SELECT SUM(amount) AS m FROM s WHERE NOT done",
    f"SELECT SUM(amount) AS m FROM {_O} WHERE NOT status = 'completed'",
    # filters on both sides, and a chain
    f"WITH s AS (SELECT * FROM {_O} WHERE amount > 0) "
    f"SELECT SUM(amount) AS m FROM s WHERE status = 'completed'",
    f"SELECT SUM(amount) AS m FROM {_O} WHERE amount > 0 AND status = 'completed'",
    f"WITH s AS (SELECT * FROM {_O} WHERE amount > 0), u AS (SELECT * FROM s WHERE "
    f"status = 'completed') SELECT SUM(amount) AS m FROM u",
    # grouping through an inlined projection
    f"WITH s AS (SELECT channel AS ch, amount FROM {_O}) SELECT ch, SUM(amount) AS m FROM s GROUP BY ch",
    f"SELECT channel, SUM(amount) AS m FROM {_O} GROUP BY channel",
    f"WITH s AS (SELECT DATE_TRUNC('month', order_date) AS mo, amount FROM {_O}) "
    f"SELECT mo, SUM(amount) AS m FROM s GROUP BY 1",
    # HAVING over an output alias
    f"SELECT channel, SUM(amount) AS m FROM {_O} GROUP BY channel HAVING m > 60",
    f"SELECT channel, SUM(amount) AS m FROM {_O} GROUP BY channel HAVING SUM(amount) > 60",
    # NULL-sensitive expressions are preserved, not simplified
    f"SELECT SUM(CASE WHEN amount > 0 THEN amount END) AS m FROM {_O}",
    f"SELECT SUM(amount) AS m FROM {_O} WHERE NOT (amount <= 0)",
    f"SELECT COUNT(*) AS m FROM {_O} WHERE amount = amount",
    f"SELECT COUNT(*) AS m FROM {_O}",
    f"SELECT SUM(amount) AS m FROM {_O} WHERE amount BETWEEN 1 AND 100",
    f"SELECT SUM(amount) AS m FROM {_O} WHERE NOT amount BETWEEN 1 AND 100",
    f"SELECT SUM(amount) AS m FROM {_O} WHERE amount < 1 OR amount > 100",
    # an aggregate only in ORDER BY makes a one-row aggregate (sweep 2, #3)
    f"SELECT COUNT(*) AS m FROM (SELECT 'all' AS seg FROM {_O} ORDER BY SUM(amount)) t",
    f"SELECT COUNT(*) AS m FROM {_O}",
    f"SELECT COUNT(*) AS m FROM (SELECT 1 AS k FROM {_O} HAVING TRUE) t",
    f"SELECT COUNT(*) AS m FROM {_O} GROUP BY 'x'",
    f"SELECT COUNT(*) AS m FROM {_O} GROUP BY ()",
]

# Round seventeen's blind spots: the generators never wrote `%`, a chain of
# comparison / IS operators, a star modifier, a nested WITH, an inner ORDER BY
# or a constant GROUP BY key — which is why the reviews, not the oracle, found
# them. Each is refused today; listed so that anything which ever lets one in
# is executed against what it is equated with, and its proof form.
ROUND17_VARIANTS = [
    f"SELECT SUM(qty + 1 % 3) AS m FROM {_O}", f"SELECT SUM((qty + 1) % 3) AS m FROM {_O}",
    f"SELECT SUM(qty % 3 * 2) AS m FROM {_O}", f"SELECT SUM(qty % (3 * 2)) AS m FROM {_O}",
    f"SELECT COUNT(*) AS m FROM {_O} WHERE qty = 1 IS NOT TRUE",
    f"SELECT COUNT(*) AS m FROM {_O} WHERE qty = (1 IS NOT TRUE)",
    f"SELECT COUNT(*) AS m FROM {_O} WHERE (qty = 1) IS NOT TRUE",
    f"SELECT COUNT(*) FILTER (WHERE qty = customer_id IS NULL) AS m FROM {_O}",
    f"SELECT COUNT(*) FILTER (WHERE (customer_id IS NULL) = qty) AS m FROM {_O}",
    f"SELECT COUNT(*) AS m FROM {_O} WHERE NOT qty IN (1, 2) IN (TRUE, NULL)",
    f"SELECT COUNT(*) AS m FROM {_O} WHERE (NOT qty IN (1, 2)) IN (TRUE, NULL)",
    f"WITH s AS (SELECT * REPLACE (refund_amount AS amount) FROM {_O}) "
    f"SELECT SUM(amount) AS m FROM s",
    f"WITH s AS (SELECT * EXCLUDE (refund_amount) FROM {_O}) SELECT SUM(amount) AS m FROM s",
    f"WITH s AS (SELECT * RENAME (refund_amount AS amount, amount AS refund_amount) "
    f"FROM {_O}) SELECT SUM(amount) AS m FROM s",
    f"SELECT *, COUNT(*) AS m FROM (SELECT channel FROM {_O}) d GROUP BY ALL",
    f"WITH s AS (SELECT amount FROM {_O}) SELECT SUM(amount) AS m FROM "
    f"(WITH s AS (SELECT refund_amount AS amount FROM {_O}) SELECT amount FROM s) d",
    f"WITH s AS (SELECT amount FROM {_O} ORDER BY amount) SELECT SUM(amount) AS m FROM s",
    f"SELECT AVG(amount) AS m FROM (SELECT amount FROM {_O} ORDER BY qty) t",
    f"SELECT channel, SUM(amount) AS m FROM {_O} GROUP BY (1)",
    f"SELECT qty, SUM(amount) AS m FROM {_O} GROUP BY (1)",
    f"SELECT SUM(amount) AS m, 1 AS k FROM {_O} GROUP BY k",
    f"WITH s AS (SELECT 'year' AS month, order_date, amount FROM {_O}) "
    f"SELECT DATE_TRUNC(month, order_date) AS k, SUM(amount) AS m FROM s GROUP BY 1",
    f"SELECT DATE_TRUNC('month', order_date) AS k, SUM(amount) AS m FROM {_O} GROUP BY 1",
    f"SELECT COUNT(*) FILTER (WHERE like(status, 'comp%')) AS m FROM {_O}",
    f"SELECT COUNT(*) FILTER (WHERE status LIKE 'comp%') AS m FROM {_O}",
    f"SELECT COUNT(*) AS m FROM {_O} WHERE qty BETWEEN 2.5 AND '2.5'",
    f"SELECT SUM(-qty ^ 2) AS m FROM {_O}", f"SELECT SUM(POWER(-qty, 2)) AS m FROM {_O}",
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
    ("main", f"SELECT channel AS k, SUM(amount) AS m FROM {_O} GROUP BY channel ORDER BY k LIMIT 1",
             f"SELECT channel AS k, SUM(amount) AS m FROM {_O} GROUP BY channel ORDER BY k DESC LIMIT 1"),
    ("multidb", "SELECT SUM(amount) AS m FROM prod.raw.orders", "SELECT SUM(amount) AS m FROM dev.raw.orders"),
]


# ------------------------------------------- combinations inside the grammar
#
# touchstone-proof/2's rules compose: a renamed CTE column under BETWEEN under
# an output-alias GROUP BY. Each wrapper reads the same orders table through
# a different source chain; each placeholder is a base column, spelled as the
# wrapper exposes it. Every combination is executed against its own proof
# form, and every pair the engine equates against each other — including
# across wrappers, so `SUM(amt) … FROM s` (renamed) must bucket with, and
# execute as, `SUM(amount) … FROM orders`.

_BASE_COLS = ("order_id", "customer_id", "amount", "refund_amount", "a", "b", "c",
              "qty", "price", "status", "channel", "order_date")
_RENAMED = {c: f"r_{c}" for c in _BASE_COLS}
_SWAPPED = {**{c: c for c in _BASE_COLS}, "amount": "refund_amount",
            "refund_amount": "amount", "a": "b", "b": "a"}


def _project(names: dict[str, str]) -> str:
    return ", ".join(f"{c} AS {names[c]}" for c in _BASE_COLS)


GRAMMAR_WRAPPERS: dict[str, tuple[str, str, dict[str, str]]] = {
    # name: (WITH prefix, FROM relation, base column -> visible name)
    "direct": ("", _O, {c: c for c in _BASE_COLS}),
    "cte_star": (f"WITH s AS (SELECT * FROM {_O}) ", "s", {c: c for c in _BASE_COLS}),
    "cte_rename": (f"WITH s AS (SELECT {_project(_RENAMED)} FROM {_O}) ", "s", _RENAMED),
    # the trap: visible `amount` IS refund_amount
    "cte_swap": (f"WITH s AS (SELECT {_project(_SWAPPED)} FROM {_O}) ", "s",
                 {v: k for k, v in _SWAPPED.items()}),
    "cte_filter": (f"WITH s AS (SELECT * FROM {_O} WHERE order_id > 1) ", "s",
                   {c: c for c in _BASE_COLS}),
    "derived": ("", f"(SELECT * FROM {_O}) AS t", {c: c for c in _BASE_COLS}),
    "chain": (f"WITH s AS (SELECT * FROM {_O}), u AS (SELECT {_project(_RENAMED)} FROM s) ",
              "u", _RENAMED),
    "cte_computed": (f"WITH s AS (SELECT *, amount - refund_amount AS net FROM {_O}) ", "s",
                     {**{c: c for c in _BASE_COLS}, "net": "net"}),
}
GRAMMAR_MEASURES = [
    "SUM({amount})", "SUM({amount} - {refund_amount})", "SUM({a} + {b} * {c})",
    "SUM(({a} + {b}) * {c})", "SUM({a} - ({b} - {c}))", "COUNT(*)", "COUNT({customer_id})",
    "COUNT(DISTINCT {customer_id})", "AVG({amount})", "MIN({amount})", "MAX({amount})",
    "SUM(CASE WHEN {status} = 'completed' THEN {amount} END)",
    "SUM({amount}) FILTER (WHERE {channel} = 'web')", "SUM({qty} * {price})",
    "SUM({amount}) / NULLIF(COUNT(*), 0)", "-SUM({amount})",
]
GRAMMAR_FILTERS = [
    "", "{status} = 'completed'", "NOT {status} = 'completed'", "(({status} = 'completed'))",
    "{amount} BETWEEN 1 AND 100", "NOT {amount} BETWEEN 1 AND 100",
    "({status} = 'completed' OR {channel} = 'web')", "{status} IN ('completed', 'pending')",
    "{amount} IS NOT NULL AND {channel} <> 'app'", "NOT ({amount} > 10 AND {channel} = 'web')",
]
GRAMMAR_GRAINS = [
    ("", ""), ("{status}, ", "GROUP BY {status}"), ("{status} AS k, ", "GROUP BY 1"),
    ("{status} AS k, ", "GROUP BY k"), ("{status}, {channel}, ", "GROUP BY ALL"),
    ("DATE_TRUNC('month', {order_date}) AS mo, ", "GROUP BY mo"),
    ("{status}, ", "GROUP BY {status} HAVING COUNT(*) > 1"),
]


def generate_grammar_combinations(full: bool = False) -> list[str]:
    def q(wrapper, measure, flt, grain):
        prefix, rel, names = GRAMMAR_WRAPPERS[wrapper]
        fmt = {c: names.get(c, c) for c in _BASE_COLS}
        if wrapper == "cte_computed":
            measure = measure.replace("{amount} - {refund_amount}", "net")
        sel, group = grain
        where = f" WHERE {flt.format(**fmt)}" if flt else ""
        return (f"{prefix}SELECT {sel.format(**fmt)}{measure.format(**fmt)} AS m "
                f"FROM {rel}{where}{(' ' + group.format(**fmt)) if group else ''}")

    out: list[str] = []
    wrappers = list(GRAMMAR_WRAPPERS)
    if full:
        for w, m, f, g in product(wrappers, GRAMMAR_MEASURES, GRAMMAR_FILTERS, GRAMMAR_GRAINS):
            out.append(q(w, m, f, g))
    else:
        # Every wrapper × measure, with filters and groupings rotated so each
        # appears with every wrapper; plus every filter and grain over two
        # anchors.
        for i, (w, m) in enumerate(product(wrappers, GRAMMAR_MEASURES)):
            out.append(q(w, m, GRAMMAR_FILTERS[i % len(GRAMMAR_FILTERS)],
                         GRAMMAR_GRAINS[i % len(GRAMMAR_GRAINS)]))
        for w, f, g in product(wrappers, GRAMMAR_FILTERS, GRAMMAR_GRAINS[:3]):
            out.append(q(w, "SUM({amount} - {refund_amount})", f, g))
    return list(dict.fromkeys(out))


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
    out += SPECIALS + INLINE_VARIANTS + ROUND17_VARIANTS + generate_grammar_combinations(full)
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
        return _exact(decimal.Decimal(v))
    if isinstance(v, int):
        return _exact(decimal.Decimal(v))
    if isinstance(v, decimal.Decimal):
        if v.is_nan():
            return "NaN"
        if v.is_infinite():
            return "+inf" if v > 0 else "-inf"
        return _exact(v)
    return str(v)


def _exact(d: decimal.Decimal) -> tuple:
    """A Decimal's exact value as (tag, sign, digits, exponent) with trailing
    zeros stripped — so 1.10 == 1.1 and 5 == 5.0 — computed from its digit
    tuple, never through the decimal CONTEXT. Decimal.normalize() rounds to
    the context precision (28 digits by default) and made the DECIMAL(38,28)
    values 1.0000000000000000000000000001 and …0002 compare equal. Both
    zeros are one zero. The tag keeps a number from ever equalling a string."""
    sign, digits, exponent = d.as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if digits == [0]:
        return ("num", 0, (0,), 0)
    return ("num", sign, tuple(digits), exponent)


def _key(row: tuple) -> str:
    return repr(tuple(row))


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
    # Every proof-grade query is also executed against its OWN trusted
    # transformed SQL (the proof form): the rules that produced it are
    # challenged one query at a time, not only through bucket pairs.
    transforms_executed: int = 0
    transform_mismatches: list[dict] = field(default_factory=list)
    # The parser-disagreement detector on every query's ORIGINAL text, by
    # status; a proof-grade query it says DuckDB reads differently fails the
    # run (canonicalize refuses those, so any here is a broken refusal).
    parse_checks: dict[str, int] = field(default_factory=dict)
    parse_disagreements: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.mismatches or self.self_test_failures or self.transform_mismatches
                    or self.parse_disagreements)


def _registry(estate: str, queries: list[str]) -> SchemaRegistry:
    import sqlglot
    from sqlglot import exp

    from touchstone.extraction.sqlutil import table_fqname

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


_TRANSFORMED: dict[str, str] = {}   # original SQL -> its proof form, per run


_PARSE_CHECKS: dict[str, tuple[dict, bool]] = {}   # original SQL -> (check, proof grade)


def _buckets(estate: str, queries: list[str]) -> tuple[dict[str, list[str]], int]:
    from touchstone.extraction.parse_agreement import check_parse

    registry = _registry(estate, queries)
    by_hash: dict[str, list[str]] = {}
    proof = 0
    for i, sql in enumerate(queries):
        canon = canonicalize(sql, registry, DIALECT)
        fps = extract_fingerprints(canon, "oracle.q", Provenance("oracle", f"q{i}"),
                                   DIALECT, registry)
        graded = len(fps) == 1 and fps[0].canon_level in PROOF_LEVELS and not fps[0].gaps
        _PARSE_CHECKS[sql] = (canon.parse_check or check_parse(sql, DIALECT).as_dict(), graded)
        if not graded:
            continue
        proof += 1
        by_hash.setdefault(fps[0].core.hash(), []).append(sql)
        _TRANSFORMED[sql] = canon.ast.sql(dialect=DIALECT)
    return by_hash, proof


def run_oracle(full: bool = False) -> OracleReport:
    report = OracleReport()
    for estate, queries in (("main", generate_queries(full)), ("multidb", MULTIDB_QUERIES)):
        _TRANSFORMED.clear()
        _PARSE_CHECKS.clear()
        by_hash, proof = _buckets(estate, queries)
        transformed = dict(_TRANSFORMED)
        from touchstone.extraction.parse_agreement import DISAGREES

        for sql, (check, graded) in _PARSE_CHECKS.items():
            report.parse_checks[check["status"]] = report.parse_checks.get(
                check["status"], 0) + 1
            if graded and check["status"] == DISAGREES:
                report.parse_disagreements.append({"estate": estate, "sql": sql,
                                                   "detail": check["detail"]})
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
                for original, proof_form in transformed.items():
                    report.transforms_executed += 1
                    r_orig, r_proof = _run(con, original), _run(con, proof_form)
                    # Exact, errors included: the same error class on both
                    # sides is agreement (an overflow is a result), while a
                    # proof form that fails where the original runs is not.
                    if r_orig != r_proof:
                        report.transform_mismatches.append({
                            "estate": estate, "dataset": dataset, "original": original,
                            "transformed": proof_form, "result_original": r_orig,
                            "result_transformed": r_proof})
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


# --------------------------------------------- conformance challenger ------
#
# The monitor's CONFORMING verdict comes from bounded rules
# (services.conformance), not from execution. This section challenges those
# rules: every query the rules call a conforming variation of an authority
# is executed against the expected query — the authority's measure over its
# base table under its required filters AND the variation the rules
# identified — on every adversarial dataset, exactly. A disagreement means
# the rules mis-decomposed the query (a conjunct that was not one, a
# grouping term that was not a dimension, NULL semantics). Generated near
# misses — weakened, dropped, broadened or replaced required filters,
# transformed or changed measures, undeclared slices, unsupported shapes —
# must never come out conforming.
#
# What agreement establishes, stated plainly: that on these datasets the
# rules' decomposition held. It does not prove the rules correct in general,
# and it says nothing about whether a slice is PERMITTED — that is policy,
# covered by the acceptance tests. Shape B (MEASURE() over a metric view) is
# Databricks-only and is not executed here.

CONFORMANCE_AUTHORITIES = [
    {"name": "net_revenue", "expr": "SUM(amount) - SUM(refund_amount)",
     "filters": ["status = 'completed'"],
     "dimensions": ["channel", "order_date", "customer_id", "qty",
                    ("month", "DATE_TRUNC('MONTH', order_date)")]},
    {"name": "units", "expr": "SUM(qty)", "filters": ["status <> 'cancelled'"],
     "dimensions": ["channel", "order_date", "customer_id"]},
    {"name": "orders_count", "expr": "COUNT(*)",
     "filters": ["status = 'completed'", "channel IS NOT NULL"],
     "dimensions": ["channel", "order_date"]},
]
CONFORMANCE_SLICES = [
    "channel = 'web'", "channel IN ('web', 'app')", "NOT channel = 'web'",
    "channel IS NULL", "channel IS NOT NULL", "channel <> 'app'",
    "customer_id BETWEEN 10 AND 11", "customer_id IN (10, 12)",
    "order_date >= DATE '2026-02-01'", "order_date < CAST('2026-03-01' AS DATE)",
    "DATE_TRUNC('MONTH', order_date) = DATE '2026-01-01'",
    "(channel = 'web' OR customer_id = 11)", "NOT (channel = 'web' AND customer_id = 10)",
    "qty > 1", "qty IS NULL",
    "order_date >= CURRENT_DATE - INTERVAL 36500 DAY",
]
CONFORMANCE_GRAINS = [
    [], ["channel"], ["DATE_TRUNC('MONTH', order_date)"], ["channel", "customer_id"],
    ["order_date"], ["customer_id", "DATE_TRUNC('MONTH', order_date)"],
]


@dataclass
class ConformanceReport:
    queries: int = 0
    conforming: int = 0
    executions: int = 0
    near_misses: int = 0
    near_miss_conformed: list[dict] = field(default_factory=list)
    # Generated ordinary cases are the authority's own definition under
    # declared-dimension slices and groupings — legitimate by construction —
    # so a VIOLATION verdict on one is a false accusation.
    false_accusations: list[dict] = field(default_factory=list)
    mismatches: list[dict] = field(default_factory=list)
    self_test_failures: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.near_miss_conformed or self.false_accusations
                    or self.mismatches or self.self_test_failures)


def _conformance_authorities():
    from touchstone.adapters.base import DeclaredSpec, DimensionSpec
    from touchstone.engine import build_claim
    from touchstone.services.conformance import Authority

    registry = SchemaRegistry(MAIN_CATALOG)
    registry.set_estate_depth([_O])
    out = []
    for a in CONFORMANCE_AUTHORITIES:
        dims = []
        for d in a["dimensions"]:
            if isinstance(d, tuple):
                dims.append(DimensionSpec(name=d[0], expr=d[1], column="order_date",
                                          truncation="MONTH", supported=True))
            else:
                dims.append(DimensionSpec(name=d, expr=d, column=d, supported=True))
        spec = DeclaredSpec(system="oracle", name=a["name"], model=_O, expr=a["expr"],
                            filters=list(a["filters"]), dimensions=dims)
        claim = build_claim(spec, registry, DIALECT)
        if not claim.expressible or claim.core is None:
            raise RuntimeError(f"oracle authority {a['name']} is not expressible: {claim.error}")
        out.append((a, Authority(concept=a["name"], system="oracle", name=a["name"],
                                 core=claim.core, dimensions=dims)))
    return registry, out


def _dimension_columns(a: dict) -> set[str]:
    return {d if isinstance(d, str) else "order_date" for d in a["dimensions"]}


def _slice_columns(text: str) -> set[str]:
    import sqlglot
    from sqlglot import exp

    return {c.name.lower() for c in sqlglot.parse_one(text, read=DIALECT).find_all(exp.Column)}


def _qualified(text: str, alias: str) -> str:
    import sqlglot

    from touchstone.extraction.sqlutil import requalify_columns

    node = sqlglot.parse_one(text, read=DIALECT)
    requalify_columns(node, alias, only_qualified=False)
    return node.sql(dialect=DIALECT)


def _conformance_query(a: dict, grain: list[str], where: list[str], measure: str | None = None,
                       tail: str = "") -> str:
    sel = "".join(f"{g}, " for g in grain)
    sql = f"SELECT {sel}{measure or a['expr']} AS m FROM {_O}"
    if where:
        sql += " WHERE " + " AND ".join(f"({w})" for w in where)
    if grain:
        sql += " GROUP BY " + ", ".join(grain)
    return sql + tail


def _via_cte(a: dict, grain: list[str], inner: list[str], outer: list[str],
             measure: str | None = None, body: str = "*") -> str:
    """The same observed query, read through a select-project-filter CTE: the
    proof path inlines it, so conformance must come out as for the direct
    spelling — and execution checks that it does."""
    sql = _conformance_query(a, grain, outer, measure)
    where = (" WHERE " + " AND ".join(f"({w})" for w in inner)) if inner else ""
    return (f"WITH s AS (SELECT {body} FROM {_O}{where}) "
            + sql.replace(f"FROM {_O}", "FROM s", 1))


def generate_conformance_cases(full: bool = False) -> list[tuple[dict, str, bool]]:
    """(authority, observed SQL, must_not_conform) triples."""
    from itertools import combinations

    cases: list[tuple[dict, str, bool]] = []
    for a in CONFORMANCE_AUTHORITIES:
        dims = _dimension_columns(a)
        slices = [s for s in CONFORMANCE_SLICES if _slice_columns(s) <= dims]
        grains = [g for g in CONFORMANCE_GRAINS
                  if set().union(*(_slice_columns(t) for t in g)) <= dims]
        width = 3 if full else 2
        combos = [list(c) for n in range(width + 1) for c in combinations(slices, n)]
        if not full:
            combos = combos[: 1 + len(slices) + 12]
        required = list(a["filters"])
        for grain in grains:
            for combo in combos:
                cases.append((a, _conformance_query(a, grain, required + combo), False))
        for grain in grains[:2]:
            for combo in combos[:4]:
                cases.append((a, _via_cte(a, grain, required, combo), False))
                cases.append((a, _via_cte(a, grain, [], required + combo), False))
        # Required filters in other spellings: conform only if canonicalization
        # makes them the declared conjunct; executed either way when they do.
        for r in required:
            col, _, rest = r.partition(" ")
            if rest.startswith("= "):
                flipped = f"{rest[2:]} = {col}"
                cases.append((a, _conformance_query(a, [], [flipped] + [x for x in required if x != r]), False))
        # The required filter moved INTO the measure (FILTER / CASE) computes the
        # same numbers: it may be unverifiable, never a violation.
        if a["name"] == "net_revenue":
            for moved in ("SUM(amount) FILTER (WHERE status = 'completed') - "
                          "SUM(refund_amount) FILTER (WHERE status = 'completed')",
                          "SUM(CASE WHEN status = 'completed' THEN amount END) - "
                          "SUM(CASE WHEN status = 'completed' THEN refund_amount END)"):
                cases.append((a, _conformance_query(a, [], [], measure=moved), False))
                cases.append((a, _conformance_query(a, ["channel"], required, measure=moved),
                              False))
        # Near misses: none may conform.
        near: list[str] = []
        for i, r in enumerate(required):
            others = required[:i] + required[i + 1:]
            near.append(_conformance_query(a, [], others))                                  # dropped
            near.append(_conformance_query(a, [], [f"{r} OR channel = 'web'"] + others))   # OR-weakened
            near.append(_conformance_query(a, ["channel"], [f"{r} OR customer_id = 11"] + others))
            if r == "status = 'completed'":
                near.append(_conformance_query(a, [], ["status IN ('completed', 'pending')"] + others))
                near.append(_conformance_query(a, [], ["status = 'pending'"] + others))
            if r == "status <> 'cancelled'":
                near.append(_conformance_query(a, [], ["status <> 'pending'"] + others))
        for m in (f"({a['expr']}) * 2", f"ROUND({a['expr']}, 2)", f"{a['expr']} + 1"):
            near.append(_conformance_query(a, [], required, measure=m))
        changed = {"net_revenue": "SUM(amount - refund_amount)", "units": "SUM(qty * 1.0)",
                   "orders_count": "COUNT(channel)"}[a["name"]]
        near.append(_conformance_query(a, [], required, measure=changed))
        for undeclared in ("amount > 0", "refund_amount = 0", "status <> 'refunded'"):
            if _slice_columns(undeclared) <= dims:
                continue
            q = _conformance_query(a, [], required + [undeclared])
            if undeclared == "status <> 'refunded'" and "status = 'completed'" in required:
                # IMPLIED by the required filter, and the optimizer proves it
                # away: the core equals the definition's, so this one may
                # conform (exactly) — and is then executed like any other.
                cases.append((a, q, False))
            else:
                near.append(q)
        near.append(_conformance_query(a, ["channel"], required,
                                       tail=f" HAVING {a['expr']} > 0"))
        near.append(_conformance_query(a, [], required, tail=" LIMIT 1"))
        # Through a CTE: an undeclared condition hidden inside it, and (for
        # net_revenue) the two measure columns swapped by the CTE's aliases.
        near.append(_via_cte(a, [], ["refund_amount = 0"], required))
        if a["name"] == "net_revenue":
            near.append(_via_cte(a, [], [], required, body="refund_amount AS amount, "
                                 "amount AS refund_amount, status, channel"))
        near.append(f"SELECT {_qualified(a['expr'], 'o')} AS m "
                    f"FROM {_O} o JOIN {_C} c ON o.customer_id = c.customer_id "
                    f"WHERE " + " AND ".join(f"({_qualified(x, 'o')})" for x in required))
        cases += [(a, q, True) for q in near]
    return cases


def _expected_sql(auth, outcome) -> str:
    where = list(auth.core.filters) + list(outcome.variation["slices"])
    grain = list(outcome.variation["grain"])
    sql = f"SELECT {''.join(f'{g}, ' for g in grain)}{auth.core.measure_expr} AS m FROM {_O}"
    if where:
        sql += " WHERE " + " AND ".join(f"({w})" for w in where)
    if grain:
        sql += " GROUP BY " + ", ".join(grain)
    return sql


def _same_rows(r1, r2) -> bool:
    """Exact agreement up to column ORDER within a row: the expected query
    projects grouping terms in canonical order, the observed one in its own."""
    if r1[0] == "error" or r2[0] == "error":
        return r1[0] == r2[0]
    def rows(r):
        return sorted((tuple(sorted(row, key=lambda v: _key((v,)))) for row in r[1]), key=_key)
    return rows(r1) == rows(r2)


def run_conformance_oracle(full: bool = False) -> ConformanceReport:
    import sqlglot

    from touchstone.services.conformance import check_raw, raw_shape_problem

    from touchstone.extraction.proof import is_grammar_gap
    from touchstone.models import CanonLevel

    registry, authorities = _conformance_authorities()
    by_name = {a["name"]: auth for a, auth in authorities}
    report = ConformanceReport()
    to_execute: list[tuple[str, str, str]] = []   # (observed, expected, authority)
    for a, sql, must_not in generate_conformance_cases(full):
        report.queries += 1
        auth = by_name[a["name"]]
        canon = canonicalize(sql, registry, DIALECT)
        fps = extract_fingerprints(canon, "oracle.q", Provenance("oracle", "c"),
                                   DIALECT, registry)
        if len(fps) != 1:
            continue
        fp = fps[0]
        proof = fp.canon_level in PROOF_LEVELS and not fp.gaps
        # As the monitor does: a core outside the proof grammar is judged only
        # as a hypothesis (always inside a boundary); any other gap, not at all.
        hypothesis = (sorted(fp.gaps)[0] if not proof and fp.gaps
                      and all(is_grammar_gap(g) for g in fp.gaps) else None)
        if not proof and hypothesis is None:
            continue
        written = (canon.ast if canon.level == CanonLevel.SUPPORTED
                   else sqlglot.parse_one(sql, read=DIALECT))
        outcome = check_raw(fp.core, auth, DIALECT, raw_shape_problem(written),
                            frozenset(), hypothesis)
        if must_not:
            report.near_misses += 1
            if outcome.status == "conforming":
                report.near_miss_conformed.append({"authority": a["name"], "sql": sql,
                                                   "outcome": outcome.as_dict()})
            continue
        if outcome.status == "violation":
            report.false_accusations.append({"authority": a["name"], "sql": sql,
                                             "outcome": outcome.as_dict()})
        if outcome.status == "conforming":
            report.conforming += 1
            to_execute.append((sql, _expected_sql(auth, outcome), a["name"]))
    # Poisoned self-test: a dropped required filter, planted as conforming
    # with no variation, must be caught by execution.
    poison_obs = f"SELECT {CONFORMANCE_AUTHORITIES[0]['expr']} AS m FROM {_O}"
    poison_exp = _conformance_query(CONFORMANCE_AUTHORITIES[0], [],
                                    CONFORMANCE_AUTHORITIES[0]["filters"])
    poison_caught = False
    for dataset in DATASETS:
        con = _connect("main", dataset)
        try:
            for observed, expected, name in to_execute:
                report.executions += 1
                r_obs, r_exp = _run(con, observed), _run(con, expected)
                if not _same_rows(r_obs, r_exp):
                    report.mismatches.append({"authority": name, "dataset": dataset,
                                              "observed": observed, "expected": expected,
                                              "result_observed": r_obs,
                                              "result_expected": r_exp})
            if not _same_rows(_run(con, poison_obs), _run(con, poison_exp)):
                poison_caught = True
        finally:
            con.close()
    if not poison_caught:
        report.self_test_failures.append({"sql": poison_obs,
                                          "why": "a dropped required filter executed "
                                                 "identically on every dataset"})
    return report
