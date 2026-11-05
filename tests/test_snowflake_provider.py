"""The Snowflake backend against a scripted SQL API — no network, no account."""

from __future__ import annotations

import gzip
import json
import socket

import pytest

from touchstone.assess.criteria import aggregate, load
from touchstone.assess.providers import ProviderConfig, render_prompt
from touchstone.assess.providers.snowflake import SnowflakeProvider
from touchstone.assess.runner import build_record, items, load_dataset

TOKEN = "pat-test-token-0123456789abcdef"
ENV = {"SNOWFLAKE_TOKEN": TOKEN, "SNOWFLAKE_ACCOUNT": "acme_org-prod"}
MODEL = "claude-sonnet-4-5"
GOOD = {"answers": {"identity": "same_metric", "formula": "same", "population": "preserved",
                    "grain": "permitted", "source": "same"}, "confidence": 0.8}
HANDLE = "01b2c3d4-0000-1111-2222-333344445555"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("no network in provider tests")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(scope="module")
def records():
    ds = load_dataset()
    return [build_record(ds, it) for _, it in items(ds)[:3]]


class Fake:
    """A scripted SQL API: each reply is (status, body[, headers, raw bytes])."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": json.loads(body) if body else None})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, payload, *rest = reply
        headers = rest[0] if rest else {}
        raw = rest[1] if len(rest) > 1 else json.dumps(payload).encode()
        return status, headers, raw


def done(rows, partitions=1):
    return (200, {"code": "090001", "statementHandle": HANDLE, "data": rows,
                  "resultSetMetaData": {"numRows": len(rows),
                                        "partitionInfo": [{"rowCount": 1}] * partitions}})


def answer(payload=GOOD):
    return json.dumps(payload, indent=2)          # an OBJECT arrives as its JSON text


def provider(fake, env=ENV, endpoint="ANALYTICS_WH", **options):
    clock = iter(range(0, 10_000))
    return SnowflakeProvider(ProviderConfig("snowflake", model=MODEL, endpoint=endpoint,
                                            options=options),
                             transport=fake, env=env, clock=lambda: next(clock),
                             sleep=lambda s: None, request_id=lambda: "req-1")


def test_the_request_is_what_the_sql_api_documents(records):
    fake = Fake([done([[answer(), None]])])
    (a,) = provider(fake).assess(records[:1], load())
    (call,) = fake.calls
    assert call["method"] == "POST"
    assert call["url"] == "https://acme-org-prod.snowflakecomputing.com/api/v2/statements" \
                          "?requestId=req-1"
    headers = call["headers"]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-Snowflake-Authorization-Token-Type"] == "PROGRAMMATIC_ACCESS_TOKEN"
    assert headers["User-Agent"].startswith("touchstone/")
    body = call["body"]
    assert body["warehouse"] == "ANALYTICS_WH" and "role" not in body
    # The prompt is bind variable 1, never spliced into the SQL.
    assert body["bindings"] == {"1": {"type": "TEXT",
                                      "value": render_prompt(records[0], load())}}
    sql = body["statement"]
    assert "prompt => ?" in sql and "AI_COMPLETE(model => 'claude-sonnet-4-5'" in sql
    assert "response_format => {'type': 'json', 'schema': {'type': 'object'" in sql
    assert "'additionalProperties': FALSE" in sql and "model_parameters => " \
        "{'max_tokens': 8192}" in sql
    assert render_prompt(records[0], load())[:40] not in sql
    assert a.label == aggregate(GOOD["answers"], load()) and a.confidence == 0.8
    assert a.error is None
    assert a.provider == "snowflake" and a.model == MODEL


def test_a_running_statement_is_polled_and_a_json_string_answer_is_unwrapped(records):
    fake = Fake([(202, {"code": "333334", "statementHandle": HANDLE}),
                 (202, {"code": "333334", "statementHandle": HANDLE}),
                 done([[json.dumps(json.dumps(GOOD)), None]])])
    (a,) = provider(fake).assess(records[:1], load())
    assert [c["method"] for c in fake.calls] == ["POST", "GET", "GET"]
    assert fake.calls[1]["url"].endswith(f"/api/v2/statements/{HANDLE}")
    assert a.error is None and a.answers == GOOD["answers"]


def test_a_failed_statement_is_an_error_never_a_label(records):
    fake = Fake([(422, {"code": "000904", "sqlState": "42000", "statementHandle": HANDLE,
                        "message": "json mode output validation error"})])
    (a,) = provider(fake).assess(records[:1], load())
    assert a.error_kind == "statement FAILED" and "000904" in a.error
    assert a.label == "insufficient_evidence" and a.answers == {}


def test_a_refused_token_stops_the_calls_and_never_appears(records):
    fake = Fake([done([[answer(), None]]),
                 (401, {"code": "390303", "message": f"PAT_INVALID {TOKEN[:20]}"})])
    p = provider(fake)
    out = p.assess(records, load())
    assert out[0].error is None
    assert out[1].error_kind == "credential refused" and p.stop_kind == "credential refused"
    assert out[2].error_kind == "not attempted" and len(fake.calls) == 2
    dumped = json.dumps([a.as_dict() for a in out]) + repr(p)
    assert TOKEN[:12] not in dumped and "[redacted]" in out[1].error


def test_a_batch_travels_as_one_binding_and_follows_gzip_partitions(records):
    rows = [[str(i), answer(), None] for i in range(3)]
    second = json.dumps({"data": rows[2:]}).encode()
    fake = Fake([done(rows[:2], partitions=2),
                 (200, None, {"Content-Encoding": "gzip"}, gzip.compress(second))])
    out = provider(fake, batch_size=5).assess(records, load())
    assert [a.error for a in out] == [None, None, None]
    post, page = fake.calls
    assert "TABLE(FLATTEN(input => PARSE_JSON(?)))" in post["body"]["statement"]
    batch = json.loads(post["body"]["bindings"]["1"]["value"])
    assert [b["id"] for b in batch] == ["0", "1", "2"]
    assert page["url"].endswith(f"/api/v2/statements/{HANDLE}?partition=1")


def test_a_statement_past_its_deadline_is_canceled(records):
    fake = Fake([(202, {"statementHandle": HANDLE}), (202, {"statementHandle": HANDLE}),
                 (200, {"code": "000000"})])
    (a,) = provider(fake, timeout_s=2).assess(records[:1], load())
    assert a.error_kind == "timeout" and "cancel requested" in a.error
    assert fake.calls[-1]["url"].endswith(f"/api/v2/statements/{HANDLE}/cancel")


@pytest.mark.parametrize("env, options, message", [
    ({"SNOWFLAKE_ACCOUNT": "acme"}, {}, "SNOWFLAKE_TOKEN"),
    ({"SNOWFLAKE_TOKEN": TOKEN}, {}, "account URL"),
    ({**ENV, "SNOWFLAKE_TOKEN_TYPE": "PASSWORD"}, {}, "SNOWFLAKE_TOKEN_TYPE"),
    ({**ENV}, {"host": "http://acme.snowflakecomputing.com"}, "account URL"),
    ({**ENV}, {"batch_size": 0}, "batch_size"),
])
def test_bad_settings_are_refused_without_echoing_the_token(env, options, message):
    with pytest.raises(ValueError, match=message) as e:
        provider(Fake([]), env=env, **options)
    assert TOKEN not in str(e.value)


def test_the_role_and_an_oauth_token_type_are_sent_when_set(records):
    fake = Fake([done([[answer(), None]])])
    env = {**ENV, "SNOWFLAKE_ROLE": "CORTEX_ANALYST", "SNOWFLAKE_TOKEN_TYPE": "oauth"}
    provider(fake, env=env, endpoint=None).assess(records[:1], load())
    (call,) = fake.calls
    assert call["body"]["role"] == "CORTEX_ANALYST" and "warehouse" not in call["body"]
    assert call["headers"]["X-Snowflake-Authorization-Token-Type"] == "OAUTH"
