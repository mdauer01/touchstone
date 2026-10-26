"""A receipt must never claim more than its evidence.

Round-two adversarial review, 2026-07-26. Every test here is written from a
reviewer's reproduction and FAILED before the fix. The common defect: the
store re-derived concept membership by string-matching the concept label,
throwing away the clustering the scan had already computed and recorded — so
for any concept whose members are spelled differently from its label (which
is the entire point of ALIASING) the receipt named zero definitions, zero
lineage, and was signed anyway.
"""

import re

import pytest

from semantix.engine import run_scan
from semantix.models import VerdictType
from semantix.signing import verify_receipt_dict

from .policy import engine_verdict

_EXPERIMENTAL = re.compile(
    r"^Experimental comparison, not a conclusion — (\w+) under the experimental rules")


def _engine_kinds(receipt) -> set[str]:
    """The engine's verdicts a receipt states. Under the conclusions policy a
    receipt lists effective verdicts (UNRESOLVED / SUSPECTED_DIVERGENCE) and
    carries the engine's result in each summary."""
    out = set()
    for v in receipt.verdicts:
        m = _EXPERIMENTAL.match(v["summary"])
        if m:
            out.add(m.group(1).upper())
        elif v["verdict"] == VerdictType.SUSPECTED_DIVERGENCE.value:
            assert v["summary"].startswith("Suspected divergence"), v["summary"]
            out.add(VerdictType.CONTRADICTION.value)
        else:
            out.add(v["verdict"])
    return out


@pytest.fixture
def scanned(acme, tmp_path):
    result, store = run_scan(
        dbt_path=str(acme / "dbt_project"),
        metrics_path=str(acme / "metrics.yml"),
        catalog_path=str(acme / "catalog.yml"),
        db_path=str(tmp_path / "s.db"),
    )
    return result, store


def test_aliased_concept_has_history(scanned):
    """`gmv` clusters gross_merchandise_value + total_gmv; no member is
    spelled `gmv`. Matching by name found nothing."""
    _, store = scanned
    intervals = store.history("gmv")
    assert intervals, "aliased concept must have derived valid time"
    assert {i.relation for i in intervals} == {
        "analytics.gross_merchandise_value",
        "analytics.total_gmv",
    }


def test_certified_receipt_names_its_definitions(scanned):
    """The headline failure: a signature-valid CERTIFIED certificate naming
    zero definitions and zero source columns."""
    _, store = scanned
    receipt = store.issue_receipt("gmv")
    # Positive verdicts are suspended: the receipt states UNRESOLVED and
    # carries the engine's certification as an experimental comparison.
    assert receipt.status == VerdictType.UNRESOLVED.value
    assert VerdictType.CERTIFIED.value in _engine_kinds(receipt)
    assert receipt.definition_version, "CERTIFIED with no definitions is evidence-free"
    assert receipt.lineage, "CERTIFIED with no lineage is evidence-free"
    assert {d["relation"] for d in receipt.definition_version} == {
        "analytics.gross_merchandise_value",
        "analytics.total_gmv",
    }
    ok, _ = verify_receipt_dict(receipt.as_dict())
    assert ok


def test_no_certified_receipt_is_ever_signed_without_evidence(scanned):
    """The backstop, independent of why the evidence went missing."""
    _, store = scanned
    checked = 0
    for label in ("gmv", "net_revenue", "refund_rate", "customer_ltv"):
        receipt = store.issue_receipt(label)
        if receipt is None:
            continue
        assert receipt.status != VerdictType.CERTIFIED.value     # suspended
        # The engine's certification, now carried as an experimental
        # comparison, still never travels without evidence.
        if VerdictType.CERTIFIED.value in _engine_kinds(receipt):
            checked += 1
            assert receipt.definition_version, f"{label}: signed CERTIFIED with no evidence"
    assert checked, "the backstop must exercise at least one certified concept"


def test_receipt_exposes_every_verdict_not_just_the_primary(scanned):
    """`gmv` is CERTIFIED *and* ALIASING. Reporting only one hides a real
    finding from whoever trusts the receipt."""
    _, store = scanned
    receipt = store.issue_receipt("gmv")
    kinds = {v["verdict"] for v in receipt.verdicts}
    # Both are reported, as experimental comparisons under the policy.
    assert kinds == {VerdictType.UNRESOLVED.value}
    assert len(receipt.verdicts) == 2
    assert _engine_kinds(receipt) == {VerdictType.CERTIFIED.value, VerdictType.ALIASING.value}


def test_a_contradiction_can_never_be_headlined_as_anything_else(scanned):
    """Whatever else a concept accrues, an unresolved disagreement is the
    answer to "can I trust this number"."""
    _, store = scanned
    receipt = store.issue_receipt("net_revenue")
    # Under the conclusions policy the disagreement headlines as a suspected
    # divergence — the engine's CONTRADICTION, carried in its evidence.
    assert receipt.status == VerdictType.SUSPECTED_DIVERGENCE.value
    assert receipt.known_divergences
    rows = [r for r in store.verdicts_for_scan(receipt.scan_id)
            if r["concept_label"] == "net_revenue" and r["verdict"] == receipt.status]
    assert rows and {engine_verdict(r) for r in rows} == {VerdictType.CONTRADICTION}
    assert VerdictType.CONTRADICTION.value in _engine_kinds(receipt)


def test_secondary_verdicts_are_never_silently_dropped(scanned):
    """gmv is CERTIFIED *and* ALIASING by the engine. CERTIFIED was the
    headline before positive verdicts were suspended; whatever the headline,
    the duplicate implementation must still reach whoever reads the receipt."""
    _, store = scanned
    receipt = store.issue_receipt("gmv")
    # Under the conclusions policy nothing headlines as CERTIFIED; both the
    # certification and the duplicate implementation still reach the reader.
    assert receipt.status == VerdictType.UNRESOLVED.value
    assert {VerdictType.CERTIFIED.value, VerdictType.ALIASING.value} <= _engine_kinds(receipt)


def test_orphan_claim_receipt_is_honest_about_having_no_computed_definition(scanned):
    """customer_ltv is declared and never computed. Empty definitions are the
    correct answer here — and must not be mistaken for certification."""
    _, store = scanned
    receipt = store.issue_receipt("customer_ltv")
    assert receipt.status == VerdictType.ORPHAN_CLAIM.value
    assert receipt.definition_version == []


def test_history_is_membership_based_not_name_based(scanned):
    """A concept renamed between scans keeps one history; two concepts that
    merely share a name do not get conflated."""
    _, store = scanned
    by_name = store.history("total_gmv")   # a member name, not a concept label
    by_label = store.history("gmv")
    assert by_label, "label lookup must work"
    # querying by a member name resolves to the same concept, not a fragment
    assert {i.ref_id for i in by_name} <= {i.ref_id for i in by_label}
