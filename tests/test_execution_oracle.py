"""The no-false-certification guarantee, checked by EXECUTING SQL.

Every pair of generated queries the engine would certify as equal is run
against adversarial DuckDB datasets; their results must agree. This test
does not ask the canonicalizer whether it is right.
"""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

import touchstone.oracle as oracle  # noqa: E402


def test_every_pair_the_engine_equates_executes_identically():
    report = oracle.run_oracle()
    assert not report.self_test_failures, report.self_test_failures
    assert not report.mismatches, report.mismatches[:3]
    # Non-vacuous: a quiet oracle must have compared something.
    assert report.buckets >= 50 and report.pairs_executed >= 500


def test_every_proof_form_executes_as_its_original():
    """touchstone-proof/2: each proof-grade query's trusted transformed SQL is
    run against the query as written, on every dataset — NULLs, duplicates,
    empty input, float cancellation and overflow, integer extremes."""
    report = oracle.run_oracle()
    assert not report.transform_mismatches, report.transform_mismatches[:3]
    assert report.proof_grade >= 250
    assert report.transforms_executed >= 250 * len(oracle.DATASETS)


def test_the_oracle_catches_a_wrong_proof_form(monkeypatch):
    """A rule that rewrote meaning must be caught however buckets fall."""
    real = oracle._buckets

    def poisoned(estate, queries):
        out = real(estate, queries)
        if estate == "main":
            oracle._TRANSFORMED["SELECT SUM(a + b + c) AS m FROM db.main.orders"] = \
                "SELECT SUM(a + c + b) AS m FROM db.main.orders"
        return out

    monkeypatch.setattr(oracle, "_buckets", poisoned)
    report = oracle.run_oracle()
    assert any(m["original"].startswith("SELECT SUM(a + b + c)")
               for m in report.transform_mismatches)


def test_the_oracle_catches_an_injected_false_certification(monkeypatch):
    """Detection must not depend on the engine's current behaviour: plant the
    round-eight reassociation pair in one bucket and the oracle must flag it."""
    real = oracle._buckets

    def poisoned(estate, queries):
        buckets, proof = real(estate, queries)
        if estate == "main":
            buckets["poisoned"] = ["SELECT SUM(a + b + c) AS m FROM db.main.orders",
                                   "SELECT SUM(a + c + b) AS m FROM db.main.orders"]
        return buckets, proof

    monkeypatch.setattr(oracle, "_buckets", poisoned)
    report = oracle.run_oracle()
    assert any(m["hash"] == "poisoned" for m in report.mismatches)


# ------------------------------------------------- conformance challenger

def test_every_conforming_verdict_executes_as_definition_plus_variation():
    report = oracle.run_conformance_oracle()
    assert not report.self_test_failures, report.self_test_failures
    assert not report.mismatches, report.mismatches[:3]
    assert not report.near_miss_conformed, report.near_miss_conformed[:3]
    assert not report.false_accusations, report.false_accusations[:3]
    # Non-vacuous: the rules said "conforming" often, and near misses ran.
    assert report.conforming >= 300 and report.near_misses >= 40
    assert report.executions >= report.conforming * len(oracle.DATASETS)


def test_conformance_challenger_catches_a_planted_wrong_verdict(monkeypatch):
    """Plant a rule bug — a dropped required filter judged conforming — and
    execution must expose it, whatever the rules currently do."""
    from touchstone.services import conformance

    real = conformance.check_raw

    def lenient(core, auth, dialect, shape_problem, models=frozenset(), hypothesis=None):
        outcome = real(core, auth, dialect, shape_problem, models, hypothesis)
        if outcome.reasons and outcome.reasons[0]["code"] == "required_filter_missing":
            return conformance.Outcome("conforming", shape="A")
        return outcome

    monkeypatch.setattr(conformance, "check_raw", lenient)
    report = oracle.run_conformance_oracle()
    assert report.near_miss_conformed            # the policy check fires
    # Relabel every near miss as an ordinary case: the planted "conforming"
    # verdicts are then executed, and execution must disagree with them.
    monkeypatch.setattr(oracle, "generate_conformance_cases",
                        lambda full=False: [(a, q, False) for a, q, _ in _real_cases(full)])
    report = oracle.run_conformance_oracle()
    assert report.mismatches                     # and so does execution


_real_cases = oracle.generate_conformance_cases
