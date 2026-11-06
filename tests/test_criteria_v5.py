"""Criteria v5: v4's facts with the metric view's join semantics stated; a
prohibited variation is decided before the core facts."""

from __future__ import annotations

from touchstone.assess.criteria import decide, load
from touchstone.assess.providers import render_prompt
from touchstone.assess.runner import build_record, items, load_dataset

V4, V5 = load(4), load(5)
PRESERVED = {"identity": "governed", "calculation": "same", "eligibility": "kept",
             "source_rows": "same"}


def record():
    return {"record_id": "r1",
            "definition": {"name": "fare_revenue",
                           "approved_variations": ["inner joins along the declared keys"],
                           "prohibited_variations": ["airport carve-outs"]},
            "claimed_metric": {"identity_evidence": [{"source": "output",
                                                      "value": "fare_revenue"}]}}


def test_a_prohibited_variation_outranks_the_core_from_v5_only():
    facts = {**PRESERVED, "source_rows": "changed", "variation": "other",
             "prohibited_rules": ["P1"]}
    assert decide(facts, V5, record()) == ("prohibited_variation", {"P1": "airport carve-outs"})
    assert decide(facts, V4, record())[0] == "suspected_definition_change"
    unknown = {**facts, "source_rows": "unknown"}
    assert decide(unknown, V5, record())[0] == "prohibited_variation"
    # The identity guards still come first.
    assert decide({**facts, "identity": "separate_measure"}, V5, record())[0] \
        == "separate_measure"


def test_an_approval_that_settles_a_core_fact_is_carried():
    facts = {**PRESERVED, "variation": "declared", "approved_rules": ["A1"]}
    assert decide(facts, V5, record()) == ("likely_permitted",
                                           {"A1": "inner joins along the declared keys"})
    assert decide(facts, V4, record()) == ("likely_permitted", {})
    # A cited rule the record does not list still counts for nothing.
    assert decide({**facts, "approved_rules": ["A9"]}, V5, record()) == ("likely_permitted", {})


def test_the_v5_prompt_states_the_join_semantics():
    ds = load_dataset()
    rec = build_record(ds, items(ds)[0][1])
    prompt = render_prompt(rec, V5)
    assert "a left outer join" in prompt and "criteria v5" in prompt
    assert "a left outer join" not in render_prompt(rec, V4)
