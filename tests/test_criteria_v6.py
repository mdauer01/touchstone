"""Criteria v6: one assessment with evidence, three review states; the
approvals and prohibitions of v4/v5 are not routed."""

from __future__ import annotations

import json

import pytest

from touchstone.assess.criteria import decide_single, evidence, finalize, load
from touchstone.assess.providers import ProviderError, parse_answers, render_prompt, response_format
from touchstone.assess.runner import build_record, items, load_dataset

V6 = load(6)
STATEMENT = ("SELECT SUM(`l_extendedprice` * (1 - `l_discount`)) AS revenue\n"
             "FROM `samples`.`tpch`.`lineitem`\nWHERE `l_shipdate` >= DATE('1995-01-01')")
SNAPSHOT = ("source: samples.tpch.lineitem\nfilter: l_returnflag <> 'R'\n"
            "measures:\n  - name: revenue\n    expr: SUM(l_extendedprice * (1 - l_discount))\n")


def record(name="revenue", output="revenue"):
    return {"record_id": "r1", "execution": {"statement": STATEMENT},
            "definition": {"name": name, "snapshot_text": SNAPSHOT,
                           "approved_variations": ["anything"],
                           "prohibited_variations": ["something"]},
            "claimed_metric": {"identity_evidence": [{"source": "alias", "value": output}]}}


def reply(comparison, presented_as="revenue", statement_clause="", definition_clause="",
          explanation=""):
    return {"comparison": comparison, "presented_as": presented_as,
            "statement_clause": statement_clause, "definition_clause": definition_clause,
            "explanation": explanation}


def test_a_missing_required_filter_is_evidence_from_the_definition_alone():
    answers = reply("departs", definition_clause="l_returnflag <> 'R'",
                    explanation="Returned line items are counted.")
    assert finalize("review_needed", answers, record(), V6) == ("review_needed", {})
    found = evidence(answers, record())["excerpts_found"]
    assert found == {"statement_clause": None, "definition_clause": True}


def test_excerpts_must_occur_in_the_text_they_quote():
    # Backticks, whitespace and case do not matter; an ellipsis splits parts.
    quoted = reply("departs", statement_clause="sum(l_extendedprice*(1-l_discount)) ... DATE('1995-01-01')",
                   explanation="x")
    assert finalize("review_needed", quoted, record(), V6)[0] == "review_needed"
    invented = reply("departs", statement_clause="WHERE l_returnflag = 'R'",
                     definition_clause="filter: l_tax > 0", explanation="x")
    assert finalize("review_needed", invented, record(), V6)[0] == "could_not_assess"
    # A departure needs an explanation.
    assert finalize("review_needed", reply("departs", definition_clause="l_returnflag <> 'R'"),
                    record(), V6)[0] == "could_not_assess"


@pytest.mark.parametrize("answers, state", [
    (reply("preserved"), "no_issue_flagged"),
    (reply("undetermined", explanation="no fact says each part key is unique"),
     "could_not_assess"),
    # A separately named quantity: nothing presents the metric.
    (reply("preserved", presented_as=""), "no_issue_flagged"),
    (reply("departs", presented_as="", definition_clause="l_returnflag <> 'R'",
           explanation="x"), "no_issue_flagged"),
])
def test_the_state_follows_from_the_answer(answers, state):
    assert finalize("x", answers, record(output="avg_order_revenue"), V6)[0] == state


def test_a_result_under_the_metrics_exact_name_cannot_be_set_aside():
    assert finalize("no_issue_flagged", reply("preserved", presented_as=""), record(), V6) \
        == ("could_not_assess", {})


def test_without_the_record_excerpts_are_taken_at_their_word():
    assert decide_single(reply("departs", statement_clause="anything", explanation="x"), V6) \
        == "review_needed"


def test_the_reply_carries_evidence_text():
    schema = response_format(V6)["json_schema"]["schema"]
    assert set(schema["properties"]["answers"]["required"]) == {
        "comparison", "presented_as", "statement_clause", "definition_clause", "explanation"}
    good = {"answers": reply("departs", definition_clause="l_returnflag <> 'R'",
                             explanation="returns counted"), "confidence": 0.9}
    answers, confidence = parse_answers(json.dumps(good), V6)
    assert answers["definition_clause"] == "l_returnflag <> 'R'" and confidence == 0.9
    for bad in ({**good, "answers": {**good["answers"], "explanation": None}},
                {**good, "answers": {k: v for k, v in good["answers"].items()
                                     if k != "statement_clause"}},
                {**good, "approved_rules": []}):
        with pytest.raises(ProviderError):
            parse_answers(json.dumps(bad), V6)


def test_the_v6_prompt_routes_no_owner_rules():
    ds = load_dataset()
    rec = build_record(ds, items(ds)[0][1])
    rec["definition"] = {**rec["definition"], "approved_variations": ["trip-length bands"],
                         "prohibited_variations": ["airport carve-outs"]}
    prompt = render_prompt(rec, V6)
    assert "criteria v6" in prompt and "statement_clause (text)" in prompt
    assert "trip-length bands" not in prompt and "airport carve-outs" not in prompt
    assert "trip-length bands" in render_prompt(rec, load(4))


def test_quotes_may_carry_the_prompts_json_escapes_and_join_parts():
    # The statement is shown JSON-encoded: a model may quote "\n" literally,
    # and join two definition clauses with "; ".
    answers = reply("departs", statement_clause="SELECT SUM(`l_extendedprice` * (1 - `l_discount`))"
                    " AS revenue\\nFROM `samples`.`tpch`.`lineitem`",
                    definition_clause="filter: l_returnflag <> 'R'; expr: SUM(l_extendedprice * "
                                      "(1 - l_discount))", explanation="x")
    assert evidence(answers, record())["excerpts_found"] == {"statement_clause": True,
                                                            "definition_clause": True}


def test_an_output_read_as_the_metric_must_be_named_as_it():
    unnamed = {**record(), "claimed_metric": {"identity_evidence": [{"source": "none",
                                                                     "value": None}]}}
    departs = reply("departs", presented_as="sum_disc_price",
                    definition_clause="l_returnflag <> 'R'", explanation="returns counted")
    assert finalize("review_needed", departs, unnamed, V6)[0] == "could_not_assess"
    assert evidence(departs, unnamed)["presented_by_name"] is False
    # A qualified name carries it; so does a tag or asset naming the metric.
    assert finalize("x", {**departs, "presented_as": "eu_revenue"}, unnamed, V6)[0] \
        == "review_needed"
    tagged = {**unnamed, "claimed_metric": {"identity_evidence": [{"source": "tag",
                                                                   "value": "revenue"}]}}
    assert finalize("x", departs, tagged, V6)[0] == "review_needed"
