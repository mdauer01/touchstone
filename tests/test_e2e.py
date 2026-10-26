from semantix.store import normalize_ts
"""End-to-end: scan the acme demo estate and check every verdict lands."""

import shutil


import semantix.store as store_mod
from semantix.engine import run_scan
from semantix.models import VerdictType
from semantix.report.html import render_report, report_json


def scan(acme, db_path, label="test"):
    return run_scan(
        dbt_path=str(acme / "dbt_project"),
        metrics_path=str(acme / "metrics.yml"),
        catalog_path=str(acme / "catalog.yml"),
        db_path=str(db_path),
        label=label,
    )


def test_acme_estate_produces_all_verdict_types(acme, tmp_path):
    result, store = scan(acme, tmp_path / "e2e.db")

    assert result.artifacts == 7
    assert len(result.fingerprints) == 5
    # ops_refund_rate joins refunds to orders: outside the proof grammar
    # (semantix-proof/2), so 4 of 5 definitions are proof grade and the fifth
    # names its reason.
    assert result.coverage == 80.0
    (joined,) = [fp for fp in result.fingerprints if fp.concept_candidate == "refund_rate"]
    assert joined.gaps == ("outside the supported grammar (semantix-proof/2): uses a join",)

    by_concept = {}
    for v in result.verdicts:
        by_concept.setdefault(v.concept_label, set()).add(v.verdict_type)

    assert by_concept["net_revenue"] == {VerdictType.CONTRADICTION}
    assert by_concept["gmv"] == {VerdictType.CERTIFIED, VerdictType.ALIASING}
    assert by_concept["customer_ltv"] == {VerdictType.ORPHAN_CLAIM}
    assert by_concept["refund_rate"] == {VerdictType.SHADOW_METRIC}

    contradiction = next(v for v in result.verdicts if v.verdict_type == VerdictType.CONTRADICTION)
    assert "analytics.growth_net_revenue" in contradiction.summary
    assert contradiction.evidence["component_diff"]["grain"] is True
    assert contradiction.evidence["component_diff"]["expression"] is False
    # highest severity item in the scan is the contradiction
    assert result.verdicts[0].verdict_type == VerdictType.CONTRADICTION


def test_report_renders(acme, tmp_path):
    result, store = scan(acme, tmp_path / "r.db")
    meta = {"scan_id": result.scan_id, "observed_at": result.observed_at, "dialect": "duckdb", **result.stats()}
    html = render_report(result.concepts, result.verdicts, store, meta)
    for needle in (
        "net_revenue",
        "CONTRADICTION",
        "CERTIFIED",
        "ALIASING",
        "ORPHAN_CLAIM",
        "SHADOW_METRIC",
        "gross_merchandise_value",
        "raw.orders.refund_amount",
    ):
        assert needle in html
    js = report_json(result.verdicts, meta)
    assert '"CONTRADICTION"' in js


def test_definition_change_shows_in_history_and_asof(acme, tmp_path, monkeypatch):
    times = iter(["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"])
    current = {}

    def fake_utcnow():
        return current["t"]

    monkeypatch.setattr(store_mod, "utcnow", fake_utcnow)

    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "h.db"

    current["t"] = next(times)
    scan(estate, db, label="before")

    # growth team "fixes" its model: definition changes at the same site
    growth = (
        estate
        / "dbt_project/target/compiled/acme/models/growth/growth_net_revenue.sql"
    )
    growth.write_text(
        """select date_trunc('month', o.order_date) as revenue_month,
       sum(o.amount - o.refund_amount) as net_revenue
from analytics.stg_orders as o
where o.status = 'completed'
group by 1
"""
    )
    current["t"] = next(times)
    _, store = scan(estate, db, label="after")

    growth_site = [
        iv for iv in store.history("net_revenue") if "growth" in (iv.relation or "")
    ]
    assert len(growth_site) == 2
    old, new = sorted(growth_site, key=lambda iv: iv.valid_from)
    assert old.valid_to == normalize_ts("2026-02-01T00:00:00Z")
    assert new.valid_to is None

    # bitemporal as-of: January's meaning is still answerable after the change
    live_jan = store.as_of("net_revenue", "2026-01-15T00:00:00Z")
    assert any(iv.ref_id == old.ref_id for iv in live_jan)

    # a Jan-vs-Feb comparison spans a definition boundary → restatement
    assert store.restatement_check(
        "net_revenue", "2026-01-15T00:00:00Z", "2026-02-15T00:00:00Z"
    )

    receipt = store.issue_receipt("net_revenue")
    assert receipt is not None
    assert receipt.status in ("CONTRADICTION", "CERTIFIED")
    assert receipt.definition_version
