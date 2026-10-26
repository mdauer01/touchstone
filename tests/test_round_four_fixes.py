"""Round-four regressions: the findings that survived round three.

2026-07-28. Each test below failed before its fix. The theme of this round is
narrower than the last: not "the engine gets the wrong answer" but "the engine
reports a true answer for a false reason" — a `failed` edge that was only
un-demonstrated, an UNRESOLVED that names a cause that does not hold, a blame
sentence that reads as a rendering bug, a benchmark that exits 0 having proved
nothing. A verifier that cannot be trusted about WHY is not a verifier.
"""

import time

import pytest

from semantix.extraction.canonicalize import canonicalize
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import Provenance, VerdictType
from semantix.services.blame import blame_members
from semantix.services.execute import _relations_read, _same_relation
from semantix.services.relationships import edge_status

from .policy import engine_verdict

CATALOG = {"raw.orders": {"order_id": "bigint", "customer_id": "bigint",
                          "status": "varchar", "amount": "decimal(18,2)"}}


# ------------------------------------------------- relationship classification

def test_a_check_that_demonstrated_nothing_is_unverified_not_failed():
    """`failed` tells an agent the join is KNOWN broken. An empty table
    demonstrates nothing in either direction, and saying otherwise blames
    working code for data that has not landed yet."""
    row = {"check": {"passed": False,
                     "evidence": {"inconclusive": True,
                                  "reason": "no non-null values in raw.orders.customer_id"}}}
    assert edge_status(row) == "unverified"


def test_a_demonstrated_violation_is_still_failed():
    """The other half of the same split: evidence the positive-evidence floor
    cannot take away must keep its verdict, or the fix above would quietly
    disarm the check."""
    row = {"check": {"passed": False,
                     "evidence": {"cardinality": "N:M",
                                  "reason": "no unique side (N:M)"}}}
    assert edge_status(row) == "failed"


def test_an_n_to_m_over_sparse_data_still_fails(tmp_path):
    """N:M is established from distinct counts alone, so it survives the
    evidence floor even when the column is mostly NULL. Classifying it as
    inconclusive would let a row-multiplying join through."""
    pytest.importorskip("duckdb")
    from semantix.services.relationships import record_declared, verify_edges
    from semantix.store import Store
    from semantix.warehouse import DuckDBWarehouse

    store = Store(tmp_path / "rel.db")

    warehouse = DuckDBWarehouse.ephemeral(
        """
        CREATE SCHEMA IF NOT EXISTS raw;
        CREATE TABLE raw.orders (customer_id BIGINT);
        INSERT INTO raw.orders VALUES (1),(1),(NULL),(NULL),(NULL);
        CREATE TABLE raw.customers (customer_id BIGINT);
        INSERT INTO raw.customers VALUES (1),(1),(NULL),(NULL),(NULL);
        """,
        label="sparse-nm",
    )
    try:
        record_declared(store, [("raw.orders.customer_id",
                                 "raw.customers.customer_id")], "c.yml")
        row = verify_edges(store, warehouse)[0]
        assert row["status"] == "failed"
        assert "N:M" in row["check"]["evidence"]["reason"]
    finally:
        warehouse.close()


# ------------------------------------------------------------ execution taint

def test_relations_read_are_parsed_not_substring_matched():
    reads = _relations_read(
        "select sum(amount) as m from raw.orders_backup where note = 'raw.orders'",
        "duckdb",
    )
    assert reads == {"raw.orders_backup"}
    # The failed relation `raw.orders` appears twice in that text and is read
    # zero times. Substring matching suppressed measurement on both.
    assert not any(_same_relation("raw.orders", r) for r in reads)


def test_same_relation_tolerates_qualification_but_not_prefixes():
    assert _same_relation("analytics.orders", "prod.analytics.orders")
    assert _same_relation("raw.orders", "raw.orders")
    assert not _same_relation("raw.orders", "raw.orders_backup")
    assert not _same_relation("raw.orders", "staging.orders")


def test_ctes_are_not_reported_as_physical_relations():
    reads = _relations_read(
        "with orders as (select * from raw.orders) select sum(amount) as m from orders",
        "duckdb",
    )
    assert reads == {"raw.orders"}


def test_a_certification_is_measured_against_the_data(acme, tmp_path):
    """Execution is the only instrument that can falsify a canonical equality,
    and it was pointed exclusively at CONTRADICTION — the one verdict class
    that does not need falsifying. Certifications must be measured too."""
    pytest.importorskip("duckdb")
    from semantix.engine import run_scan

    result, _ = run_scan(
        dbt_path=str(acme / "dbt_project"),
        metrics_path=str(acme / "metrics.yml"),
        catalog_path=str(acme / "catalog.yml"),
        db_path=str(tmp_path / "cert.db"),
        execute_seed=str(acme / "seed.sql"),
    )
    # The engine's certifications (reported as experimental comparisons while
    # positive verdicts are suspended).
    certified = [v for v in result.verdicts if engine_verdict(v) is VerdictType.CERTIFIED]
    assert certified, "the demo estate should certify something"
    assert all(v.verdict_type is VerdictType.UNRESOLVED for v in certified)
    assert any((v.evidence.get("measured") or {}).get("measured") for v in certified), \
        "no certification was measured — equality went unfalsified"
    # It survived: nothing the engine proved identical disagreed on real data.
    assert result.execution["collisions"] == 0


def test_a_falsified_certification_is_withdrawn(tmp_path):
    """The other half: when members the engine proved identical compute
    different numbers, the certificate cannot stand. Constructed rather than
    found — the corpus reports zero false certifications, so a genuine
    collision does not exist to test with."""
    pytest.importorskip("duckdb")
    from semantix.models import Concept, Fingerprint, SemanticCore, Verdict
    from semantix.services.execute import annotate_with_execution

    core = SemanticCore(measure_expr="SUM(amount)", source_columns=("raw.orders.amount",))

    def fp(relation, sql):
        return Fingerprint(
            fingerprint_id=Fingerprint.make_id(core, "revenue"),
            concept_candidate="revenue", core=core, relation=relation,
            dialect="duckdb", canon_level="supported", canonical_sql=sql,
            provenance=Provenance("sql", relation, f"{relation}.sql"),
        )

    concept = Concept(
        concept_id="c1", label="revenue",
        # Same core hash, different SQL: exactly the shape a hash collision or
        # an unmodeled construct produces.
        fingerprints=[
            fp("analytics.a", "select sum(amount) as revenue from raw.orders"),
            fp("analytics.b",
               "select sum(amount) as revenue from raw.orders where status = 'completed'"),
        ],
    )
    verdict = Verdict(VerdictType.CERTIFIED, "c1", "revenue", 5,
                      "Definitions are canonically identical.", {})

    seed = tmp_path / "seed.sql"
    seed.write_text(
        "CREATE SCHEMA IF NOT EXISTS raw;\n"
        "CREATE TABLE raw.orders (order_id BIGINT, status VARCHAR, amount DECIMAL(18,2));\n"
        "INSERT INTO raw.orders VALUES (1,'completed',100),(2,'pending',200);\n"
    )
    summary = annotate_with_execution([concept], [verdict], [], seed, "duckdb")

    assert summary["collisions"] == 1
    assert verdict.verdict_type is VerdictType.CONTRADICTION, \
        "a certification the data falsified kept its certificate"
    assert verdict.severity >= 95
    # Wording under the conclusions policy: "Certification WITHDRAWN" became
    # an experimental equality falsified by an observed discrepancy.
    assert verdict.summary.startswith("Experimental equality FALSIFIED by execution")
    assert "(an observed discrepancy)" in verdict.summary
    assert "canonical equality unsound" in verdict.summary


# ----------------------------------------------------------------- blame text

def test_blame_never_says_x_instead_of_x():
    """Aliases and quotes are stripped for readability, which made two
    expressions that differ only in what they bind to render identically —
    'computes SUM(amount) instead of SUM(amount)'."""
    def member(mid, expr, lineage):
        return {"member_id": mid, "kind": "computed", "core_hash": mid,
                "where": f"analytics.{mid}", "name": "revenue",
                "components": {"expression": expr, "filters": [], "grain": [],
                               "join graph": [], "physical lineage": lineage}}

    blame = blame_members([
        member("a", 'SUM(t1."amount")', ["raw.orders"]),
        member("b", 'SUM(t1."amount")'.replace("t1", "t1"), ["raw.orders_v2"]),
    ])
    # Same rendered expression, different lineage: the sentence must not claim
    # the member computes X instead of X.
    for delta in blame["divergent"]:
        sentence = delta["sentence"]
        assert "SUM(amount) instead of SUM(amount)" not in sentence


def test_blame_still_names_a_real_expression_difference():
    def member(mid, expr):
        return {"member_id": mid, "kind": "computed", "core_hash": mid,
                "where": f"analytics.{mid}", "name": "revenue",
                "components": {"expression": expr, "filters": [], "grain": [],
                               "join graph": [], "physical lineage": ["raw.orders"]}}

    blame = blame_members([member("a", 'SUM(t1."amount")'),
                           member("b", 'SUM(t1."amount" - t1."refund_amount")')])
    assert any("refund_amount" in d["sentence"] for d in blame["divergent"])


# ---------------------------------------------------------- CTE digest scale

def _diamond(depth: int) -> str:
    """Each layer reads BOTH of the previous — the shape a layered dbt project
    produces, and the shape that re-hashed shared bodies once per path."""
    parts = ["l0_a as (select order_id, amount from raw.orders where status = 'a')",
             "l0_b as (select order_id, amount from raw.orders where status = 'b')"]
    for i in range(1, depth):
        for side in ("a", "b"):
            parts.append(
                f"l{i}_{side} as (select order_id, sum(amount) as amount "
                f"from l{i-1}_a group by 1 union all select order_id, "
                f"sum(amount) as amount from l{i-1}_b group by 1)")
    return "with " + ",\n".join(parts) + f"\nselect sum(amount) as m from l{depth-1}_a"


def test_a_deep_cte_graph_extracts_in_bounded_time():
    """Timing is a blunt instrument, but the failure mode here is not a wrong
    answer — it is extraction never returning. Pre-fix this shape cost ~4x per
    two levels: 7s at depth 14, ~110s at depth 16. The bound is loose enough
    to be stable and tight enough that the old recursion cannot pass it."""
    registry = SchemaRegistry(CATALOG)
    sql = _diamond(16)
    start = time.perf_counter()
    canon = canonicalize(sql, registry, "duckdb")
    fps = extract_fingerprints(canon, "analytics.deep", Provenance("t", "t"),
                               "duckdb", registry)
    elapsed = time.perf_counter() - start
    assert fps, "a deep CTE graph produced no fingerprint"
    assert elapsed < 5.0, f"deep CTE extraction took {elapsed:.1f}s"


def test_deep_cte_bodies_still_distinguish_different_logic():
    """Memoizing must not make two structurally different graphs collide —
    the whole point of folding CTE bodies in."""
    registry = SchemaRegistry(CATALOG)

    def core_of(sql):
        canon = canonicalize(sql, registry, "duckdb")
        fps = extract_fingerprints(canon, "analytics.deep", Provenance("t", "t"),
                                   "duckdb", registry)
        return fps[0].core.hash()

    same = core_of(_diamond(6)) == core_of(_diamond(6))
    differs = core_of(_diamond(6)) != core_of(_diamond(6).replace("'a'", "'z'"))
    assert same, "the same graph hashed differently twice"
    assert differs, "a changed filter deep in the graph did not change the hash"
