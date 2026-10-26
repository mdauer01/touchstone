"""A scan that could not read the estate must not certify it.

Round-two adversarial review, 2026-07-26. The absence rule added on 07-25
inferred "this definition is gone" from mere SYSTEM-level scan coverage,
collapsing three different states into one:

  (a) read, and genuinely deleted        -> validity legitimately ends
  (b) read, but unparseable              -> unknown
  (c) never selected by this scan        -> unknown

Only (a) justifies an end-date. Treating (b) and (c) the same way lets a
routine `dbt build --select finance` erase the model that contradicts a
declared metric — and the scan then reports CERTIFIED, having printed
`! SKIPPED` for the very definition it just dropped.
"""

import json
import shutil

import pytest

import semantix.store as store_mod
from semantix.engine import run_scan
from semantix.models import VerdictType

from .policy import engine_kinds, engine_verdict

GROWTH = "dbt_project/target/compiled/acme/models/growth/growth_net_revenue.sql"


@pytest.fixture
def clock(monkeypatch):
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


def test_partial_compile_does_not_erase_a_contradiction(acme, tmp_path, clock):
    """The headline failure, reproduced through the engine: growth's compiled
    SQL is missing (as after `dbt build --select finance`). The engine knows —
    it reports the model as skipped — so it must not conclude the definition
    was retired, and must not certify."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"

    before, _ = scan(estate, db)
    # The engine contradicts; the policy states it as a suspected divergence.
    assert any(engine_verdict(v) == VerdictType.CONTRADICTION
               and v.verdict_type == VerdictType.SUSPECTED_DIVERGENCE
               for v in before.verdicts if v.concept_label == "net_revenue")

    (estate / GROWTH).unlink()
    clock()
    after, store = scan(estate, db)

    assert after.skipped, "the model must be reported as unreadable"
    verdicts = {v.verdict_type for v in after.verdicts if v.concept_label == "net_revenue"}
    assert VerdictType.CERTIFIED not in verdicts, (
        "certified while a contradicting definition was unreadable"
    )
    # The policy hides any certification, so the check that matters is the
    # engine's own judgment: it must not certify either, not even
    # experimentally.
    assert VerdictType.CERTIFIED not in engine_kinds(after.verdicts, "net_revenue"), (
        "certified while a contradicting definition was unreadable"
    )

    receipt = store.issue_receipt("net_revenue")
    assert receipt.status != VerdictType.CERTIFIED.value
    assert not any(v["summary"].startswith(
        "Experimental comparison, not a conclusion — certified ") for v in receipt.verdicts)
    assert receipt.known_divergences


def test_unreadable_site_stays_live_not_retired(acme, tmp_path, clock):
    """An unparseable or unreadable definition is UNKNOWN, not deleted. Its
    valid interval must stay open rather than acquiring a fabricated end."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"

    scan(estate, db)
    (estate / GROWTH).unlink()
    clock()
    _, store = scan(estate, db)

    growth = [i for i in store.history("net_revenue")
              if "growth" in (i.relation or "")]
    assert growth, "history must not lose the site"
    assert all(i.valid_to is None for i in growth), (
        "an unreadable definition was given a fabricated retirement date"
    )


def test_genuine_deletion_still_ends_validity(acme, tmp_path, clock):
    """The feature must still work: a model removed from the manifest — proof
    the scan looked and it was not there — does end validity."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"

    scan(estate, db)
    (estate / GROWTH).unlink()
    manifest = estate / "dbt_project" / "target" / "manifest.json"
    data = json.loads(manifest.read_text())
    del data["nodes"]["model.acme.growth_net_revenue"]
    manifest.write_text(json.dumps(data))
    clock()
    _, store = scan(estate, db)

    growth = [i for i in store.history("net_revenue")
              if "growth" in (i.relation or "")]
    assert growth, "history is never rewritten"
    assert all(i.valid_to is not None for i in growth), (
        "a genuinely deleted definition must stop being live"
    )


def test_narrowed_scan_does_not_retire_unselected_definitions(acme, tmp_path, clock):
    """`dbt --select` equivalents: a scan that only looked at part of the
    estate says nothing about the rest."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"

    scan(estate, db)
    clock()
    # Rescan reading only the metrics side; the dbt models were never examined,
    # so this scan is entitled to say nothing at all about them.
    (tmp_path / "empty_sql").mkdir(exist_ok=True)
    _, store = run_scan(
        sql_dir=str(tmp_path / "empty_sql"),
        metrics_path=str(estate / "metrics.yml"),
        catalog_path=str(estate / "catalog.yml"),
        db_path=str(db),
    )
    live = {i.relation for i in store.as_of("net_revenue", "2026-03-15T00:00:00Z")}
    assert "analytics.finance_net_revenue" in live
    assert "analytics.growth_net_revenue" in live
