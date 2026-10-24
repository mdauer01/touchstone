"""Round seven: the review of the September build wave.

Ninety-four findings across six reviewers. The ones here are the structural
ones — each test names the false certification or false accusation it
pins shut, so a future refactor that reopens it fails by name.
"""

from __future__ import annotations


from semantix.engine import run_scan
from semantix.extraction.canonicalize import canonicalize_sql
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import PROOF_LEVELS, Fingerprint, Provenance, SemanticCore, VerdictType
from semantix.signing import receipt_trust

CATALOG = {"raw.orders": {"customer_id": "INT", "amount": "DOUBLE",
                          "status": "TEXT", "order_date": "DATE"}}


def _core(sql: str, dialect: str, catalog=CATALOG):
    registry = SchemaRegistry(catalog)
    canon = canonicalize_sql(sql, registry.sqlglot_schema(dialect), dialect)
    fps = extract_fingerprints(canon, "db.main.m", Provenance("t", "m"), dialect, registry)
    assert len(fps) == 1, (canon.level, canon.error)
    return fps[0]


def _never_equal_at_proof(a: str, b: str, dialect: str) -> None:
    fa, fb = _core(a, dialect), _core(b, dialect)
    proof = fa.canon_level in PROOF_LEVELS and not fa.gaps \
        and fb.canon_level in PROOF_LEVELS and not fb.gaps
    assert not (proof and fa.core.hash() == fb.core.hash()), \
        "two different computations certified equal at proof grade"


# ------------------------------------------------ canonicalization false certs


def test_case_mismatched_cte_reference_still_gates_ordering():
    # DuckDB folds unquoted identifiers: `S` and `s` are one CTE, and
    # first() consumes its ordering. Matching CTE names as written missed
    # the chain and asc/desc variants certified equal.
    a = ("WITH S AS (SELECT amount FROM raw.orders ORDER BY amount DESC) "
         "SELECT first(amount) AS m FROM s")
    b = a.replace("DESC", "ASC")
    _never_equal_at_proof(a, b, "duckdb")


def test_distinct_on_inheriting_order_is_not_proof_grade():
    a = ("WITH o AS (SELECT customer_id, amount FROM raw.orders "
         "ORDER BY customer_id, amount DESC), "
         "d AS (SELECT DISTINCT ON (customer_id) customer_id, amount FROM o) "
         "SELECT SUM(amount) AS m FROM d")
    b = a.replace("amount DESC", "amount ASC")
    _never_equal_at_proof(a, b, "postgres")


def test_group_by_ordinal_targeting_aggregate_is_a_gap():
    fp = _core("SELECT status, SUM(amount) AS m FROM raw.orders GROUP BY 2", "duckdb")
    assert fp.canon_level not in PROOF_LEVELS or fp.gaps
    assert any("ordinal" in g for g in fp.gaps)


# ---------------------------------------------- the `_` pin and reference forms


def _mixed_depth_estate(tmp_path):
    estate = tmp_path / "estate"
    estate.mkdir()
    body = "SELECT SUM(amount) AS net_revenue FROM {ref} WHERE status = 'completed'\n"
    for name, ref in (("m1", "raw.orders"), ("m3", "PROD.raw.orders"), ("m2", "orders")):
        (estate / f"{name}.sql").write_text(
            f"-- semantix: relation=PROD.analytics.{name}\n" + body.format(ref=ref),
            encoding="utf-8")
    catalog = tmp_path / "catalog.yml"
    catalog.write_text("raw:\n  orders:\n    amount: DOUBLE\n    status: TEXT\n",
                       encoding="utf-8")
    return estate, catalog


def test_pin_placeholder_never_leaks_into_cores(tmp_path):
    """The `_` qualification pin must never surface in a core. (Round seven
    also asserted the three spellings below were ONE table; round eight
    found that assumption certified prod vs dev reads as identical. Without
    execution context the short spellings are not provably the same table.)"""
    estate, catalog = _mixed_depth_estate(tmp_path)
    result, store = run_scan(sql_dir=str(estate), catalog_path=str(catalog),
                             db_path=str(tmp_path / "s.db"))
    store.close()
    assert len(result.fingerprints) == 3, result.canon_levels
    assert not any("_." in fp.canonical_sql for fp in result.fingerprints)
    assert not any(n.startswith("_.") for fp in result.fingerprints for n in fp.core.join_nodes)
    by_rel = {fp.relation.rsplit(".", 1)[-1]: fp for fp in result.fingerprints}
    # The explicitly qualified one is proof grade; the short ones depend on
    # the session and say so, so nothing certifies across them.
    assert by_rel["m3"].canon_level in PROOF_LEVELS
    for short in ("m1", "m2"):
        assert by_rel[short].canon_level not in PROOF_LEVELS
        assert any("depends on the session" in g for g in by_rel[short].gaps)
    assert not any(v.verdict_type == VerdictType.CERTIFIED for v in result.verdicts)


def test_execution_context_makes_spellings_one_table(tmp_path):
    estate, catalog = _mixed_depth_estate(tmp_path)
    result, store = run_scan(sql_dir=str(estate), catalog_path=str(catalog),
                             db_path=str(tmp_path / "s.db"),
                             default_database="PROD", default_schema="raw")
    store.close()
    assert all(fp.canon_level in PROOF_LEVELS for fp in result.fingerprints)
    assert len({fp.core.hash() for fp in result.fingerprints}) == 1
    assert not any(v.verdict_type == VerdictType.CONTRADICTION for v in result.verdicts)


def test_canonical_relation_never_drops_or_guesses_qualifiers():
    registry = SchemaRegistry({"db.main.orders": {"amount": "DOUBLE"}})
    # No context: a short reference keeps its short identity...
    assert registry.canonical_relation("orders") == "orders"
    assert registry.canonical_relation("main.orders") == "main.orders"
    # ...and an explicit qualifier is never dropped.
    assert registry.canonical_relation("PROD.db.main.orders") == "PROD.db.main.orders"
    assert registry.canonical_relation("db.main.orders") == "db.main.orders"
    assert registry.canonical_relation("cte:abc") == "cte:abc"
    short = SchemaRegistry({"raw.orders": {"amount": "DOUBLE"}})
    assert short.canonical_relation("prod.raw.orders") != short.canonical_relation("dev.raw.orders")
    # With context, the omitted parts are filled — and only those.
    ctx = SchemaRegistry({"db.main.orders": {"amount": "DOUBLE"}},
                         default_database="db", default_schema="main")
    assert ctx.canonical_relation("orders") == "db.main.orders"
    assert ctx.canonical_relation("main.orders") == "db.main.orders"
    assert ctx.canonical_relation("other.main.orders") == "other.main.orders"


# ------------------------------------------------------- store identity is per site


def test_fingerprint_identity_is_per_site_and_per_level():
    core = SemanticCore(measure_expr="SUM(x)", join_nodes=("t",), source_columns=("t.x",))
    a = Fingerprint.make_id(core, "m", "db.a", "optimized", ())
    b = Fingerprint.make_id(core, "m", "db.b", "optimized", ())
    c = Fingerprint.make_id(core, "m", "db.a", "incomplete", ("FROM: TABLESAMPLE",))
    assert len({a, b, c}) == 3


def test_two_sites_same_math_both_reach_the_store(tmp_path):
    estate = tmp_path / "estate"
    estate.mkdir()
    for name in ("rev_mart", "rev_mart_v2"):
        (estate / f"{name}.sql").write_text(
            f"-- semantix: relation=db.main.{name}\n"
            "SELECT SUM(amount) AS total_revenue FROM db.main.orders\n", encoding="utf-8")
    (tmp_path / "catalog.yml").write_text(
        "db:\n  main:\n    orders:\n      amount: DOUBLE\n", encoding="utf-8")
    result, store = run_scan(sql_dir=str(estate), catalog_path=str(tmp_path / "catalog.yml"),
                             db_path=str(tmp_path / "s.db"))
    (concept,) = [c for c in store.concepts_for_scan(result.scan_id)
                  if c["label"] == "total_revenue"]
    rows = [store.fingerprint_row(f) for f in concept["members"]["fingerprints"]]
    store.close()
    assert {r["relation"] for r in rows} == {"db.main.rev_mart", "db.main.rev_mart_v2"}


def test_level_change_records_a_new_row(tmp_path):
    estate = tmp_path / "estate"
    estate.mkdir()
    model = estate / "rev_mart.sql"
    clean = ("-- semantix: relation=db.main.rev_mart\n"
             "SELECT SUM(amount) AS total_revenue FROM db.main.orders\n")
    model.write_text(clean, encoding="utf-8")
    (tmp_path / "catalog.yml").write_text(
        "db:\n  main:\n    orders:\n      amount: DOUBLE\n", encoding="utf-8")
    kwargs = dict(sql_dir=str(estate), catalog_path=str(tmp_path / "catalog.yml"),
                  db_path=str(tmp_path / "s.db"))
    _, store = run_scan(**kwargs)
    store.close()
    model.write_text(clean.replace("db.main.orders", "db.main.orders TABLESAMPLE 10 PERCENT"),
                     encoding="utf-8")
    result, store = run_scan(**kwargs)
    (concept,) = store.concepts_for_scan(result.scan_id)
    rows = [store.fingerprint_row(f) for f in concept["members"]["fingerprints"]]
    store.close()
    assert rows and all(r["canon_level"] not in PROOF_LEVELS for r in rows)


def test_claim_identity_covers_model_and_refusal(tmp_path):
    from semantix.adapters.base import DeclaredSpec
    from semantix.engine import build_claim

    registry = SchemaRegistry({})
    a = build_claim(DeclaredSpec("s", "rev", model="db.a", expr="SUM(x)"), registry, "duckdb")
    b = build_claim(DeclaredSpec("s", "rev", model="db.b", expr="SUM(x)"), registry, "duckdb")
    r1 = build_claim(DeclaredSpec("s", "rev", untranslatable=["MDX"]), registry, "duckdb")
    r2 = build_claim(DeclaredSpec("s", "rev", untranslatable=["TABLEAU"]), registry, "duckdb")
    assert len({a.claim_id, b.claim_id, r1.claim_id, r2.claim_id}) == 4


# -------------------------------------------------------------- receipt trust


def test_receipt_trust_unknown_freshness_as_of_is_disclosed_not_blocked():
    receipt = {"status": "CERTIFIED", "as_of": "2026-01-01T00:00:00Z",
               "freshness": {"status": "unknown"}}
    trusted, warnings = receipt_trust(receipt)
    assert trusted and any("valid for that instant only" in w for w in warnings)
    current = {"status": "CERTIFIED", "freshness": {"status": "unknown"}}
    assert receipt_trust(current)[0] is False
