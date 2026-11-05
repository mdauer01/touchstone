"""Criteria v4: the model records facts and cites owner rules; fixed code
decides the outcome and its treatment."""

from __future__ import annotations

import json

import pytest

from touchstone.assess.criteria import decide, finalize, load
from touchstone.assess.providers import (Assessment, ProviderError, parse_answers,
                                         render_prompt, response_format)
from touchstone.assess.runner import assess_records, build_record, items, load_dataset

V4 = load(4)
PRESERVED = {"identity": "governed", "calculation": "same", "eligibility": "kept",
             "source_rows": "same"}


def record(name="fare_revenue", approved=(), prohibited=(), evidence=("trips_fare",)):
    return {"record_id": "r1",
            "definition": {"name": name, "approved_variations": list(approved),
                           "prohibited_variations": list(prohibited)},
            "claimed_metric": {"identity_evidence": [{"source": "output", "value": v}
                                                     for v in evidence]}}


@pytest.mark.parametrize("facts, outcome", [
    ({"variation": "none"}, "likely_permitted"),
    ({"variation": "declared"}, "likely_permitted"),
    # An investigation on another attribute, with the core preserved.
    ({"variation": "other"}, "unreviewed_variation"),
    # Unspecified policy is not an escape hatch for an undetermined core.
    ({"variation": "other", "eligibility": "unknown"}, "insufficient_evidence"),
    ({"variation": "none", "source_rows": "unknown"}, "insufficient_evidence"),
    ({"variation": "unknown"}, "insufficient_evidence"),
    # A change outranks every unknown and every variation.
    ({"variation": "declared", "calculation": "changed", "eligibility": "unknown"},
     "suspected_definition_change"),
    ({"variation": "other", "eligibility": "changed"}, "suspected_definition_change"),
    ({"identity": "separate_measure", "calculation": "changed"}, "separate_measure"),
    ({"identity": "unknown", "variation": "none"}, "insufficient_evidence"),
])
def test_the_outcome_follows_from_the_facts(facts, outcome):
    assert decide({**PRESERVED, **facts}, V4, record())[0] == outcome
    assert V4.treatment_of(outcome) in ("alert", "informational", "unresolved", "none")


def test_owner_rules_count_only_when_cited_and_listed():
    rec = record(approved=["trip-length bands"], prohibited=["airport carve-outs"])
    assert decide({**PRESERVED, "variation": "approved", "approved_rules": ["A1"]}, V4, rec) \
        == ("likely_permitted", {"A1": "trip-length bands"})
    # An approval the record does not list is not an approval.
    assert decide({**PRESERVED, "variation": "approved", "approved_rules": ["A4"]}, V4, rec)[0] \
        == "unreviewed_variation"
    assert decide({**PRESERVED, "variation": "approved", "approved_rules": []}, V4, rec)[0] \
        == "unreviewed_variation"
    assert decide({**PRESERVED, "variation": "declared", "prohibited_rules": ["P1"]}, V4, rec) \
        == ("prohibited_variation", {"P1": "airport carve-outs"})
    assert decide({**PRESERVED, "variation": "declared", "prohibited_rules": ["P2"]}, V4, rec)[0] \
        == "likely_permitted"
    # A definition change is the alert, whatever rule is cited.
    assert decide({**PRESERVED, "calculation": "changed", "variation": "approved",
                   "approved_rules": ["A1"]}, V4, rec)[0] == "suspected_definition_change"


def test_a_result_named_as_the_metric_cannot_be_set_aside():
    answers = {**PRESERVED, "identity": "separate_measure", "variation": "none"}
    assert finalize("separate_measure", answers, record(evidence=("fare_revenue",)), V4) \
        == ("insufficient_evidence", {})
    assert finalize("separate_measure", answers, record(evidence=("trip_receipts",)), V4) \
        == ("separate_measure", {})
    not_a_use = {**answers, "identity": "not_a_use"}
    assert finalize("not_a_use", not_a_use, record(evidence=("fare_revenue",)), V4)[0] \
        == "insufficient_evidence"


def test_the_reply_cites_rules_by_listed_ids_only():
    schema = response_format(V4)["json_schema"]["schema"]
    assert {"approved_rules", "prohibited_rules"} <= set(schema["required"])
    good = {"answers": {**PRESERVED, "variation": "approved"}, "approved_rules": ["A2", "A1"],
            "prohibited_rules": [], "confidence": 0.8}
    answers, confidence = parse_answers(json.dumps(good), V4)
    assert answers["approved_rules"] == ["A1", "A2"] and confidence == 0.8
    for bad in ({**good, "approved_rules": ["P1"]}, {k: v for k, v in good.items()
                                                    if k != "prohibited_rules"}):
        with pytest.raises(ProviderError):
            parse_answers(json.dumps(bad), V4)
    # v3 replies are unchanged: no citations.
    assert "approved_rules" not in response_format(load(3))["json_schema"]["schema"]["properties"]


def test_the_v4_prompt_lists_owner_rules_by_id():
    ds = load_dataset()
    rec = build_record(ds, items(ds)[0][1])
    rec["definition"] = {**rec["definition"], "approved_variations": ["trip-length bands"],
                         "prohibited_variations": ["airport carve-outs"]}
    prompt = render_prompt(rec, V4)
    assert "A1 (approved): trip-length bands" in prompt
    assert "P1 (prohibited): airport carve-outs" in prompt
    assert "approved_rules" in prompt
    assert "P1 (prohibited)" not in render_prompt(rec, load(3))


class Scripted:
    name, model, measures_performance = "scripted", "m", True

    def __init__(self):
        self.calls = 0

    def assess(self, records, criteria):
        self.calls += len(records)
        return [Assessment(record_id=r["record_id"], provider="scripted", model="m",
                           criteria_version=criteria.version,
                           answers={**PRESERVED, "variation": "other", "approved_rules": [],
                                    "prohibited_rules": []},
                           label="unreviewed_variation", confidence=0.9) for r in records]


def test_a_later_dashboard_execution_raises_priority_without_another_call():
    ds = load_dataset()
    rec = build_record(ds, items(ds)[0][1])
    rec["occurrences"] = {"executions": 1, "contexts": [
        {"client": "Databricks SQL Editor", "assets": [], "executions": 1,
         "first_start_time": "2026-10-03T09:00:00Z", "last_start_time": "2026-10-03T09:00:00Z"}],
        "asset_kinds": {"unattributed": 1}}
    provider, cache = Scripted(), {}
    (first,), _ = assess_records(provider, [rec], V4, cache=cache)
    assert first["label"] == "unreviewed_variation" and first["treatment"] == "informational"
    later = json.loads(json.dumps(rec))
    later["occurrences"] = {"executions": 31, "contexts": [
        {"client": "Databricks SQL Dashboard", "assets": ["dashboard:01ef"], "executions": 30,
         "first_start_time": "2026-10-04T09:00:00Z", "last_start_time": "2026-10-30T09:00:00Z"},
        *rec["occurrences"]["contexts"]], "asset_kinds": {"dashboard": 30, "unattributed": 1}}
    (second,), stats = assess_records(provider, [later], V4, cache=cache)
    assert provider.calls == 1 and second["from_cache"] and stats["calls"] == 0
    assert second["priority"]["rank"] > first["priority"]["rank"]
    assert any("dashboard 01ef" in e for e in second["priority"]["evidence"])
    assert any("unattributed" in e for e in second["priority"]["evidence"])
