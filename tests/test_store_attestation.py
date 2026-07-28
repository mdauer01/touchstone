"""Attestation must not misdate itself.

A receipt is a signed claim about a metric at an instant. If the instant and
the verdict come from different moments, the signature certifies a statement
the store's own history contradicts. Found by adversarial review, 2026-07-25.
"""

import json
import shutil

import semantix.store as store_mod
from semantix.engine import run_scan
from semantix.signing import verify_receipt_dict
from semantix.store import Store, normalize_ts

GROWTH = "dbt_project/target/compiled/acme/models/growth/growth_net_revenue.sql"
# Growth aligning with finance: same expression, same filter, and crucially
# the same source relation (reading stg_orders instead of fct_orders is itself
# a divergence, which is what the join graph is for).
ALIGNED = (
    "select date_trunc('month', o.order_date) as revenue_month,\n"
    "       sum(o.amount - o.refund_amount) as net_revenue\n"
    "from analytics.fct_orders as o\n"
    "where o.status = 'completed'\n"
    "group by 1\n"
)


def scan(estate, db, **kw):
    return run_scan(
        dbt_path=str(estate / "dbt_project"),
        metrics_path=str(estate / "metrics.yml"),
        catalog_path=str(estate / "catalog.yml"),
        db_path=str(db),
        **kw,
    )


def test_normalize_ts_canonicalizes_offsets():
    assert normalize_ts("2026-03-01T00:00:00+00:00") == "2026-03-01T00:00:00Z"
    assert normalize_ts("2026-03-01T05:00:00+05:00") == "2026-03-01T00:00:00Z"
    assert normalize_ts("2026-03-01T00:00:00Z") == "2026-03-01T00:00:00Z"
    # Unparseable input RAISES. Passing it through does not fail — text
    # comparison against '03/01/2026' silently answers with the wrong
    # definition and then signs it, which is worse than an error.
    import pytest as _pytest

    with _pytest.raises(ValueError):
        normalize_ts("not a timestamp")


def test_as_of_accepts_offset_timestamps(acme, tmp_path, monkeypatch):
    times = iter(["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"])
    now = {}
    monkeypatch.setattr(store_mod, "utcnow", lambda: now["t"])

    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"
    now["t"] = next(times)
    scan(estate, db)
    (estate / GROWTH).write_text(ALIGNED)
    now["t"] = next(times)
    _, store = scan(estate, db)

    # The same instant in two representations must resolve identically.
    z = store.as_of("net_revenue", "2026-01-15T00:00:00Z")
    offset = store.as_of("net_revenue", "2026-01-15T00:00:00+00:00")
    shifted = store.as_of("net_revenue", "2026-01-15T05:00:00+05:00")
    assert {i.ref_id for i in z} == {i.ref_id for i in offset} == {i.ref_id for i in shifted}
    assert z, "January definitions must still be resolvable"


def test_as_of_receipt_carries_the_verdict_that_was_true_then(acme, tmp_path, monkeypatch):
    """The critical one: growth's model contradicted finance in January and
    agreed in February. An as-of receipt for January must say CONTRADICTION —
    stamping February's CERTIFIED onto it would sign the opposite of what the
    store records."""
    times = iter(["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"])
    now = {}
    monkeypatch.setattr(store_mod, "utcnow", lambda: now["t"])

    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"

    now["t"] = next(times)
    before, _ = scan(estate, db)
    january_verdict = next(v.verdict_type.value for v in before.verdicts
                           if v.concept_label == "net_revenue")

    (estate / GROWTH).write_text(ALIGNED)
    now["t"] = next(times)
    after, store = scan(estate, db)
    february_verdict = next(v.verdict_type.value for v in after.verdicts
                            if v.concept_label == "net_revenue")

    assert january_verdict == "CONTRADICTION"
    assert february_verdict != january_verdict, "estate must actually have changed"

    historical = store.issue_receipt("net_revenue", as_of="2026-01-15T00:00:00Z")
    assert historical.status == january_verdict
    current = store.issue_receipt("net_revenue")
    assert current.status == february_verdict

    # and the historical claim is signed, so it is evidence
    ok, _ = verify_receipt_dict(historical.as_dict())
    assert ok
    assert historical.definition_version


def test_receipt_before_any_scan_is_refused(acme, tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "utcnow", lambda: "2026-05-01T00:00:00Z")
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    _, store = scan(estate, tmp_path / "s.db")
    # An instant predating the store's first observation has no verdicts to
    # report; inventing one would be the whole failure mode in miniature.
    assert store.issue_receipt("net_revenue", as_of="2020-01-01T00:00:00Z") is None


def test_receipt_never_signs_an_empty_definition(acme, tmp_path):
    """The store and the resolver must agree on name normalization, or a
    concept clustered under one spelling cannot be found under the other and
    the receipt ships signed but empty."""
    _, store = scan(acme, tmp_path / "s.db")
    for label in ("net_revenue", "Net Revenue", "NET-REVENUE"):
        receipt = store.issue_receipt(label)
        assert receipt is not None, label
        assert receipt.definition_version, label
        assert receipt.lineage, label


def test_deleted_definition_site_stops_being_live(acme, tmp_path, monkeypatch):
    """A model that no longer exists is not still computing. Without reading
    absence, its last observation stays open forever and the deleted model
    keeps appearing in the current CERTIFIED receipt."""
    times = iter(["2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z"])
    now = {}
    monkeypatch.setattr(store_mod, "utcnow", lambda: now["t"])

    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"

    now["t"] = next(times)
    scan(estate, db)
    live_before = {i.relation for i in Store(str(db)).as_of("net_revenue", "2026-01-15T00:00:00Z")}
    assert "analytics.growth_net_revenue" in live_before

    # growth's model is deleted from the project entirely
    (estate / GROWTH).unlink()
    manifest = estate / "dbt_project" / "target" / "manifest.json"
    data = json.loads(manifest.read_text())
    del data["nodes"]["model.acme.growth_net_revenue"]
    manifest.write_text(json.dumps(data))

    now["t"] = next(times)
    _, store = scan(estate, db)

    # January still shows it — history is not rewritten
    assert "analytics.growth_net_revenue" in {
        i.relation for i in store.as_of("net_revenue", "2026-01-15T00:00:00Z")
    }
    # but it is no longer live, and no longer in the current receipt
    assert "analytics.growth_net_revenue" not in {
        i.relation for i in store.as_of("net_revenue", "2026-02-15T00:00:00Z")
    }
    receipt = store.issue_receipt("net_revenue")
    assert all("growth" not in (d["relation"] or "") for d in receipt.definition_version)
    closed = [i for i in store.history("net_revenue") if i.valid_to is not None]
    assert any(i.relation == "analytics.growth_net_revenue" for i in closed)


def test_absence_needs_coverage_of_that_system(acme, tmp_path, monkeypatch):
    """Only a scan that READ a system may assert absence within it. A scan of
    just the dbt project says nothing about metrics-yaml claims, and must not
    silently retire them."""
    times = iter(["2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z"])
    now = {}
    monkeypatch.setattr(store_mod, "utcnow", lambda: now["t"])

    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"

    now["t"] = next(times)
    scan(estate, db)
    now["t"] = next(times)
    # rescan WITHOUT the metrics source; dbt definitions are untouched
    _, store = run_scan(dbt_path=str(estate / "dbt_project"),
                        catalog_path=str(estate / "catalog.yml"), db_path=str(db))

    live = {i.relation for i in store.as_of("net_revenue", "2026-04-15T00:00:00Z")}
    assert "analytics.finance_net_revenue" in live
    assert "analytics.growth_net_revenue" in live


def test_store_carries_no_private_normalizer():
    """The store's old private copy disagreed with the resolver's on names
    with digits or punctuation — the cause of the empty-receipt bug above."""
    from semantix.services.resolution import normalize_name

    assert not hasattr(store_mod, "_norm")
    assert store_mod.normalize_name is normalize_name
    assert normalize_name("Net Revenue 2") == "net_revenue_2"
