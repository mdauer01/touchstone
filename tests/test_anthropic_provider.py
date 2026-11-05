"""The direct API backend against a scripted client — no network, no credentials."""

from __future__ import annotations

import json
import socket
from types import SimpleNamespace

import pytest

from touchstone.assess.criteria import load
from touchstone.assess.providers import ProviderConfig, render_prompt
from touchstone.assess.providers.anthropic_api import AnthropicProvider
from touchstone.assess.providers.databricks import response_format
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


def reply(payload=GOOD, stop="end_turn", usage=(1000, 500), details=None):
    return SimpleNamespace(
        stop_reason=stop, stop_details=details,
        content=[SimpleNamespace(type="thinking", thinking=""),
                 SimpleNamespace(type="text", text=json.dumps(payload))],
        usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]))


class Client:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = SimpleNamespace(create=self._create(False))
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create(True)))

    def _create(self, beta):
        def create(**kwargs):
            self.calls.append({"beta": beta, **kwargs})
            r = self.replies.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return create


def provider(client, model="claude-haiku-5-5", **options):
    return AnthropicProvider(ProviderConfig("anthropic", model=model, options=options),
                             client=client)


def test_the_model_is_required_and_api_still_names_this_backend():
    with pytest.raises(ValueError, match="--model is required"):
        provider(Client([]), model=None)
    p = AnthropicProvider(ProviderConfig("api", model="claude-haiku-5-5"), client=Client([]))
    assert p.name == "anthropic" and p.model == "claude-haiku-5-5"


def test_the_request_carries_the_shared_prompt_and_schema_and_a_fallback(records):
    client = Client([reply()])
    (a,) = provider(client, model="claude-opus-5-5", concurrency=1).assess(records[:1], load())
    (call,) = client.calls
    assert call["model"] == "claude-opus-5-5" and call["beta"] is True
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["messages"] == [{"role": "user", "content": render_prompt(records[0], load())}]
    assert call["output_config"]["format"] == {
        "type": "json_schema", "schema": response_format(load())["json_schema"]["schema"]}
    assert "temperature" not in call and "thinking" not in call and "effort" not in \
        call["output_config"]
    assert a.label == "suspected_drift" and a.confidence == 0.9 and a.error is None
    assert a.cost_estimate_usd == round((1000 * 4 + 500 * 20) / 1e6, 6)


def test_haiku_takes_no_server_fallback_and_effort_is_sent_when_set(records):
    client = Client([reply()])
    (a,) = provider(client, effort="low").assess(records[:1], load())
    (call,) = client.calls
    assert call["model"] == "claude-haiku-5-5" and a.model == "claude-haiku-5-5"
    assert call["beta"] is False and "fallbacks" not in call
    assert a.cost_estimate_usd == round((1000 * 0.10 + 500 * 0.50) / 1e6, 6)
    assert call["output_config"]["effort"] == "low"
    with pytest.raises(ValueError, match="effort"):
        provider(Client([]), effort="extreme")
    with pytest.raises(ValueError, match="temperature"):
        provider(Client([]), temperature=0)


def test_a_refusal_a_truncation_or_a_bad_answer_is_an_error_never_a_label(records):
    bad = {**GOOD, "answers": {**GOOD["answers"], "formula": "probably fine"}}
    client = Client([reply(stop="refusal", details=SimpleNamespace(category="cyber")),
                     reply(stop="max_tokens"), reply(bad)])
    out = provider(client, concurrency=1).assess(records, load())
    assert [a.error_kind for a in out] == ["refused", "truncated", "invalid response"]
    assert all(a.label == "insufficient_evidence" and a.answers == {} for a in out)
    assert "cyber" in out[0].error


def test_a_refused_credential_stops_the_calls_and_keeps_what_was_answered(records):
    class AuthenticationError(Exception):
        status_code = 401

    client = Client([reply(), AuthenticationError("invalid x-api-key")])
    p = provider(client, concurrency=1)
    out = p.assess(records, load())
    assert out[0].error is None
    assert out[1].error_kind == "credential refused" and p.stop_kind == "credential refused"
    assert out[2].error_kind == "not attempted" and len(client.calls) == 2


def test_an_empty_credit_balance_stops_the_calls(records):
    class BadRequestError(Exception):
        status_code = 400

    client = Client([BadRequestError("Your credit balance is too low to access the "
                                     "Anthropic API.")])
    p = provider(client, concurrency=1)
    out = p.assess(records, load())
    assert out[0].error_kind == "no credit" and p.stop_kind == "no credit"
    assert [a.error_kind for a in out[1:]] == ["not attempted"] * 2 and len(client.calls) == 1
