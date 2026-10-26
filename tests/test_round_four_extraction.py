"""Round four: eleven reproduced false certifications, and the gate that
should have caught them.

2026-07-28. Every pair below hashed IDENTICALLY at proof grade with `gaps=()`
while computing different numbers on real data — the one unsurvivable failure
class. They were found by inverting the question: instead of asking "which
constructs do we know we cannot model?", the reviewer enumerated the sqlglot
arguments the SemanticCore never reads. There were 41.

So the gate is now deny-by-default (extraction/completeness.py): an argument
is unmodeled until it is named as modeled. The tests here come in two halves —
constructs that must be GATED (reported, never certified), and constructs that
must be MODELED (distinguished by hash). Adding a name to the modeled sets
without a test here re-opens a false-certification path.
"""

import pytest

from semantix.extraction.canonicalize import canonicalize
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import PROOF_LEVELS, Provenance

CATALOG = {
    "raw.orders": {"order_id": "bigint", "customer_id": "bigint", "status": "varchar",
                   "channel": "varchar", "amount": "decimal(18,2)",
                   "refund_amount": "decimal(18,2)"},
    "raw.refunds": {"refund_id": "bigint", "order_id": "bigint", "amt": "decimal(18,2)"},
    "raw.sales": {"status": "varchar", "amount": "decimal(18,2)"},
}


def core(sql, dialect="duckdb"):
    """(hash, canon_level, gaps) for the first measure in `sql`."""
    registry = SchemaRegistry(CATALOG)
    canon = canonicalize(sql, registry, dialect)
    fps = extract_fingerprints(canon, "analytics.m", Provenance("t", "t"),
                               dialect, registry)
    if not fps:
        return None, "no-fingerprint", ()
    return fps[0].core.hash(), fps[0].canon_level, fps[0].gaps


def assert_not_certifiable(label, a, b, dialect="duckdb"):
    """The pair must not be provably identical: either the hashes differ, or
    at least one side is below proof grade with a named gap."""
    ha, la, ga = core(a, dialect)
    hb, lb, gb = core(b, dialect)
    both_proof = la in PROOF_LEVELS and lb in PROOF_LEVELS
    assert not (ha is not None and ha == hb and both_proof), (
        f"{label}: FALSE CERTIFICATION — same hash at proof grade "
        f"({la}/{lb}), gaps {ga}/{gb}")


# ------------------------------------------------- MODELED: hashes differ

def test_join_direction_is_semantic():
    """`A LEFT JOIN B` and `B LEFT JOIN A` preserve different sides. The edge
    recorded the predicate but never WHICH relation was joined, and
    normalization sorts the ON operands — so the two rendered byte-identically.
    Needs no exotic syntax: two ordinary dbt models, 700 vs 200 on real data.
    This was the worst false-certification path in the engine."""
    a = "select sum(o.amount) as m from raw.orders o left join raw.refunds r on o.order_id = r.order_id"
    b = "select sum(o.amount) as m from raw.refunds r left join raw.orders o on o.order_id = r.order_id"
    assert_not_certifiable("join direction", a, b)
    assert core(a)[0] != core(b)[0]


def test_asof_join_is_not_an_inner_join():
    """ASOF keeps one nearest match per probe row; INNER keeps all of them —
    a 3x row multiplication certified as identity."""
    assert_not_certifiable(
        "asof",
        "select sum(o.amount) as m from raw.orders o asof join raw.refunds r on o.order_id >= r.order_id",
        "select sum(o.amount) as m from raw.orders o join raw.refunds r on o.order_id >= r.order_id")


def test_group_by_all_is_a_real_grain():
    """GROUP BY ALL lives in its own arg, so the grain read as EMPTY and a full
    breakdown hashed as a grand total — the same hole ROLLUP had, reopened by
    the default idiom of modern DuckDB/Databricks SQL."""
    assert_not_certifiable(
        "group by all",
        "select status, sum(amount) as m from raw.orders group by all",
        "select sum(amount) as m from raw.orders")


def test_group_by_all_equals_its_explicit_spelling():
    """Modeled, not merely gated: it must also compare EQUAL to the explicit
    grouping it means, or a rewrite would look like a contradiction."""
    a = core("select status, sum(amount) as m from raw.orders group by all")
    b = core("select status, sum(amount) as m from raw.orders group by status")
    assert a[0] == b[0], "GROUP BY ALL did not match its explicit equivalent"
    assert a[1] in PROOF_LEVELS


def test_ordinals_inside_rollup_are_resolved():
    """Ordinals in ROLLUP/CUBE/GROUPING SETS live in their own args, so
    `rollup(1,2)` was left unresolved — grain non-empty but content-free, and
    two different breakdowns hashed the same."""
    assert_not_certifiable(
        "rollup ordinals",
        "select status, channel, sum(amount) as m from raw.orders group by rollup(1, 2)",
        "select channel, status, sum(amount) as m from raw.orders group by rollup(1, 2)")


def test_distinct_on_carries_its_key():
    """DISTINCT ON (k) keeps one row per k. Collapsing every Distinct node to
    the bare tag "SELECT DISTINCT" made a keyed dedup certify equal to a
    differently-keyed one."""
    assert_not_certifiable(
        "distinct on",
        "select distinct on (customer_id) customer_id, sum(amount) as m "
        "from raw.orders group by customer_id, status",
        "select distinct on (status) customer_id, sum(amount) as m "
        "from raw.orders group by customer_id, status")


def test_order_by_inside_a_cte_is_load_bearing():
    """ORDER BY is neutral only in the OUTERMOST select. Inside a staging CTE
    it decides what FIRST/LAST/ARG_MAX see in the scope above, and stripping
    happened before the body was digested, so the digest could not recover it."""
    assert_not_certifiable(
        "cte ordering",
        "with s as (select customer_id, amount from raw.orders order by amount asc) "
        "select customer_id, first(amount) as m from s group by 1",
        "with s as (select customer_id, amount from raw.orders order by amount desc) "
        "select customer_id, first(amount) as m from s group by 1")


# --------------------------------------------------- GATED: never certified

@pytest.mark.parametrize("label,dialect,a,b", [
    ("TABLESAMPLE", "duckdb",
     "select sum(amount) as m from raw.orders using sample 2 rows (reservoir, 42)",
     "select sum(amount) as m from raw.orders"),
    ("time travel", "databricks",
     "select sum(amount) as m from raw.orders version as of 3",
     "select sum(amount) as m from raw.orders"),
    ("PIVOT", "duckdb",
     "select sum(completed) as m from raw.sales pivot (sum(amount) for status in ('completed'))",
     "select sum(completed) as m from raw.sales pivot (min(amount) for status in ('completed'))"),
    ("named WINDOW", "duckdb",
     "select customer_id, sum(amount) as m from raw.orders group by 1 "
     "window w as (order by sum(amount) desc) qualify row_number() over w = 1",
     "select customer_id, sum(amount) as m from raw.orders group by 1 "
     "window w as (order by sum(amount) asc) qualify row_number() over w = 1"),
    ("inline VALUES", "duckdb",
     "select sum(col1) as m from (values (1), (2)) t(col1)",
     "select sum(col1) as m from (values (10), (20)) t(col1)"),
    ("table function", "duckdb",
     "select sum(x) as m from generate_series(1, 10) t(x)",
     "select sum(x) as m from generate_series(1, 100) t(x)"),
    ("LATERAL VIEW", "databricks",
     "select sum(o.amount) as m from raw.orders o lateral view explode(array(1,2,3)) e as x",
     "select sum(o.amount) as m from raw.orders o"),
    ("alias column list", "duckdb",
     "select sum(a) as m from raw.refunds t(a, b, c)",
     "select sum(a) as m from raw.refunds t(b, a, c)"),
])
def test_unmodelled_constructs_cannot_certify(label, dialect, a, b):
    assert_not_certifiable(label, a, b, dialect)


@pytest.mark.parametrize("sql,dialect,fragment", [
    ("select sum(amount) as m from raw.orders using sample 2 rows (reservoir, 42)",
     "duckdb", "TABLESAMPLE"),
    ("select sum(amount) as m from raw.orders version as of 3", "databricks", "time travel"),
    ("select sum(x) as m from generate_series(1, 10) t(x)", "duckdb", "table function"),
    ("select sum(col1) as m from (values (1)) t(col1)", "duckdb", "VALUES"),
])
def test_a_gate_says_what_it_could_not_read(sql, dialect, fragment):
    """Refusing is only half of it — the gap must NAME the construct, or a
    reader cannot tell a modelling limit from a bug."""
    _, level, gaps = core(sql, dialect)
    assert level not in PROOF_LEVELS
    assert any(fragment in g for g in gaps), f"{fragment} not named in {gaps}"


def test_ordinary_sql_is_still_proof_grade():
    """The deny-by-default gate must not swallow the constructs the core does
    model — otherwise nothing certifies and the tool is useless."""
    for sql in [
        "select sum(amount) as m from raw.orders where status = 'completed'",
        "select customer_id, sum(amount) as m from raw.orders group by 1",
        "with s as (select * from raw.orders) select sum(amount) as m from s",
        "select customer_id, sum(amount) as m from raw.orders group by 1 having sum(amount) > 1",
        "select customer_id, sum(amount) as m from raw.orders group by 1 order by 2 desc",
    ]:
        _, level, gaps = core(sql)
        assert level in PROOF_LEVELS, f"{sql!r} -> {level} {gaps}"
        assert gaps == (), f"{sql!r} -> {gaps}"
    # Joins and ordered LIMIT left the proof grammar in semantix-proof/2:
    # refused by name — coverage backlog, not proof.
    for sql, construct in [
        ("select sum(o.amount) as m from raw.orders o join raw.refunds r "
         "on o.order_id = r.order_id", "uses a join"),
        ("select customer_id, sum(amount) as m from raw.orders group by 1 "
         "order by 2 desc limit 10", "LIMIT"),
    ]:
        _, level, gaps = core(sql)
        assert level not in PROOF_LEVELS
        assert any("outside the supported grammar" in g and construct in g for g in gaps), gaps


# ------------------------------------------------------- CTE body digests

def test_renaming_an_intermediate_cte_does_not_change_the_fingerprint():
    """`_resolve_cte_sources` documents "keyed on the body alone, never the
    CTE's name". That held only for the leaf: the body TEXT named the CTEs it
    read, and the digest payload keyed each nested entry by name, so a pure
    refactor reported a CONTRADICTION on every downstream metric."""
    a = core("with s as (select customer_id, sum(amount) as t from raw.orders group by 1), "
             "f as (select sum(t) as x from s) select sum(x) as m from f")
    b = core("with stg as (select customer_id, sum(amount) as t from raw.orders group by 1), "
             "f as (select sum(t) as x from stg) select sum(x) as m from f")
    assert a[0] == b[0], "renaming a CTE changed the fingerprint"


def test_changed_cte_logic_still_changes_the_fingerprint():
    """The converse — the rename fix must not make bodies interchangeable."""
    a = core("with s as (select customer_id, sum(amount) as t from raw.orders group by 1), "
             "f as (select sum(t) as x from s) select sum(x) as m from f")
    c = core("with s as (select customer_id, sum(amount) as t from raw.orders "
             "where status = 'x' group by 1), "
             "f as (select sum(t) as x from s) select sum(x) as m from f")
    assert a[0] != c[0]


def test_a_recursive_cte_does_not_restore_the_exponential_blowup():
    """The memo was skipped for any body a cycle touched, and `cyclic`
    propagated to every ancestor — so ONE `WITH RECURSIVE` (a date spine, a
    hierarchy walk) disabled memoization for the whole graph. 32 CTEs went
    from 0.06s to 58s; two more layers, four minutes."""
    import time

    parts = ["r as (select 1 as n union all select n + 1 from r where n < 3)",
             "l0_a as (select order_id, amount from raw.orders where status = 'a')",
             "l0_b as (select order_id, amount from raw.orders where status = 'b')"]
    for i in range(1, 16):
        for side in ("a", "b"):
            parts.append(
                f"l{i}_{side} as (select order_id, sum(amount) as amount "
                f"from l{i-1}_a group by 1 union all select order_id, "
                f"sum(amount) as amount from l{i-1}_b group by 1)")
    sql = "with recursive " + ",\n".join(parts) + "\nselect sum(amount) as m from l15_a"

    start = time.perf_counter()
    core(sql)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"recursive CTE graph took {elapsed:.1f}s"


# ------------------------------------------------ the gate applies to CLAIMS

def test_a_declared_definition_is_held_to_the_completeness_gate():
    """`claim.canon_level` came from canonicalize_sql, which never returns
    INCOMPLETE — the downgrade happens inside extract_fingerprints and was
    never copied back. reconcile reads exactly this field to decide proof
    grade, so a declaration using an unmodeled construct was fed straight into
    the CERTIFIED gate."""
    from semantix.adapters.base import DeclaredSpec
    from semantix.engine import build_claim

    registry = SchemaRegistry(CATALOG)
    ok = build_claim(DeclaredSpec(system="yaml", name="m", model="raw.orders",
                                  expr="sum(amount)"), registry, "duckdb")
    assert ok.canon_level in PROOF_LEVELS
    assert ok.gaps == ()

    gated = build_claim(DeclaredSpec(system="yaml", name="m",
                                     model="raw.orders USING SAMPLE 2 ROWS",
                                     expr="sum(amount)"), registry, "duckdb")
    assert gated.canon_level not in PROOF_LEVELS, "a declaration bypassed the gate"
    assert gated.gaps
    assert gated.error
