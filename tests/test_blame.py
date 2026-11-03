"""Semantic blame: verdicts name the exact clause that diverges."""

from touchstone.engine import run_scan
from touchstone.models import VerdictType
from touchstone.services.blame import blame_members

from .policy import engine_verdict


def _member(member_id, kind, where, core_hash, *, expression="SUM(t0.amount)",
            filters=(), grain=(), joins=("analytics.fct_orders",), lineage=()):
    return {
        "member_id": member_id, "kind": kind, "name": "m", "where": where,
        "path": None, "canon_level": "supported", "core_hash": core_hash,
        "components": {
            "expression": expression,
            "filters": list(filters),
            "grain": list(grain),
            "join graph": list(joins),
            "physical lineage": list(lineage),
        },
    }


def test_declared_member_is_baseline_and_deltas_are_minimal():
    members = [
        _member("c1", "declared", "analytics.fct_orders", "aaa",
                expression="SUM(t0.amount - t0.refund_amount)",
                filters=['t0."status" = \'completed\''],
                lineage=["raw.orders.amount", "raw.orders.refund_amount"]),
        _member("f1", "computed", "analytics.growth_net_revenue", "bbb",
                expression="SUM(t0.amount)",
                filters=["t0.\"status\" IN ('completed', 'pending')"],
                joins=("analytics.stg_orders",),
                lineage=["raw.orders.amount"]),
        _member("f2", "computed", "analytics.finance_net_revenue", "aaa",
                expression="SUM(t0.amount - t0.refund_amount)",
                filters=['t0."status" = \'completed\''],
                lineage=["raw.orders.amount", "raw.orders.refund_amount"]),
    ]
    blame = blame_members(members)
    assert blame["baseline"]["member_id"] == "c1"
    assert len(blame["divergent"]) == 1
    d = blame["divergent"][0]
    assert d["where"] == "analytics.growth_net_revenue"
    assert d["filters"]["missing"] == ['t0."status" = \'completed\'']
    assert d["expression"]["missing_refs"] == ["refund_amount"]
    assert d["sources"] == {"missing": ["analytics.fct_orders"],
                            "extra": ["analytics.stg_orders"]}
    assert "missing filter status = 'completed'" in d["sentence"]
    assert "expression omits refund_amount" in d["sentence"]
    assert "reads analytics.stg_orders instead of analytics.fct_orders" in d["sentence"]


def test_majority_core_is_baseline_when_nothing_is_declared():
    members = [
        _member("f1", "computed", "a.m1", "aaa"),
        _member("f2", "computed", "a.m2", "aaa"),
        _member("f3", "computed", "a.m3", "zzz", filters=["t0.x > 0"]),
    ]
    blame = blame_members(members)
    assert blame["baseline"]["member_id"] == "f1"
    assert [d["member_id"] for d in blame["divergent"]] == ["f3"]
    assert blame["divergent"][0]["filters"]["extra"] == ["t0.x > 0"]


def test_acme_contradiction_summary_names_the_clauses(acme, tmp_path):
    result, _ = run_scan(
        dbt_path=str(acme / "dbt_project"),
        metrics_path=str(acme / "metrics.yml"),
        catalog_path=str(acme / "catalog.yml"),
        db_path=str(tmp_path / "b.db"),
    )
    # The engine's CONTRADICTION surfaces as a suspected divergence under the
    # conclusions policy; its summary and blame are carried unchanged.
    v = next(v for v in result.verdicts if engine_verdict(v) == VerdictType.CONTRADICTION)
    assert v.verdict_type == VerdictType.SUSPECTED_DIVERGENCE
    assert "missing filter status = 'completed'" in v.summary
    assert "expression omits refund_amount" in v.summary
    assert "reads analytics.stg_orders instead of analytics.fct_orders" in v.summary
    blame = v.evidence["blame"]
    assert blame["baseline"]["kind"] == "declared"
    assert blame["divergent"][0]["where"] == "analytics.growth_net_revenue"
