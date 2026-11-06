"""Build the v4 fresh test's answer key by execution - no person labels anything.

    python answer_key.py            # writes answer_key.jsonl next to this file

Three questions per statement, each answered by code:

  presented as   the output name: "revenue", or "revenue" qualified by a subset (owner
                 rule A3: eu_revenue, revenue_1996) presents the metric; a name that
                 states another quantity (avg_daily_revenue, revenue_before_discount)
                 is a separately named measure; a name without "revenue" does not
                 present it at all.
  what changed   the statement and its reference query (answer_key.yaml: the governed
                 measure over the governed rows, plus exactly the statement's own extra
                 filters and groupings) run in DuckDB on local TPC-H-shaped data, once
                 clean and once dirty (NULLs, unknown flag values, duplicate rows -
                 nothing the definition or the owner's rules exclude). A different result
                 on clean data is a changed definition; only on dirty data, a result
                 that depends on facts nobody stated.
  allowed        each extra filter or grouping is classed by the columns it reads:
                 declared dimensions, an owner rule (A1 NULL guards on the measure's
                 inputs, A4 part brand or type, P1 supplier nation or region, P2 one
                 named customer), or other attributes.

The outcome follows from the answers, in the order of the target behavior: not
presented as the metric -> no alert; a prohibited variation -> alert; a changed
definition -> alert; depends on unstated facts -> unresolved; a variation on other
attributes -> informational; otherwise no alert. Nothing here runs on a warehouse.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import duckdb
import sqlglot
import yaml
from sqlglot import exp

HERE = Path(__file__).parent
VIEW = yaml.safe_load((HERE / "pilot/views/revenue_mv.yaml").read_text())
METRIC = "revenue"
MEASURE = next(m["expr"] for m in VIEW["measures"] if m["name"] == METRIC)
# Words that make a "revenue" name state another quantity.
OTHER_QUANTITY = {"avg", "average", "mean", "median", "share", "pct", "percent", "ratio",
                  "rate", "growth", "change", "before", "excl", "incl", "tax", "discount",
                  "gross", "charge", "count", "per", "cumulative", "running"}
TREATMENT = {"separate_measure": "none", "prohibited_variation": "alert",
             "suspected_definition_change": "alert", "insufficient_evidence": "unresolved",
             "unreviewed_variation": "informational", "likely_permitted": "none"}


def _leaf(expr: str) -> str:
    """orders.customer.nation.n_name -> n_name: the definition's join paths, flattened."""
    return re.sub(r"\b(?:\w+\.)+(\w+)\b", r"\1", expr)


def _norm(sql: str) -> str:
    return sqlglot.parse_one(sql, read="duckdb").sql(dialect="duckdb")


def _columns(sql: str) -> set[str]:
    return {c.name for c in sqlglot.parse_one(sql, read="duckdb").find_all(exp.Column)}


DIMENSIONS = {_norm(_leaf(d["expr"])) for d in VIEW["dimensions"]}
DIMENSION_COLUMNS = set().union(*(_columns(_leaf(d["expr"])) for d in VIEW["dimensions"]))
REQUIRED = _columns(VIEW["filter"])
MEASURE_INPUTS = _columns(MEASURE)


# --- local TPC-H-shaped data --------------------------------------------------------

_NATIONS = ["ALGERIA", "ARGENTINA", "BRAZIL", "CANADA", "EGYPT", "ETHIOPIA", "FRANCE",
            "GERMANY", "INDIA", "INDONESIA", "IRAN", "IRAQ", "JAPAN", "JORDAN", "KENYA",
            "MOROCCO", "MOZAMBIQUE", "PERU", "CHINA", "ROMANIA", "SAUDI ARABIA", "VIETNAM",
            "RUSSIA", "UNITED KINGDOM", "UNITED STATES"]
_NATION_REGION = [0, 1, 1, 1, 4, 0, 3, 3, 2, 2, 4, 4, 2, 4, 0, 0, 0, 1, 2, 3, 4, 2, 3, 3, 1]


def _pick(values: list[str], key: str) -> str:
    return "[" + ", ".join(f"'{v}'" for v in values) + f"][{key} + 1]"


CLEAN = [
    "ATTACH ':memory:' AS samples",
    "CREATE SCHEMA samples.tpch",
    "CREATE MACRO u(k, salt, n) AS (hash(k, salt) % n)::INTEGER",
    "CREATE TABLE samples.tpch.region AS SELECT * FROM (VALUES " + ", ".join(
        f"({i}, '{r}')" for i, r in enumerate(
            ["AFRICA", "AMERICA", "ASIA", "EUROPE", "MIDDLE EAST"])) + ") t(r_regionkey, r_name)",
    "CREATE TABLE samples.tpch.nation AS SELECT * FROM (VALUES " + ", ".join(
        f"({i}, '{n}', {r})" for i, (n, r) in enumerate(zip(_NATIONS, _NATION_REGION))
    ) + ") t(n_nationkey, n_name, n_regionkey)",
    f"""CREATE TABLE samples.tpch.customer AS SELECT i AS c_custkey,
        'Customer#' || lpad(i::VARCHAR, 9, '0') AS c_name, 'Address ' || i AS c_address,
        u(i, 1, 25) AS c_nationkey, '10-' || lpad(i::VARCHAR, 7, '0') AS c_phone,
        ((u(i, 2, 1099999) - 99999) / 100.0)::DECIMAL(15, 2) AS c_acctbal,
        {_pick(['AUTOMOBILE', 'BUILDING', 'FURNITURE', 'HOUSEHOLD', 'MACHINERY'], 'u(i, 3, 5)')}
          AS c_mktsegment, 'comment ' || i AS c_comment
        FROM range(1, 30001) t(i)""",
    f"""CREATE TABLE samples.tpch.part AS SELECT i AS p_partkey,
        'Brand#' || (u(i, 1, 5) + 1) || (u(i, 2, 5) + 1) AS p_brand,
        {_pick(['STANDARD', 'SMALL', 'MEDIUM', 'LARGE', 'ECONOMY', 'PROMO'], 'u(i, 3, 6)')} || ' ' ||
        {_pick(['ANODIZED', 'BURNISHED', 'PLATED', 'POLISHED', 'BRUSHED'], 'u(i, 4, 5)')} || ' ' ||
        {_pick(['TIN', 'NICKEL', 'BRASS', 'STEEL', 'COPPER'], 'u(i, 5, 5)')} AS p_type,
        u(i, 6, 50) + 1 AS p_size,
        {_pick(['SM', 'LG', 'MED', 'JUMBO', 'WRAP'], 'u(i, 7, 5)')} || ' ' ||
        {_pick(['CASE', 'BOX', 'BAG', 'JAR', 'PKG', 'PACK', 'CAN', 'DRUM'], 'u(i, 8, 8)')}
          AS p_container,
        ((90000 + ((i // 10) % 20001) + 100 * (i % 1000)) / 100.0)::DECIMAL(15, 2) AS p_retailprice
        FROM range(1, 20001) t(i)""",
    """CREATE TABLE samples.tpch.supplier AS SELECT i AS s_suppkey,
        u(i, 1, 25) AS s_nationkey FROM range(1, 1001) t(i)""",
    f"""CREATE TABLE orders_base AS SELECT i AS o_orderkey, u(i, 1, 30000) + 1 AS o_custkey,
        DATE '1992-01-01' + u(i, 2, 2406) AS o_orderdate,
        {_pick(['1-URGENT', '2-HIGH', '3-MEDIUM', '4-NOT SPECIFIED', '5-LOW'], 'u(i, 3, 5)')}
          AS o_orderpriority, 0 AS o_shippriority
        FROM range(1, 300001) t(i)""",
    f"""CREATE TABLE samples.tpch.lineitem AS
        WITH l AS (
          SELECT o_orderkey AS l_orderkey, n AS l_linenumber, o_orderkey * 8 + n AS k,
                 o_orderdate + u(o_orderkey * 8 + n, 6, 121) + 1 AS l_shipdate
          FROM orders_base, range(1, 8) t(n) WHERE n <= u(o_orderkey, 4, 7) + 1)
        SELECT l_orderkey, l_linenumber, u(k, 1, 20000) + 1 AS l_partkey,
          u(k, 2, 1000) + 1 AS l_suppkey, (u(k, 3, 50) + 1)::DECIMAL(15, 2) AS l_quantity,
          ((u(k, 3, 50) + 1) * p.p_retailprice)::DECIMAL(15, 2) AS l_extendedprice,
          (u(k, 4, 11) / 100.0)::DECIMAL(15, 2) AS l_discount,
          (u(k, 5, 9) / 100.0)::DECIMAL(15, 2) AS l_tax,
          -- Independent of dates: dbgen ties returns to early receipts, the definition does not.
          CASE u(k, 8, 4) WHEN 0 THEN 'R' WHEN 1 THEN 'A' ELSE 'N' END AS l_returnflag,
          CASE WHEN l_shipdate > DATE '1995-06-17' THEN 'O' ELSE 'F' END AS l_linestatus,
          l_shipdate,
          {_pick(['DELIVER IN PERSON', 'COLLECT COD', 'NONE', 'TAKE BACK RETURN'], 'u(k, 9, 4)')}
            AS l_shipinstruct,
          {_pick(['REG AIR', 'AIR', 'RAIL', 'SHIP', 'TRUCK', 'MAIL', 'FOB'], 'u(k, 10, 7)')}
            AS l_shipmode
        FROM l JOIN samples.tpch.part p ON p.p_partkey = u(k, 1, 20000) + 1""",
    """CREATE TABLE samples.tpch.orders AS
        SELECT o.*, t.total::DECIMAL(15, 2) AS o_totalprice FROM orders_base o
        LEFT JOIN (SELECT l_orderkey, SUM(l_extendedprice * (1 + l_tax) * (1 - l_discount)) AS total
                   FROM samples.tpch.lineitem GROUP BY l_orderkey) t ON t.l_orderkey = o.o_orderkey""",
]

# What the definition and the owner's rules do not rule out. Not orphaned line items:
# owner rule A2 states every line item has its order, customer, nation and region.
_K = "l_orderkey * 8 + l_linenumber"
DIRTY = [
    *(f"UPDATE samples.tpch.lineitem SET {col} = {val} WHERE u({_K}, {salt}, {n}) = 0"
      for col, val, salt, n in [
          ("l_returnflag", "NULL", 21, 97), ("l_returnflag", "'r'", 22, 101),
          ("l_returnflag", "'X'", 23, 103), ("l_shipdate", "NULL", 24, 89),
          ("l_shipmode", "NULL", 25, 83), ("l_quantity", "NULL", 26, 79),
          ("l_extendedprice", "NULL", 27, 73), ("l_discount", "NULL", 28, 71),
          ("l_linestatus", "NULL", 29, 67), ("l_tax", "NULL", 30, 61)]),
    f"INSERT INTO samples.tpch.lineitem SELECT * FROM samples.tpch.lineitem WHERE u({_K}, 31, 211) = 0",
    *(f"UPDATE samples.tpch.orders SET {col} = NULL WHERE u(o_orderkey, {salt}, {n}) = 0"
      for col, salt, n in [("o_totalprice", 41, 53), ("o_orderpriority", 42, 59),
                           ("o_orderdate", 43, 47)]),
    *(f"UPDATE samples.tpch.customer SET {col} = NULL WHERE u(c_custkey, {salt}, {n}) = 0"
      for col, salt, n in [("c_acctbal", 51, 43), ("c_mktsegment", 52, 41), ("c_name", 53, 37)]),
]


def governed_rows(con: duckdb.DuckDBPyConnection) -> None:
    """revenue_mv's rows as views: its source and declared (LEFT OUTER) joins, with and
    without the required filter. Built from the snapshot, not written by hand."""
    def joins(parent: list[dict]) -> str:
        return "".join(f" LEFT JOIN {j['source']} AS {j['name']} ON {j.get('on', j.get(True))}" + joins(j.get("joins", []))
                       for j in parent)

    rows = f"SELECT * FROM {VIEW['source']} AS source{joins(VIEW['joins'])}"
    con.execute(f"CREATE OR REPLACE VIEW governed_all AS {rows}")
    con.execute(f"CREATE OR REPLACE VIEW governed AS {rows} WHERE {VIEW['filter']}")


# --- the reference query --------------------------------------------------------------

def _qualify(sql: str) -> str:
    """Unqualified columns are the governed rows' (alias e)."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    for col in tree.find_all(exp.Column):
        if not col.table:
            col.set("table", exp.to_identifier("e"))
    return tree.sql(dialect="duckdb")


def reference(spec: dict, metric: str, rows: str = "governed") -> str:
    for part in spec.get("filters", []) + spec.get("group_by", []):
        if _columns(part.split(" AS ")[0]) & REQUIRED:
            raise ValueError(f"an ingredient reads the required filter's column: {part}")
    groups = [g.rsplit(" AS ", 1) for g in spec.get("group_by", [])]
    select = [f"{_qualify(g)} AS {a}" for g, a in groups] + [f"{_qualify(MEASURE)} AS {metric}"]
    sql = f"SELECT {', '.join(select)} FROM {rows} AS e"
    for j in spec.get("joins", []):
        sql += f" JOIN samples.tpch.{j['table']} AS {j['alias']} ON {_qualify(j['on'])}"
    if spec.get("filters"):
        sql += " WHERE " + " AND ".join(f"({_qualify(f)})" for f in spec["filters"])
    if groups:
        sql += " GROUP BY " + ", ".join(_qualify(g) for g, _ in groups)
    if spec.get("order_by"):
        sql += f" ORDER BY {spec['order_by']}"
    if spec.get("limit"):
        sql += f" LIMIT {spec['limit']}"
    if spec.get("top"):
        sql = (f"SELECT * EXCLUDE (rk) FROM (SELECT *, RANK() OVER (ORDER BY {metric} DESC) AS rk "
               f"FROM ({sql})) WHERE rk <= {spec['top']}")
    return sql


def kind(part: str, grouping: bool) -> str:
    """Which rule, if any, covers an extra filter or grouping - from the columns it reads."""
    expr = part.rsplit(" AS ", 1)[0] if grouping else part
    tree = sqlglot.parse_one(expr, read="duckdb")
    cols = [(c.table, c.name) for c in tree.find_all(exp.Column)]
    if any(t == "sn" for t, _ in cols):
        return "P1"
    if (not grouping and isinstance(tree, exp.Not) and isinstance(tree.this, exp.Is)
            and isinstance(tree.this.this, exp.Column) and not tree.this.this.table
            and tree.this.this.name in MEASURE_INPUTS):
        return "A1"
    if (not grouping and isinstance(tree, exp.EQ) and isinstance(tree.this, exp.Column)
            and tree.this.name in ("c_custkey", "c_name") and isinstance(tree.expression, exp.Literal)):
        return "P2"
    if grouping and _norm(expr) in DIMENSIONS:
        return "declared"
    if not grouping and all(not t and n in DIMENSION_COLUMNS for t, n in cols):
        return "declared"
    if all(t == "p" and n in ("p_brand", "p_type") for t, n in cols):
        return "A4"
    return "other"


# --- running and comparing --------------------------------------------------------------

def presented_as(statement: str) -> tuple[str, str | None]:
    names = [e.alias_or_name for e in sqlglot.parse_one(statement, read="databricks").selects]
    named = [n for n in names if METRIC in n.lower().split("_")]
    if not named:
        return "not_presented", None
    words = set(named[0].lower().split("_"))
    return ("separate_measure" if words & OTHER_QUANTITY else "governed"), named[0]


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple]:
    def cell(v):
        return round(float(v), 4) if isinstance(v, (int, float)) or hasattr(v, "as_tuple") else str(v)
    return sorted((tuple(cell(v) for v in r) for r in con.execute(sql).fetchall()), key=repr)


def run_statement(con, statement: str, keys: list[str], metric: str) -> list[tuple]:
    duck = sqlglot.transpile(statement, read="databricks", write="duckdb")[0]
    cols = ", ".join([*keys, metric])
    return _rows(con, f"SELECT {cols} FROM ({duck}) AS t")


def build() -> list[dict]:
    uses = {r["record_id"]: r for r in map(json.loads, open(HERE / "pilot/uses.jsonl"))}
    specs = yaml.safe_load((HERE / "answer_key.yaml").read_text())
    con = duckdb.connect()
    for sql in CLEAN:
        con.execute(sql)
    governed_rows(con)

    keys = {}
    for rid, rec in uses.items():
        identity, metric = presented_as(rec["execution"]["statement"])
        if (identity == "governed") != (rid in specs):
            raise ValueError(f"{rid}: presented as {identity!r}, spec {'present' if rid in specs else 'missing'}")
        keys[rid] = {"record_id": rid, "statement_sha256": hashlib.sha256(
            rec["execution"]["statement"].encode()).hexdigest(), "presented_as": identity,
            "metric_column": metric}

    results = {}
    for label, setup in (("clean", []), ("dirty", DIRTY)):
        for sql in setup:
            con.execute(sql)
        for rid, spec in specs.items():
            metric = keys[rid]["metric_column"]
            groups = [g.rsplit(" AS ", 1)[1] for g in spec.get("group_by", [])]
            ref = _rows(con, reference(spec, metric))
            got = run_statement(con, uses[rid]["execution"]["statement"], groups, metric)
            results.setdefault(rid, {})[label] = "same" if got == ref else "differs"
            if label == "clean":
                # A pass is not a proof: the data must be able to show a dropped filter.
                unfiltered = _rows(con, reference(spec, metric, rows="governed_all"))
                results[rid]["required_filter_testable"] = unfiltered != ref

    out = []
    for rid, key in sorted(keys.items()):
        spec = specs.get(rid, {})
        variations = ([{"sql": f, "kind": kind(f, False)} for f in spec.get("filters", [])]
                      + [{"sql": g, "kind": kind(g, True)} for g in spec.get("group_by", [])])
        execution = results.get(rid)
        rules = sorted({v["kind"] for v in variations} - {"declared", "other"})
        if key["presented_as"] != "governed":
            outcome = "separate_measure"
        elif any(r.startswith("P") for r in rules):
            outcome = "prohibited_variation"
        elif execution["clean"] == "differs":
            outcome = "suspected_definition_change"
        elif execution["dirty"] == "differs":
            outcome = "insufficient_evidence"
        elif any(v["kind"] == "other" for v in variations):
            outcome = "unreviewed_variation"
        else:
            outcome = "likely_permitted"
        out.append({**key, "execution": execution, "variations": variations, "rules": rules,
                    "outcome": outcome, "treatment": TREATMENT[outcome]})
    return out


if __name__ == "__main__":
    key = build()
    untestable = [k["record_id"] for k in key if k["execution"]
                  and not k["execution"]["required_filter_testable"]]
    if untestable:
        sys.exit(f"the data cannot show a dropped required filter for: {untestable}")
    with open(HERE / "answer_key.jsonl", "w") as f:
        for k in key:
            f.write(json.dumps(k, sort_keys=True) + "\n")
    from collections import Counter
    print(len(key), "items:", dict(Counter(k["treatment"] for k in key)))
