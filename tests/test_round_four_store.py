"""Round four: absence, receipts, and reads that outran their evidence.

2026-07-28. The store's two load-bearing claims are that it is append-only and
that ABSENCE REQUIRES PROOF. Each test here is a case where a read answered
with more confidence than the observation log supports — a retirement date
manufactured from an I/O error, a signed certificate for an instant nobody has
observed, a clean bill of health read out of a scan that never finished.
"""

import json
import shutil

import pytest

from semantix.engine import run_scan
from semantix.signing import verify_receipt_dict
from semantix.store import Store

from .policy import engine_verdict


@pytest.fixture
def clock(monkeypatch):
    import semantix.store as store_mod
    times = iter([f"2026-0{m}-01T00:00:00Z" for m in range(1, 9)])
    now = {"t": next(times)}
    monkeypatch.setattr(store_mod, "utcnow", lambda: now["t"])
    return lambda: now.__setitem__("t", next(times))


def scan(estate, db, **kw):
    return run_scan(
        dbt_path=str(estate / "dbt_project"),
        metrics_path=str(estate / "metrics.yml"),
        catalog_path=str(estate / "catalog.yml"),
        db_path=str(db),
        **kw,
    )


# ------------------------------------------------------ absence needs proof

def test_scanning_another_project_does_not_retire_this_ones_definitions(
        acme, tmp_path, clock):
    """Absence was concluded from "this scan touched the SYSTEM", which is not
    evidence about an individual artifact. Pointing a second scan at an
    unrelated dbt project retired every definition of the first — both are
    system == "dbt" — while the models sat untouched on disk. Re-scanning then
    produced a second interval, so the store permanently recorded a fictitious
    death-and-rebirth cycle."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"
    scan(estate, db)

    other = tmp_path / "other" / "dbt_project"
    other.mkdir(parents=True)
    (other / "manifest.json").write_text(json.dumps({"nodes": {
        "model.other.headcount": {
            "resource_type": "model", "name": "headcount", "schema": "hr",
            "alias": "headcount", "relation_name": "hr.headcount",
            "compiled_code": "select count(*) as headcount from raw.people",
            "original_file_path": "models/headcount.sql",
            "depends_on": {"nodes": []},
        }}}))
    clock()
    _, store = run_scan(dbt_path=str(other), db_path=str(db))

    assert (estate / "dbt_project").exists()
    live = {i.relation for i in store.as_of("net_revenue", "2026-03-01T00:00:00Z")}
    assert live == {"analytics.finance_net_revenue", "analytics.growth_net_revenue"}


def test_a_genuine_deletion_still_ends_validity(acme, tmp_path, clock):
    """The other half. A deleted model and a model in another project are both
    simply ABSENT, so the discriminator is overlap — this scan re-read other
    models of the same project, so it is speaking about the same estate."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"
    scan(estate, db)

    manifest = estate / "dbt_project" / "target" / "manifest.json"
    data = json.loads(manifest.read_text())
    removed = [k for k in data["nodes"] if k.endswith("growth_net_revenue")]
    assert removed, "fixture changed"
    for key in removed:
        del data["nodes"][key]
    manifest.write_text(json.dumps(data))

    clock()
    _, store = scan(estate, db)
    live = {i.relation for i in store.as_of("net_revenue", "2026-03-01T00:00:00Z")}
    assert "analytics.growth_net_revenue" not in live
    assert "analytics.finance_net_revenue" in live


def test_two_sites_computing_the_same_thing_keep_their_own_relations(
        acme, tmp_path, clock):
    """`fingerprints.relation` lives on a CONTENT-ADDRESSED row shared by every
    site computing the same thing (record_fingerprint is INSERT OR IGNORE), so
    the first site recorded won and a signed receipt attributed one team's
    definition to the other team's table."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"

    manifest = estate / "dbt_project" / "target" / "manifest.json"
    data = json.loads(manifest.read_text())
    project = estate / "dbt_project"
    finance = next(v for k, v in data["nodes"].items() if k.endswith("finance_net_revenue"))
    growth = next(v for k, v in data["nodes"].items() if k.endswith("growth_net_revenue"))
    # Make the two models semantically identical, so both sites share one
    # content-addressed fingerprint row.
    (project / growth["compiled_path"]).write_text(
        (project / finance["compiled_path"]).read_text())

    _, store = scan(estate, db)
    relations = {i.relation for i in store.history("net_revenue")}
    assert "analytics.finance_net_revenue" in relations
    assert "analytics.growth_net_revenue" in relations, \
        "one site's definition was attributed to the other site's relation"


# ------------------------------------------------------------ current reads

def test_an_interrupted_scan_does_not_report_a_clean_estate(acme, tmp_path, clock):
    """A scan that died partway wrote no stats and describes nothing, but it
    was the newest row — so `latest_scan`, and therefore concepts / attest /
    publish / every MCP tool, answered from it and reported a clean estate
    while the store held a recorded CONTRADICTION."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"
    result, store = scan(estate, db)
    assert any(engine_verdict(v).value == "CONTRADICTION" for v in result.verdicts)

    clock()
    store.begin_scan("interrupted", "duckdb")  # process dies here

    latest = store.latest_scan()
    verdicts = store.verdicts_for_scan(latest["scan_id"])
    # Read back as the policy reports it: a suspected divergence carrying the
    # engine's CONTRADICTION.
    assert any(v["verdict"] == "SUSPECTED_DIVERGENCE"
               and engine_verdict(v).value == "CONTRADICTION" for v in verdicts), \
        "an interrupted scan erased the estate's known contradiction from every read"


# --------------------------------------------------------------- receipts

def test_a_receipt_cannot_certify_a_future_instant(acme, tmp_path, clock):
    """Reachable from `attest --as-of` and from the MCP attest tool, whose
    schema invites an agent to pass "the timestamp the answer refers to"."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    _, store = scan(estate, tmp_path / "s.db")
    receipt = store.issue_receipt("gmv", as_of="2099-01-01T00:00:00Z")
    assert receipt.status != "CERTIFIED"
    assert any("future" in d for d in receipt.known_divergences)


def test_a_stale_freshness_block_blocks_certification(acme, tmp_path, clock):
    """One signed document said "this definition is live at issue time" and
    "this definition no longer exists in the estate" in the same breath."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    _, store = scan(estate, tmp_path / "s.db")
    receipt = store.issue_receipt(
        "gmv", freshness={"status": "stale", "removed": ["dbt:model.acme.gmv"]})
    assert receipt.status != "CERTIFIED"
    assert receipt.status == "UNRESOLVED"
    assert receipt.scope.get("freshness") == "stale"
    assert any("freshness is stale" in d for d in receipt.known_divergences)


def test_an_empty_pin_is_refused_at_the_api(tmp_path):
    """Same rule, exercised without a store: an empty pin must never silently
    downgrade to the embedded key."""
    from semantix.signing import ALG
    fake = {"signature": {"alg": ALG, "signature": "x", "public_key": "y"}}
    ok, detail = verify_receipt_dict(fake, public_key_b64="")
    assert ok is False
    assert "empty" in detail


def test_a_shadow_metric_receipt_needs_a_computed_definition(tmp_path, clock):
    """SHADOW_METRIC asserts "computed at N sites" — an existence claim about a
    computed definition. It was outside `evidence_required`, so a receipt could
    make that claim with definition_version: [] and lineage: []."""
    store = Store(tmp_path / "s.db")
    scan_id, observed_at = store.begin_scan("t", "duckdb")
    store.finish_scan(scan_id, {})
    # No fingerprints recorded at all, so nothing substantiates the claim.
    assert store.issue_receipt("nothing_here") is None
