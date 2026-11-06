"""A deterministic stand-in backend. It reads nothing but the record id.

Its answers are a hash of (seed, record id, component), so runs are
reproducible and every code path — answers, aggregation, probabilities,
metrics — is exercised. It knows nothing about SQL: whatever it scores
says nothing about how any real classifier performs.
"""

from __future__ import annotations

import hashlib

from touchstone.assess import ADVISORY_LABELS
from touchstone.assess.criteria import Criteria, aggregate
from touchstone.assess.providers import Assessment


class FakeProvider:
    name = "fake"
    model = None
    measures_performance = False
    confidence_source = "synthetic (fake backend — a hash, not a judgment)"

    def __init__(self, seed: str = ""):
        self.seed = seed

    def _pick(self, record_id: str, component: str, answers: list[str]) -> str:
        digest = hashlib.sha256(f"{self.seed}|{record_id}|{component}".encode()).digest()
        return answers[digest[0] % len(answers)]

    def assess(self, records: list[dict], criteria: Criteria) -> list[Assessment]:
        out = []
        for record in records:
            rid = record["record_id"]
            answers = {c: self._pick(rid, c, criteria.answers(c)) for c in criteria.components}
            # v6 evidence: placeholder text, so every review state can occur.
            answers.update({t: self._pick(rid, t, ["", f"fake {t}"]) for t, _ in criteria.texts})
            label = aggregate(answers, criteria)
            digest = hashlib.sha256(f"{self.seed}|{rid}|p".encode()).digest()
            confidence = 0.34 + (digest[1] / 255) * 0.66
            rest = (1.0 - confidence) / (len(ADVISORY_LABELS) - 1)
            probabilities = {lab: (confidence if lab == label else rest)
                             for lab in ADVISORY_LABELS}
            out.append(Assessment(record_id=rid, provider=self.name, model=None,
                                  criteria_version=criteria.version, answers=answers,
                                  label=label, probabilities=probabilities,
                                  confidence=round(confidence, 4), latency_ms=0.0,
                                  cost_estimate_usd=0.0))
        return out
