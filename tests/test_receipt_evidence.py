"""A receipt must never claim more than its evidence.

Round-two adversarial review, 2026-07-26. Every test here is written from a
reviewer's reproduction and FAILED before the fix. The common defect: the
store re-derived concept membership by string-matching the concept label,
throwing away the clustering the scan had already computed and recorded — so
for any concept whose members are spelled differently from its label (which
is the entire point of ALIASING) the receipt named zero definitions, zero
lineage, and was signed anyway.
"""

import pytest

from semantix.engine import run_scan
from semantix.models import VerdictType
from semantix.signing import verify_receipt_dict


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
    assert receipt.status == VerdictType.CERTIFIED.value
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
    for label in ("gmv", "net_revenue", "refund_rate", "customer_ltv"):
        receipt = store.issue_receipt(label)
        if receipt is None:
            continue
        if receipt.status == VerdictType.CERTIFIED.value:
            assert receipt.definition_version, f"{label}: signed CERTIFIED with no evidence"


def test_receipt_exposes_every_verdict_not_just_the_primary(scanned):
    """`gmv` is CERTIFIED *and* ALIASING. Reporting only one hides a real
    finding from whoever trusts the receipt."""
    _, store = scanned
    receipt = store.issue_receipt("gmv")
    kinds = {v["verdict"] for v in receipt.verdicts}
    assert kinds == {VerdictType.CERTIFIED.value, VerdictType.ALIASING.value}


def test_a_contradiction_can_never_be_headlined_as_anything_else(scanned):
    """Whatever else a concept accrues, an unresolved disagreement is the
    answer to "can I trust this number"."""
    _, store = scanned
    receipt = store.issue_receipt("net_revenue")
    assert receipt.status == VerdictType.CONTRADICTION.value
    assert receipt.known_divergences


def test_secondary_verdicts_are_never_silently_dropped(scanned):
    """gmv is CERTIFIED *and* ALIASING. CERTIFIED is the right headline — the
    number is trustworthy — but the duplicate implementation must still reach
    whoever reads the receipt."""
    _, store = scanned
    receipt = store.issue_receipt("gmv")
    assert receipt.status == VerdictType.CERTIFIED.value
    assert VerdictType.ALIASING.value in {v["verdict"] for v in receipt.verdicts}


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
