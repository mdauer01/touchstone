"""Round eight: the external review of ec4baff, plus what the execution oracle
found once it existed.

Two false certifications reproduced by the reviewer (database identity
collapsed onto a short catalog entry; associative arithmetic rewrites), one
more found by the oracle (rendering flattened right-nested arithmetic), an
unpinned parser that reopened grouping false certifications, a receipt that
stayed trusted after its SQL doubled, and a coverage number computed before
the completeness gate. Each test names the failure it pins shut.
"""

from __future__ import annotations

import pytest
import sqlglot
from sqlglot import exp

from semantix.engine import run_scan
from semantix.extraction.canonicalize import _resolve_group_ordinals, canonicalize
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.extraction.sqlutil import split_group
from semantix.models import PROOF_LEVELS, Provenance, VerdictType
from semantix.signing import receipt_trust

from .policy import engine_kinds, engine_verdict


def _estate(tmp_path, models: dict, catalog: str, metrics: str | None = None, **kw):
    root = tmp_path / "estate"
    (root / "models").mkdir(parents=True, exist_ok=True)
    for name, (rel, sql) in models.items():
        (root / "models" / f"{name}.sql").write_text(f"-- semantix: relation={rel}\n{sql}\n")
    (root / "catalog.yml").write_text(catalog)
    args = dict(sql_dir=str(root / "models"), catalog_path=str(root / "catalog.yml"),
                db_path=str(tmp_path / "s.db"), **kw)
    if metrics:
        (root / "metrics.yml").write_text(metrics)
        args["metrics_path"] = str(root / "metrics.yml")
    return args


def _kinds(result, label):
    return {v.verdict_type for v in result.verdicts if v.concept_label == label}


# ------------------------------------------------- finding 1: relation identity

PROD_DEV = {
    "m_prod": ("analytics.m_prod", "SELECT SUM(amount) AS m FROM prod.raw.orders"),
    "m_dev": ("analytics.m_dev", "SELECT SUM(amount) AS m FROM dev.raw.orders"),
}
SHORT_CATALOG = "raw:\n  orders:\n    amount: DOUBLE\n"
DECLARED_M = "version: 1\nmetrics:\n  - name: m\n    model: raw.orders\n    expr: \"sum(amount)\"\n"


def test_prod_and_dev_never_certify_as_one_table(tmp_path):
    """Executed, these returned 100 and 900; ec4baff certified them identical."""
    result, store = run_scan(**_estate(tmp_path, PROD_DEV, SHORT_CATALOG, DECLARED_M))
    store.close()
    nodes = {fp.relation: fp.core.join_nodes for fp in result.fingerprints}
    assert nodes["analytics.m_prod"] != nodes["analytics.m_dev"]
    # The ENGINE must not certify them (not merely the policy hiding it).
    assert VerdictType.CERTIFIED not in engine_kinds(result.verdicts, "m")


FULL_CATALOG = ("prod:\n  raw:\n    orders:\n      amount: DOUBLE\n"
                "dev:\n  raw:\n    orders:\n      amount: DOUBLE\n")


def test_execution_context_resolves_the_short_declaration(tmp_path):
    """With the catalog at the estate's depth, the short declaration resolves
    through the supplied context to prod.raw.orders — and dev's model then
    provably reads another table. (Over the two-part catalog, dev.raw.orders
    has no columns of its own; semantix-proof/2 does not borrow raw.orders'
    for it, so that variant is unresolved rather than a contradiction.)"""
    result, store = run_scan(**_estate(tmp_path, PROD_DEV, FULL_CATALOG, DECLARED_M,
                                       default_database="prod"))
    store.close()
    (v,) = [v for v in result.verdicts if v.concept_label == "m"]
    assert engine_verdict(v) == VerdictType.CONTRADICTION
    assert v.verdict_type == VerdictType.SUSPECTED_DIVERGENCE   # conclusions policy
    assert "dev.raw.orders" in v.summary


def test_short_catalog_without_its_own_columns_is_unresolved(tmp_path):
    result, store = run_scan(**_estate(tmp_path, PROD_DEV, SHORT_CATALOG, DECLARED_M,
                                       default_database="prod"))
    store.close()
    kinds = engine_kinds(result.verdicts, "m")
    assert VerdictType.CERTIFIED not in kinds and VerdictType.CONTRADICTION not in kinds


# ------------------------------------- finding 2 + oracle: arithmetic structure

def _core(sql: str):
    registry = SchemaRegistry({"db.main.t": {"a": "DOUBLE", "b": "DOUBLE", "c": "DOUBLE"}})
    canon = canonicalize(sql, registry, "duckdb")
    (fp,) = extract_fingerprints(canon, "db.main.m", Provenance("t", "m"), "duckdb", registry)
    assert fp.canon_level in PROOF_LEVELS
    return fp.core.hash()


@pytest.mark.parametrize("a, b", [
    ("SUM(a + b + c)", "SUM(a + c + b)"),        # reassociation (reviewer)
    ("SUM(a + b + c)", "SUM(a + (b + c))"),      # grouping lost in rendering (oracle)
    ("SUM(a + b + c)", "SUM(c + b + a)"),        # same, via commutation
    ("SUM(a + b - c)", "SUM(a + (b - c))"),
    ("SUM(a * b / c)", "SUM(a * (b / c))"),
    ("SUM(a * b * c)", "SUM(a * (b * c))"),
])
def test_arithmetic_evaluation_order_is_part_of_the_core(a, b):
    assert _core(f"SELECT {a} AS m FROM db.main.t") != _core(f"SELECT {b} AS m FROM db.main.t")


@pytest.mark.parametrize("a, b", [
    ("SUM(a + b)", "SUM(b + a)"),
    ("SUM(a * b)", "SUM(b * a)"),
    ("SUM((c + b) + a)", "SUM(a + (b + c))"),    # commutation only: exact in IEEE
])
def test_commutation_alone_still_proves_sameness(a, b):
    assert _core(f"SELECT {a} AS m FROM db.main.t") == _core(f"SELECT {b} AS m FROM db.main.t")


def test_reassociated_declaration_contradicts(tmp_path):
    result, store = run_scan(**_estate(
        tmp_path, {"m": ("db.main.m", "SELECT SUM(a + b + c) AS m FROM db.main.t")},
        "db:\n  main:\n    t:\n      a: DOUBLE\n      b: DOUBLE\n      c: DOUBLE\n",
        "version: 1\nmetrics:\n  - name: m\n    model: db.main.t\n    expr: \"sum(a + c + b)\"\n"))
    store.close()
    assert engine_kinds(result.verdicts, "m") == {VerdictType.CONTRADICTION}
    assert _kinds(result, "m") == {VerdictType.SUSPECTED_DIVERGENCE}   # conclusions policy


# ------------------------------------------- finding 3: the parser is trusted

def test_grouping_constructs_are_read_in_both_sqlglot_ast_shapes():
    rollup = exp.Rollup(expressions=[exp.Literal.number(1), exp.Literal.number(2)])
    for group in (exp.Group(expressions=[rollup.copy()]),   # sqlglot >= 30.2x
                  exp.Group(rollup=[rollup.copy()])):       # sqlglot <= 30.12
        plain, constructs = split_group(group)
        assert plain == [] and [kind for kind, _ in constructs] == ["rollup"]


def test_rollup_ordinals_resolve_in_the_newer_ast_shape():
    select = sqlglot.parse_one(
        "select status, channel, sum(amount) from t group by rollup(1, 2)", read="duckdb")
    (_, node), = split_group(select.args["group"])[1]
    select.set("group", exp.Group(expressions=[node.copy()]))   # force the 30.2x shape
    _resolve_group_ordinals(select)
    (_, resolved), = split_group(select.args["group"])[1]
    assert [e.name for e in resolved.expressions] == ["status", "channel"]


def test_unqualified_parser_version_withholds_certification(tmp_path, monkeypatch, acme):
    import semantix.engine as engine
    monkeypatch.setattr(engine, "toolchain_status",
                        lambda: {"sqlglot": "99.0.0", "qualified": False, "qualifying": False})
    result, store = run_scan(dbt_path=str(acme / "dbt_project"),
                             metrics_path=str(acme / "metrics.yml"),
                             catalog_path=str(acme / "catalog.yml"),
                             db_path=str(tmp_path / "s.db"))
    store.close()
    # The ENGINE withholds certification (not merely the conclusions policy).
    assert not any(engine_verdict(v) in (VerdictType.CERTIFIED, VerdictType.ALIASING)
                   for v in result.verdicts)
    assert any("sqlglot 99.0.0" in v.summary for v in result.verdicts)
    assert result.stats()["toolchain"]["qualified"] is False


def test_qualification_override_names_one_exact_version(monkeypatch):
    from semantix.toolchain import sqlglot_version, toolchain_status
    monkeypatch.setenv("SEMANTIX_QUALIFY_SQLGLOT", "0.0.0-not-this-one")
    assert toolchain_status()["qualifying"] is False
    monkeypatch.setenv("SEMANTIX_QUALIFY_SQLGLOT", sqlglot_version())
    status = toolchain_status()
    assert status["qualifying"] is True and status["qualified"] is True


def test_dependency_pin_matches_the_qualified_set():
    from pathlib import Path

    from semantix.toolchain import QUALIFIED_SQLGLOT
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    pinned = [line for line in pyproject.splitlines() if '"sqlglot==' in line]
    assert pinned, "sqlglot must be pinned exactly, not ranged"
    version = pinned[0].split("==")[1].split('"')[0]
    assert version in QUALIFIED_SQLGLOT


# ------------------------------------------ finding 4: current-use reliance

def test_edited_sql_without_recheck_is_not_valid_for_current_use(tmp_path):
    from semantix.services.freshness import check_freshness

    args = _estate(tmp_path, {"m": ("db.main.m", "SELECT SUM(amount) AS m FROM db.main.t")},
                   "db:\n  main:\n    t:\n      amount: DOUBLE\n",
                   "version: 1\nmetrics:\n  - name: m\n    model: db.main.t\n    expr: \"sum(amount)\"\n")
    _, store = run_scan(**args)
    model = tmp_path / "estate/models/m.sql"
    model.write_text("-- semantix: relation=db.main.m\nSELECT SUM(amount) * 2 AS m FROM db.main.t\n")
    unchecked = store.issue_receipt("m").as_dict()
    trusted, warnings = receipt_trust(unchecked)
    assert not trusted and "historical attestation" in warnings[0]
    fresh = check_freshness(store, sql_dir=args["sql_dir"], metrics_path=args["metrics_path"])
    assert fresh["status"] == "stale"
    rechecked = store.issue_receipt("m", freshness=fresh).as_dict()
    store.close()
    trusted, warnings = receipt_trust(rechecked)
    assert trusted is False
    # Distrusted for the edit itself, not only because positive verdicts are
    # suspended under the conclusions policy.
    assert any("the estate has moved" in w for w in warnings), warnings


def test_as_of_receipts_remain_historical_attestations():
    receipt = {"status": "CERTIFIED", "as_of": "2026-01-01T00:00:00Z",
               "freshness": {"status": "unchecked"}}
    trusted, warnings = receipt_trust(receipt)
    assert "a record of that instant only" in warnings[0]
    # Still a valid historical record; not valid for its stated use only
    # because its claim (CERTIFIED) is suspended under the conclusions policy.
    from semantix.signing import assess_receipt
    a = assess_receipt(receipt)
    assert a.historical and not a.semantic_claim_usable
    assert trusted is False and any("suspended" in w for w in warnings)


# --------------------------------------------- finding 5: honest coverage

def test_coverage_counts_measures_at_proof_grade_not_parse_success(tmp_path):
    result, store = run_scan(**_estate(
        tmp_path, {"m": ("db.main.m", "SELECT SUM(amount) AS m FROM db.main.t TABLESAMPLE 10 PERCENT")},
        "db:\n  main:\n    t:\n      amount: DOUBLE\n"))
    store.close()
    stats = result.stats()
    assert stats["parse_coverage_pct"] == 100.0
    assert stats["coverage_pct"] == 0.0
    assert stats["verifiable_measures"] == [0, 1]
