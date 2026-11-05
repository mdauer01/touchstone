"""The OpenAI-compatible backend against a scripted client — no network, no key."""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest

from touchstone.assess.criteria import load
from touchstone.assess.providers import ProviderConfig, render_prompt, response_format
from touchstone.assess.providers.openai_api import OpenAIProvider
from touchstone.assess.runner import build_record, items, load_dataset

GOOD = {"answers": {"identity": "same_metric", "formula": "changed", "population": "preserved",
                    "grain": "permitted", "source": "same"}, "confidence": 0.9}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("no network in provider tests")
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(scope="module")
def records():
    ds = load_dataset()
    return [build_record(ds, it) for _, it in items(ds)[:3]]


def reply(payload=GOOD, finish="stop", refusal=None, content=None):
    text = json.dumps(payload) if content is None else content
    message = SimpleNamespace(content=None if refusal else text, refusal=refusal)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)])


class Client:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def provider(client, model="gpt-5.4-mini", **options):
    return OpenAIProvider(ProviderConfig("openai", model=model, options=options),
                          client=client)


def test_the_request_carries_the_shared_prompt_and_strict_schema_and_nothing_unasked(records):
    client = Client([reply()])
    (a,) = provider(client, concurrency=1).assess(records[:1], load())
    (call,) = client.calls
    assert call == {"model": "gpt-5.4-mini",
                    "messages": [{"role": "user", "content": render_prompt(records[0], load())}],
                    "response_format": response_format(load())}
    assert call["response_format"]["json_schema"]["strict"] is True
    assert a.label == "suspected_drift" and a.confidence == 0.9 and a.error is None
    assert a.provider == "openai" and a.cost_estimate_usd is None


def test_optional_parameters_are_sent_only_when_set(records):
    client = Client([reply()])
    provider(client, max_tokens=4000, temperature=0.2, effort="low").assess(records[:1], load())
    (call,) = client.calls
    assert call["max_completion_tokens"] == 4000 and call["temperature"] == 0.2
    assert call["reasoning_effort"] == "low"


def test_a_refusal_a_filter_a_truncation_or_a_bad_answer_is_an_error_never_a_label(records):
    bad = {**GOOD, "answers": {**GOOD["answers"], "formula": "probably fine"}}
    client = Client([reply(refusal="I can't help with that."), reply(finish="content_filter"),
                     reply(finish="length"), reply(bad), reply(content="")])
    out = provider(client, concurrency=1).assess(records + records[:2], load())
    assert [a.error_kind for a in out] == ["refused", "refused", "truncated",
                                           "invalid response", "invalid response"]
    assert all(a.label == "insufficient_evidence" and a.answers == {} for a in out)


def test_a_refused_key_stops_the_calls_and_keeps_what_was_answered(records):
    class AuthenticationError(Exception):
        status_code = 401

    client = Client([reply(), AuthenticationError("Incorrect API key provided")])
    p = provider(client, concurrency=1)
    out = p.assess(records, load())
    assert out[0].error is None
    assert out[1].error_kind == "credential refused" and p.stop_kind == "credential refused"
    assert out[2].error_kind == "not attempted" and len(client.calls) == 2


@pytest.mark.parametrize("url, ok", [
    ("https://litellm.example.com/v1", True),
    ("http://localhost:4000/v1", True),
    ("http://127.0.0.1:11434/v1/", True),
    ("http://litellm.example.com/v1", False),
    ("https://user:pw@litellm.example.com/v1", False),
    ("https://litellm.example.com/v1?key=x", False),
    ("ftp://litellm.example.com", False),
])
def test_the_base_url_must_be_https_or_this_machine(url, ok):
    if ok:
        assert provider(Client([]), base_url=url).base_url == url.rstrip("/")
    else:
        with pytest.raises(ValueError, match="base-url"):
            provider(Client([]), base_url=url)


def test_the_model_is_required():
    with pytest.raises(ValueError, match="--model is required"):
        provider(Client([]), model=None)


class DecisionsClient:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.decisions = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def decision(choices, refuse=None, tokens=1800):
    def answer(name, value, p):
        if name == refuse:
            return SimpleNamespace(type="refusal", name=name)
        return SimpleNamespace(type="choice", name=name, choice=value, confidence=p,
                               probabilities=[SimpleNamespace(value=value, probability=p),
                                              SimpleNamespace(value="unknown",
                                                              probability=1 - p)])
    return SimpleNamespace(answers=[answer(n, v, p) for n, (v, p) in choices.items()],
                           usage=SimpleNamespace(input_tokens=tokens, output_tokens=0))


SAME = {"identity": ("same_metric", 1.0), "formula": ("same", 0.9),
        "population": ("preserved", 0.8), "grain": ("permitted", 1.0), "source": ("same", 1.0)}


def test_decisions_sends_typed_choice_questions_and_keeps_each_probability(records):
    client = DecisionsClient([decision(SAME)])
    p = OpenAIProvider(ProviderConfig("openai", model="gpt-6-luna",
                                      options={"decisions": True}), client=client)
    (a,) = p.assess(records[:1], load())
    (call,) = client.calls
    assert set(call) == {"model", "input", "questions"} and call["model"] == "gpt-6-luna"
    developer, user = call["input"]
    assert developer["role"] == "developer" and "## Policy (criteria v3)" in \
        developer["content"][0]["text"]
    assert user["role"] == "user" and "<<<DATA " in user["content"][0]["text"]
    assert [q["name"] for q in call["questions"]] == list(load().components)
    assert {c["value"] for c in call["questions"][0]["choices"]} == set(load().answers("identity"))
    assert a.provider == "openai-decisions" and a.label == "likely_permitted"
    assert a.probabilities == {"identity": 1.0, "formula": 0.9, "population": 0.8,
                               "grain": 1.0, "source": 1.0}
    assert a.confidence == pytest.approx(0.72) and a.cost_estimate_usd == 0.00018


def test_a_decision_refusal_or_a_missing_answer_is_an_error_never_a_label(records):
    missing = {k: v for k, v in SAME.items() if k != "grain"}
    client = DecisionsClient([decision(SAME, refuse="formula"), decision(missing)])
    p = OpenAIProvider(ProviderConfig("openai", model="gpt-6-luna",
                                      options={"decisions": True, "concurrency": 1}),
                       client=client)
    out = p.assess(records[:2], load())
    assert [a.error_kind for a in out] == ["refused", "invalid response"]
    assert all(a.label == "insufficient_evidence" for a in out)


def test_decisions_takes_no_sampling_options_and_no_other_server():
    for options in ({"temperature": 0}, {"effort": "low"}, {"max_tokens": 10},
                    {"base_url": "https://litellm.example.com/v1"}):
        with pytest.raises(ValueError, match="Decisions API"):
            provider(Client([]), decisions=True, **options)
