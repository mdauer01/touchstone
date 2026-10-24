"""The no-false-certification guarantee, checked by EXECUTING SQL.

Every pair of generated queries the engine would certify as equal is run
against adversarial DuckDB datasets; their results must agree. This test
does not ask the canonicalizer whether it is right.
"""

from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

import semantix.oracle as oracle  # noqa: E402


def test_every_pair_the_engine_equates_executes_identically():
    report = oracle.run_oracle()
    assert not report.self_test_failures, report.self_test_failures
    assert not report.mismatches, report.mismatches[:3]
    # Non-vacuous: a quiet oracle must have compared something.
    assert report.buckets >= 50 and report.pairs_executed >= 500


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
