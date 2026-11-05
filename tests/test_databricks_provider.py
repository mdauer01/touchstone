"""The Databricks backend (ai_query on a SQL warehouse), against mocked API
responses — no network, no credentials. What the official docs fix (request
body, parameter shape, states, data_array encoding, the failOnError struct
fields) is asserted exactly; what they leave open is named in the module
docstring for the first live run."""

from __future__ import annotations

import json
import re
import socket

import pytest

from touchstone.assess.criteria import aggregate, load
from touchstone.assess.providers import ProviderConfig, get_provider, render_prompt
from touchstone.assess.providers.databricks import DatabricksProvider, response_format
from touchstone.assess.runner import build_record, items, load_dataset, run_benchmark

TOKEN = "dapi-test-token-0123456789"
ENV = {"DATABRICKS_TOKEN": TOKEN, "DATABRICKS_HOST": "https://dbc-test.cloud.databricks.com"}
MODEL = "system.ai.claude-sonnet-4-6"
WAREHOUSE = "abc123def456"
GOOD = {"answers": {"identity": "same_metric", "formula": "same", "population": "preserved",
                    "grain": "permitted", "source": "same"}, "confidence": 0.8}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("no network in provider tests")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(scope="module")
def dataset():
    return load_dataset()


class Fake:
    """A scripted Statement Execution API."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "body": json.loads(body) if body else None})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, payload, extra = (reply + ({},))[:3] if len(reply) == 2 else reply
        return status, extra, json.dumps(payload).encode() if payload is not None else b""


def succeeded(answer=GOOD, error=None):
    return (200, {"statement_id": "s1", "status": {"state": "SUCCEEDED"},
                  "manifest": {"schema": {"columns": [{"name": "response"},
                                                      {"name": "error_message"}]}},
                  "result": {"data_array": [[json.dumps(answer) if answer is not None
                                              else None, error]]}})


def provider(fake, **options):
    clock = iter(range(0, 10_000))
    return DatabricksProvider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE,
                                             options=options),
                              transport=fake, env=ENV, clock=lambda: next(clock),
                              sleep=lambda s: None)


def test_the_request_is_what_the_statement_execution_api_documents(dataset):
    fake = Fake([succeeded()])
    record = build_record(dataset, items(dataset)[0][1])
    (a,) = provider(fake).assess([record], load(1))
    (call,) = fake.calls
    assert call["method"] == "POST"
    assert call["url"] == "https://dbc-test.cloud.databricks.com/api/2.0/sql/statements"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    body = call["body"]
    assert body["warehouse_id"] == WAREHOUSE
    assert body["format"] == "JSON_ARRAY" and body["disposition"] == "INLINE"
    assert re.fullmatch(r"(0|[5-9]|[1-4]\d|50)s", body["wait_timeout"])
    # The prompt travels as a named STRING parameter, never in the SQL text.
    assert body["parameters"] == [{"name": "request", "value": render_prompt(record, load(1)),
                                   "type": "STRING"}]
    sql = body["statement"]
    assert f"ai_query('{MODEL}', :request," in sql
    assert "failOnError => false" in sql and "r.errorMessage" in sql and "r.result AS response" in sql
    # No temperature unless configured (reasoning endpoints reject it), and a
    # max_tokens that leaves room for reasoning tokens.
    assert "modelParameters => named_struct('max_tokens', 8192))" in sql
    assert "temperature" not in sql
    assert record["execution"]["statement"] not in sql
    assert a.label == aggregate(GOOD["answers"], load(1)) == "likely_permitted"
    assert a.confidence == 0.8 and a.error is None and a.model == MODEL
    assert a.cost_estimate_usd is None          # ai_query reports no usage


def test_the_response_format_is_valid_json_schema_with_every_criteria_answer():
    p = provider(Fake([]))
    sql = p.statement(load(1))
    literal = re.search(r"responseFormat => '((?:[^'\\]|\\.)*)'", sql).group(1)
    fmt = json.loads(literal.replace("\\'", "'").replace("\\\\", "\\"))
    assert fmt == response_format(load(1))
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    props = fmt["json_schema"]["schema"]["properties"]["answers"]["properties"]
    for name in load(1).components:
        assert props[name]["enum"] == load(1).answers(name)


def test_a_pending_statement_is_polled_to_its_result(dataset):
    fake = Fake([(200, {"statement_id": "s9", "status": {"state": "PENDING"}}),
                 (200, {"statement_id": "s9", "status": {"state": "RUNNING"}}),
                 succeeded()])
    (a,) = provider(fake).assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert a.error is None
    assert [c["method"] for c in fake.calls] == ["POST", "GET", "GET"]
    assert fake.calls[1]["url"].endswith("/api/2.0/sql/statements/s9")


def test_a_statement_past_its_deadline_is_canceled(dataset):
    fake = Fake([(200, {"statement_id": "s7", "status": {"state": "PENDING"}})]
                + [(200, {"statement_id": "s7", "status": {"state": "RUNNING"}})] * 50
                + [(200, {})])
    p = provider(fake, timeout_s=3)
    fake.replies.insert(3, (200, {}))
    (a,) = p.assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert a.error and "cancel requested" in a.error and a.label == "insufficient_evidence"
    assert any(c["url"].endswith("/s7/cancel") and c["method"] == "POST" for c in fake.calls)


@pytest.mark.parametrize("reply, expect", [
    ((200, {"statement_id": "s", "status": {"state": "FAILED", "error": {
        "error_code": "BAD_REQUEST", "message": "AI_FUNCTIONS_DISABLED"}}}), "FAILED"),
    (succeeded(answer=None, error="endpoint returned 429"), "model endpoint returned an error"),
    ((200, {"statement_id": "s", "status": {"state": "SUCCEEDED"},
            "result": {"data_array": [["not json", None]]}}), "not the requested JSON"),
    ((500, {"error_code": "INTERNAL_ERROR", "message": f"echo {TOKEN}"}), "HTTP 500"),
])
def test_failures_are_errors_on_the_assessment_never_a_label(dataset, reply, expect):
    (a,) = provider(Fake([reply])).assess([build_record(dataset, items(dataset)[0][1])],
                                          load(1))
    assert a.error and expect in a.error
    assert a.label == "insufficient_evidence"
    assert TOKEN not in json.dumps(a.as_dict())


def test_rate_limits_are_retried_with_retry_after(dataset):
    waits = []
    fake = Fake([(429, {"message": "slow down"}, {"Retry-After": "2"}), succeeded()])
    clock = iter(range(0, 10_000))
    p = DatabricksProvider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE),
                           transport=fake, env=ENV, clock=lambda: next(clock),
                           sleep=waits.append)
    (a,) = p.assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert a.error is None and waits and waits[0] >= 2


def test_an_answer_outside_the_criteria_is_unknown_never_a_match(dataset):
    odd = {"answers": {**GOOD["answers"], "formula": "looks fine"}, "confidence": 7}
    (a,) = provider(Fake([succeeded(odd)])).assess(
        [build_record(dataset, items(dataset)[0][1])], load(1))
    assert a.label == "insufficient_evidence" and a.confidence is None


@pytest.mark.parametrize("env, config, message", [
    ({**ENV, "DATABRICKS_TOKEN": ""}, {}, "DATABRICKS_TOKEN"),
    ({"DATABRICKS_TOKEN": TOKEN}, {}, "DATABRICKS_HOST"),
    ({**ENV, "DATABRICKS_HOST": "http://insecure"}, {}, "https://<workspace host>"),
    (ENV, {"model": "x'); DROP TABLE t; --"}, "serving endpoint"),
    (ENV, {"endpoint": "not a warehouse!"}, "warehouse id"),
])
def test_configuration_is_refused_without_ever_echoing_a_secret(env, config, message):
    cfg = ProviderConfig("databricks", model=config.get("model", MODEL),
                         endpoint=config.get("endpoint", WAREHOUSE))
    with pytest.raises(ValueError, match=message) as e:
        DatabricksProvider(cfg, transport=Fake([]), env=env)
    assert TOKEN not in str(e.value)


def test_the_token_never_appears_in_the_provider_or_its_output(dataset):
    p = provider(Fake([succeeded()]))
    assert TOKEN not in repr(p) and TOKEN not in json.dumps(
        {k: str(v) for k, v in vars(p).items() if not k.startswith("_WarehouseProvider")})
    (a,) = p.assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert TOKEN not in json.dumps(a.as_dict())


def test_get_provider_builds_it_from_the_environment(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    p = get_provider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE))
    assert isinstance(p, DatabricksProvider) and p.measures_performance


def test_a_benchmark_run_on_it_is_a_measurement_and_reports_cost_as_unreported(dataset):
    n = len(items(dataset, "dev"))
    fake = Fake([succeeded() for _ in range(n)])
    report = run_benchmark(provider(fake), dataset=dataset, split="dev")
    assert report["performance_measured"] is True and "note" not in report
    overall = report["arms"]["without_observations"]["overall"]
    assert overall["cost_usd"] is None and overall["cost_reported"] is False
    assert overall["permitted"]["confirmed"]["count"] == overall["permitted"]["of"]


def test_the_real_transport_never_follows_a_redirect():
    """urllib copies Authorization onto a redirected request; following a
    redirect to another host would hand it the token."""
    import io
    from urllib import request as urlrequest

    from touchstone.assess.providers.databricks import _NoRedirect

    handler = _NoRedirect()
    req = urlrequest.Request("https://dbc-test.cloud.databricks.com/api/2.0/sql/statements",
                             headers={"Authorization": f"Bearer {TOKEN}"})
    assert handler.redirect_request(req, io.BytesIO(b""), 302, "Found", {},
                                    "https://evil.example.com/collect") is None



# ------------------------------- found by the adapter review (round 18)

def _assess_one(dataset, replies, **options):
    fake = Fake(replies)
    (a,) = provider(fake, **options).assess([build_record(dataset, items(dataset)[0][1])],
                                            load(1))
    return a, fake


@pytest.mark.parametrize("state", ["CANCELED", "CLOSED", "FAILED"])
@pytest.mark.parametrize("where", ["post", "poll"])
def test_every_terminal_state_other_than_succeeded_is_an_error(dataset, state, where):
    from touchstone.assess.providers.databricks import TERMINAL

    assert TERMINAL == {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}
    final = (200, {"statement_id": "s", "status": {"state": state}})
    replies = [final] if where == "post" else [
        (200, {"statement_id": "s", "status": {"state": "PENDING"}}), final]
    a, _ = _assess_one(dataset, replies)
    assert a.error and f"statement {state}" in a.error and a.label == "insufficient_evidence"


@pytest.mark.parametrize("bad", [TOKEN[:6] + "\r\n" + TOKEN[6:], TOKEN + " x",
                                 TOKEN + "\u201d", TOKEN[:4] + "\t" + TOKEN[4:]])
def test_a_malformed_token_is_refused_without_echoing_it(bad):
    with pytest.raises(ValueError, match="re-copy the token") as e:
        DatabricksProvider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE),
                           transport=Fake([]), env={**ENV, "DATABRICKS_TOKEN": bad})
    assert TOKEN not in str(e.value) and TOKEN not in repr(e.value)


def test_surrounding_whitespace_is_stripped_from_the_token(dataset):
    fake = Fake([succeeded()])
    p = DatabricksProvider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE),
                           transport=fake, env={**ENV, "DATABRICKS_TOKEN": TOKEN + "\n"},
                           sleep=lambda s: None)
    p.assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert fake.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("host", ["https://adb-1.7.azuredatabricks.net/?o=123",
                                  "https://dbc-test.cloud.databricks.com#frag",
                                  "https://user@dbc-test.cloud.databricks.com",
                                  "https://dbc-test.cloud.databricks.com/some/path",
                                  "ftp://dbc-test.cloud.databricks.com"])
def test_a_host_with_anything_but_a_hostname_is_refused(host):
    with pytest.raises(ValueError, match="no path, query or user part"):
        DatabricksProvider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE),
                           transport=Fake([]), env={**ENV, "DATABRICKS_HOST": host})


def test_a_bare_hostname_and_a_port_are_kept():
    p = DatabricksProvider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE),
                           transport=Fake([]),
                           env={**ENV, "DATABRICKS_HOST": "dbc-test.cloud.databricks.com:8443/"})
    assert p.host == "https://dbc-test.cloud.databricks.com:8443"


def test_a_token_echoed_across_the_truncation_point_is_redacted(dataset):
    for offset in (260, 270, 280, 290, 299):
        message = "x" * offset + TOKEN + "y" * 100
        a, _ = _assess_one(dataset, [(500, {"error_code": "E", "message": message})])
        assert "dapi" not in a.error, offset


def test_a_refused_credential_stops_the_calls_and_never_reads_as_abstention(dataset):
    for reply in ((401, {"error_code": "UNAUTHENTICATED", "message": f"bad {TOKEN}"}),
                  (403, {"message": "no CAN USE on the warehouse"}),
                  (200, {"statement_id": "s", "status": {"state": "FAILED", "error": {
                      "error_code": "PERMISSION_DENIED", "message": "no EXECUTE"}}})):
        fake = Fake([reply, succeeded()])
        p = provider(fake)
        out = p.assess([build_record(dataset, i) for _, i in items(dataset)[:2]], load(1))
        assert len(fake.calls) == 1                       # nothing is called after it
        assert [a.error_kind for a in out] == ["credential refused", "not attempted"]
        assert out[1].error.startswith("not attempted: ") and out[1].latency_ms is None
        assert p.credential_refused and TOKEN not in p.credential_refused
        assert all(TOKEN not in a.error for a in out)


def test_a_credential_refused_mid_run_keeps_what_was_answered(dataset, tmp_path, monkeypatch,
                                                              capsys):
    """An OAuth token expiring an hour into a run discarded every completed
    assessment. Now the report keeps them, marks the rest failed (not
    attempted), is written, and the CLI exits 2."""
    from touchstone import cli
    from touchstone.assess.providers import databricks as D

    expired = (401, {"error_code": "UNAUTHENTICATED", "message": "Token expired"})
    replies = [succeeded()] * 3 + [expired]
    fake = Fake(replies)
    monkeypatch.setattr(D, "_urllib_transport", lambda timeout: fake)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    out = tmp_path / "r.json"
    code = cli.main(["assess", "benchmark", "--provider", "databricks", "--model", MODEL,
                     "--endpoint", WAREHOUSE, "--split", "dev", "--json", str(out)])
    assert code == 2
    report = json.loads(out.read_text())
    overall = report["arms"]["without_observations"]["overall"]
    assert overall["answered"] == 3 and overall["errors"]["count"] == overall["n"] - 3
    assert overall["errors"]["by_kind"] == {"credential refused": 1,
                                            "not attempted": overall["n"] - 4}
    assert overall["latency_ms"]["reported"] == 4          # never-sent items have none
    assert any("1 of" in n and "were not attempted" in n for n in report["notes"])
    assert "Token expired" in report["credential_refused"]
    assert "Token expired" in capsys.readouterr().err
    assert len(fake.calls) == 4


@pytest.mark.parametrize("escape", ["\\u0064api", "\\u0064\\u0061pi"])
def test_a_token_echoed_with_a_json_escape_is_redacted_everywhere(dataset, escape):
    escaped = TOKEN.replace("dapi", escape)
    raw = '{"error_code":"UNAUTHENTICATED","message":"Invalid access token: ' + escaped + '"}'

    def send(method, url, headers, body):
        return 401, {}, raw.encode()
    p = provider(send)
    (a,) = p.assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert TOKEN not in a.error and TOKEN not in p.credential_refused
    assert "[redacted]" in a.error

    def send500(method, url, headers, body):
        pad = "x" * 290
        return 500, {}, ('{"message":"' + pad + escaped + '"}').encode()
    (b,) = provider(send500).assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert "dapi" not in b.error and "test-token" not in b.error


def test_an_ipv6_host_keeps_its_brackets():
    from touchstone.assess.providers.databricks import _parse_host

    assert _parse_host("https://[::1]:8443") == "https://[::1]:8443"
    assert _parse_host("https://[2001:db8::1]") == "https://[2001:db8::1]"


@pytest.mark.parametrize("reason", [ConnectionRefusedError(61, "refused"),
                                    socket.gaierror(8, "nodename nor servname")])
def test_a_post_that_provably_never_left_is_retried(dataset, reason):
    from urllib.error import URLError

    a, fake = _assess_one(dataset, [URLError(reason), succeeded()])
    assert a.error is None and len(fake.calls) == 2


def test_a_post_that_may_have_left_is_not_retried(dataset):
    a, fake = _assess_one(dataset, [TimeoutError("read timed out"), succeeded()])
    assert a.error and a.error_kind == "transport" and len(fake.calls) == 1


def test_a_throttled_cancel_is_tried_once(dataset):
    calls = []

    def send(method, url, headers, body):
        calls.append((method, url))
        if method == "POST" and url.endswith("/statements"):
            return 200, {}, json.dumps({"statement_id": "s9",
                                        "status": {"state": "RUNNING"}}).encode()
        if url.endswith("/cancel"):
            return 429, {"Retry-After": "60"}, b'{"message": "slow down"}'
        return 500, {}, b'{"message": "poll broke"}'
    p = provider(send, max_attempts=4)
    (a,) = p.assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert sum(1 for m, u in calls if u.endswith("/cancel")) == 1
    assert "cancel request failed" in a.error and a.error_kind == "HTTP 500"


def test_an_interrupt_while_polling_still_cancels(dataset):
    calls = []

    def send(method, url, headers, body):
        calls.append((method, url))
        if method == "POST" and url.endswith("/statements"):
            return 200, {}, json.dumps({"statement_id": "s3",
                                        "status": {"state": "RUNNING"}}).encode()
        if url.endswith("/cancel"):
            return 200, {}, b"{}"
        if len([c for c in calls if c[0] == "GET"]) == 1:
            raise KeyboardInterrupt
        return 200, {}, json.dumps({"status": {"state": "CANCELED"}}).encode()
    p = provider(send)
    out = p.assess([build_record(dataset, i) for _, i in items(dataset)[:3]], load(1))
    assert ("POST", "https://dbc-test.cloud.databricks.com/api/2.0/sql/statements/s3/cancel") \
        in calls
    # The interrupt stops the run, keeps what was answered, and calls nothing more.
    assert [a.error_kind for a in out] == ["interrupted", "not attempted", "not attempted"]
    assert p.stopped and "interrupted" in p.stopped
    assert sum(1 for m, u in calls if m == "POST" and u.endswith("/statements")) == 1


def test_ctrl_c_mid_run_keeps_the_report_and_exits_130(dataset, tmp_path, monkeypatch, capsys):
    from touchstone import cli
    from touchstone.assess.providers import databricks as D

    sent = []

    def send(method, url, headers, body):
        sent.append(url)
        if len(sent) == 4:
            raise KeyboardInterrupt
        return succeeded()[0], {}, json.dumps(succeeded()[1]).encode()
    monkeypatch.setattr(D, "_urllib_transport", lambda timeout: send)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    out = tmp_path / "r.json"
    code = cli.main(["assess", "benchmark", "--provider", "databricks", "--model", MODEL,
                     "--endpoint", WAREHOUSE, "--split", "dev", "--json", str(out)])
    assert code == 130 and len(sent) == 4
    overall = json.loads(out.read_text())["arms"]["without_observations"]["overall"]
    assert overall["answered"] == 3
    assert "interrupted" in capsys.readouterr().err


@pytest.mark.parametrize("body", [
    '"Invalid access token: TOK"', '["bad", "TOK"]', '{"message": {"detail": "TOK"}}'])
@pytest.mark.parametrize("status", [401, 500])
def test_an_escaped_echo_in_any_json_body_is_redacted(dataset, body, status):
    escaped = "".join(f"\\u{ord(c):04x}" for c in TOKEN)          # every character escaped
    raw = body.replace("TOK", escaped)

    def send(method, url, headers, payload):
        return status, {}, raw.encode()
    p = provider(send)
    (a,) = p.assess([build_record(dataset, items(dataset)[0][1])], load(1))
    for text in (a.error, p.credential_refused or ""):
        assert "0123456789" not in text and "test-token" not in text, text


@pytest.mark.parametrize("fragment", [TOKEN[:12], TOKEN[-10:], TOKEN[5:20]])
def test_a_partial_echo_of_the_token_is_redacted(dataset, fragment):
    a, _ = _assess_one(dataset, [(500, {"message": f"token {fragment}... expired"})])
    assert fragment not in a.error and "[redacted]" in a.error


def test_an_unreachable_workspace_stops_the_run_after_one_record(dataset):
    from urllib.error import URLError

    calls = []

    def send(method, url, headers, body):
        calls.append(url)
        raise URLError(socket.gaierror(8, "nodename nor servname provided"))
    p = provider(send, max_attempts=3)
    out = p.assess([build_record(dataset, i) for _, i in items(dataset)[:4]], load(1))
    assert len(calls) == 3                                  # one record's retries, no more
    assert [a.error_kind for a in out] == ["unreachable"] + ["not attempted"] * 3
    assert "could not be reached" in p.stopped


@pytest.mark.parametrize("state", [["RUNNING"], {"s": 1}, 7])
def test_a_malformed_poll_state_still_cancels_at_the_deadline(dataset, state):
    calls = []

    def send(method, url, headers, body):
        calls.append((method, url))
        if method == "POST" and url.endswith("/statements"):
            return 200, {}, json.dumps({"statement_id": "s4",
                                        "status": {"state": "RUNNING"}}).encode()
        if url.endswith("/cancel"):
            return 200, {}, b"{}"
        return 200, {}, json.dumps({"status": {"state": state}}).encode()
    (a,) = provider(send, timeout_s=5).assess([build_record(dataset, items(dataset)[0][1])],
                                               load(1))
    assert any(u.endswith("/s4/cancel") for _, u in calls)
    assert a.error_kind == "timeout"


def test_an_unexpected_exception_while_polling_says_what_the_cancel_observed(dataset):
    polls = []

    def send(method, url, headers, body):
        if method == "POST" and url.endswith("/statements"):
            return 200, {}, json.dumps({"statement_id": "s6",
                                        "status": {"state": "RUNNING"}}).encode()
        if url.endswith("/cancel"):
            return 200, {}, b"{}"
        polls.append(url)
        if len(polls) == 1:
            raise RuntimeError("decoder blew up")
        return 200, {}, json.dumps({"status": {"state": "CANCELED"}}).encode()
    (a,) = provider(send).assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert "decoder blew up" in a.error and "cancel requested; the statement is CANCELED" \
        in a.error and a.error_kind == "RuntimeError"


def test_errors_are_reported_by_kind_not_by_request_path(dataset):
    n = 3
    replies = []
    for i in range(n):
        replies += [(200, {"statement_id": f"01ef-{i}", "status": {"state": "PENDING"}}),
                    (500, {"message": "poll broke"}), (200, {}),
                    (200, {"status": {"state": "CANCELED"}})]
    report = run_benchmark(provider(Fake(replies)), dataset={
        **dataset, "families": [{"family": "f", "split": "test",
                                 "items": [i for _, i in items(dataset)[:n]]}]}, split="test")
    assert report["arms"]["without_observations"]["overall"]["errors"]["by_kind"] == \
        {"HTTP 500": n}


@pytest.mark.parametrize("raw", [None, [], "text", {"status": "SUCCEEDED"},
                                 {"statement_id": "s", "status": {"state": "SUCCEEDED"},
                                  "result": {"data_array": "nope"}},
                                 {"statement_id": "s", "status": {"state": "SUCCEEDED"},
                                  "result": {"data_array": [[1, None]]}}])
def test_malformed_bodies_are_errors_not_crashes(dataset, raw):
    a, _ = _assess_one(dataset, [(200, raw)])
    assert a.error and a.label == "insufficient_evidence"


def test_any_transport_exception_is_an_error_on_that_record_only(dataset):
    import http.client

    records = [build_record(dataset, i) for _, i in items(dataset)[:3]]
    fake = Fake([http.client.IncompleteRead(b"x", 10), succeeded(), succeeded()])
    out = provider(fake).assess(records, load(1))
    assert out[0].error and "IncompleteRead" in out[0].error
    assert out[1].error is None and out[2].error is None


def test_the_statement_post_is_never_resubmitted_after_a_transport_failure(dataset):
    a, fake = _assess_one(dataset, [ConnectionResetError(), succeeded()])
    assert a.error and [c["method"] for c in fake.calls] == ["POST"]


def test_a_failed_poll_requests_a_cancel_and_says_what_was_observed(dataset):
    a, fake = _assess_one(dataset, [
        (200, {"statement_id": "s5", "status": {"state": "PENDING"}}),
        (500, {"error_code": "INTERNAL_ERROR", "message": "boom"}),   # the poll fails
        (200, {}),                                                    # cancel accepted
        (200, {"statement_id": "s5", "status": {"state": "CANCELED"}})])
    assert any(c["url"].endswith("/s5/cancel") for c in fake.calls)
    assert "cancel requested; the statement is CANCELED" in a.error


def test_a_failed_cancel_is_reported_not_claimed(dataset):
    def by_url(method, url, headers, body):
        if url.endswith("/cancel"):
            return 500, {}, json.dumps({"message": "cancel broke"}).encode()
        return 200, {}, json.dumps({"statement_id": "s7",
                                    "status": {"state": "RUNNING"}}).encode()
    p = DatabricksProvider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE,
                                          options={"timeout_s": 3}),
                           transport=by_url, env=ENV, clock=iter(range(10_000)).__next__,
                           sleep=lambda s: None)
    (a,) = p.assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert "cancel request failed" in a.error and "was canceled" not in a.error


@pytest.mark.parametrize("answer", [
    {"answers": {"formula": "changed"}, "confidence": 0.95},
    {"answers": {**GOOD["answers"], "grain": "seems ok"}, "confidence": 0.9},
    {"answers": {**GOOD["answers"], "extra": "x"}, "confidence": 0.9},
    {"answers": GOOD["answers"], "confidence": True},
    {"answers": GOOD["answers"], "confidence": float("nan")},
    {"answers": GOOD["answers"], "confidence": 1.5},
    {"answers": GOOD["answers"], "confidence": 0.5, "label": "likely_permitted"},
])
def test_an_answer_that_violates_the_requested_schema_is_an_error(dataset, answer):
    a, _ = _assess_one(dataset, [succeeded(answer)])
    assert a.error and "violates the requested schema" in a.error
    assert a.label == "insufficient_evidence" and a.confidence is None


@pytest.mark.parametrize("value", ["inf", "-5", "nan", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_an_odd_retry_after_never_stalls_or_crashes(dataset, value):
    waits = []
    fake = Fake([(429, {"message": "slow"}, {"retry-after": value}), succeeded()])
    p = DatabricksProvider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE),
                           transport=fake, env=ENV, clock=iter(range(10_000)).__next__,
                           sleep=waits.append)
    (a,) = p.assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert a.error is None and all(0 <= w <= 60 for w in waits)


# ------------------------------------------- round-18 verification, third pass

def test_redaction_is_linear_for_a_long_oauth_token(dataset):
    import time

    jwt = "eyJ" + "a1B2c3D4e5" * 230                       # ~2.3K characters, fake
    p = DatabricksProvider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE),
                           transport=Fake([]), env={**ENV, "DATABRICKS_TOKEN": jwt})
    body = "<html>" + "x" * 50_000 + jwt[100:140] + "</html>"
    start = time.perf_counter()
    cleaned = p._clean(body)
    assert time.perf_counter() - start < 1.0
    assert jwt[100:140] not in cleaned and "[redacted]" in cleaned


def test_an_escaped_echo_is_decoded_before_it_is_redacted(dataset):
    escaped = "".join(f"\\u{ord(c):04x}" if c == "t" else c for c in TOKEN)

    def send(method, url, headers, body):
        return 401, {}, ('{"error_code": "UNAUTHENTICATED", "message": "bad token '
                         + escaped + '"}').encode()
    (a,) = provider(send).assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert a.error.endswith("bad token [redacted]"), a.error


def test_the_exit_code_comes_from_the_stop_kind_not_the_servers_words(dataset, tmp_path,
                                                                       monkeypatch, capsys):
    from touchstone import cli
    from touchstone.assess.providers import databricks as D

    refusal = Fake([(401, {"error_code": "UNAUTHENTICATED",
                           "message": "Token refresh was interrupted; sign in again"})] * 2)
    monkeypatch.setattr(D, "_urllib_transport", lambda timeout: refusal)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    args = ["assess", "benchmark", "--provider", "databricks", "--model", MODEL,
            "--endpoint", WAREHOUSE, "--split", "dev"]
    assert cli.main(args) == 2
    capsys.readouterr()

    def interrupt(method, url, headers, body):
        raise KeyboardInterrupt
    monkeypatch.setattr(D, "_urllib_transport", lambda timeout: interrupt)
    assert cli.main([*args, "--json", str(tmp_path)]) == 130     # JSON unwritable too
    err = capsys.readouterr().err
    assert "could not write the report JSON" in err and "interrupted" in err


def test_an_interrupt_during_submission_says_the_statement_may_still_run(dataset):
    def send(method, url, headers, body):
        raise KeyboardInterrupt
    p = provider(send)
    (a, b) = p.assess([build_record(dataset, i) for _, i in items(dataset)[:2]], load(1))
    assert "may be running on the warehouse" in a.error
    assert b.error_kind == "not attempted" and p.stop_kind == "interrupted"


def test_a_connect_timeout_is_never_sent_and_stops_the_run(dataset):
    from urllib.error import URLError

    calls = []

    def send(method, url, headers, body):
        calls.append(url)
        raise URLError(TimeoutError("timed out"))
    p = provider(send, max_attempts=2)
    out = p.assess([build_record(dataset, i) for _, i in items(dataset)[:3]], load(1))
    assert len(calls) == 2 and p.stop_kind == "unreachable"
    assert [a.error_kind for a in out] == ["unreachable", "not attempted", "not attempted"]


# ------------------------------------------------- login profile, model parameters, batches

PROFILE_TOKEN = "eyJprofile-oauth-token-abcdefghijklmnop"


def profile_provider(fake, tmp_path, source, *, wall=lambda: 1_000.0, **options):
    cfg = tmp_path / "databrickscfg"
    cfg.write_text("[touchstone]\nhost = https://dbc-profile.cloud.databricks.com\n"
                   "auth_type = databricks-cli\n")
    clock = iter(range(0, 10_000))
    return DatabricksProvider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE,
                                             options={"profile": "touchstone", **options}),
                              transport=fake, env={}, clock=lambda: next(clock),
                              sleep=lambda s: None, token_source=source, wall=wall,
                              config_file=cfg)


def test_temperature_is_sent_only_when_configured_and_bounded():
    assert "named_struct('temperature', 0.0, 'max_tokens', 4096)" in provider(
        Fake([]), temperature=0, max_tokens=4096).statement(load(1))
    with pytest.raises(ValueError, match="temperature"):
        provider(Fake([]), temperature=3)


def test_a_login_profile_supplies_the_host_and_a_token_that_never_surfaces(dataset, tmp_path):
    fake = Fake([succeeded()])
    p = profile_provider(fake, tmp_path, lambda: (PROFILE_TOKEN, 1_000.0 + 3600))
    (a,) = p.assess([build_record(dataset, items(dataset)[0][1])], load(1))
    assert p.host == "https://dbc-profile.cloud.databricks.com"
    assert fake.calls[0]["url"].startswith(p.host)
    assert fake.calls[0]["headers"]["Authorization"] == f"Bearer {PROFILE_TOKEN}"
    assert PROFILE_TOKEN not in repr(p) and PROFILE_TOKEN not in json.dumps(a.as_dict())


def test_an_explicit_token_wins_over_a_profile(dataset):
    p = DatabricksProvider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE),
                           transport=Fake([]), env={**ENV, "DATABRICKS_CONFIG_PROFILE": "x"},
                           token_source=lambda: (_ for _ in ()).throw(AssertionError("used")))
    assert p.profile is None


def test_a_profile_token_is_refreshed_before_it_expires_and_both_are_redacted(dataset,
                                                                             tmp_path):
    now = [1_000.0]
    tokens = iter([(PROFILE_TOKEN, 1_000.0 + 400), (PROFILE_TOKEN + "-second", 9_999_999.0)])
    fake = Fake([succeeded(), (500, {"message": f"echo {PROFILE_TOKEN}"})])
    p = profile_provider(fake, tmp_path, lambda: next(tokens), wall=lambda: now[0])
    record = build_record(dataset, items(dataset)[0][1])
    p.assess([record], load(1))
    now[0] = 1_000.0 + 200            # inside the refresh margin of the first token
    (a,) = p.assess([record], load(1))
    assert fake.calls[1]["headers"]["Authorization"] == f"Bearer {PROFILE_TOKEN}-second"
    assert PROFILE_TOKEN[:12] not in a.error           # the replaced token is still a secret


def test_a_profile_that_cannot_produce_a_token_is_refused_at_construction(tmp_path):
    from touchstone.assess.providers.databricks import DatabricksAuthError

    def fail():
        raise DatabricksAuthError("`databricks auth token --profile touchstone` failed",
                                  "credential unavailable")
    with pytest.raises(ValueError, match="databricks auth token"):
        profile_provider(Fake([]), tmp_path, fail)


def test_no_token_and_no_profile_names_both_ways_to_log_in():
    with pytest.raises(ValueError, match="DATABRICKS_TOKEN.*databricks auth login"):
        DatabricksProvider(ProviderConfig("databricks", model=MODEL, endpoint=WAREHOUSE),
                           transport=Fake([]), env={"DATABRICKS_HOST": ENV["DATABRICKS_HOST"]})


def batch_reply(rows):
    return (200, {"statement_id": "b1", "status": {"state": "SUCCEEDED"},
                  "result": {"data_array": rows}})


def test_a_batch_is_one_statement_with_one_parameter_and_rows_matched_by_id(dataset):
    records = [build_record(dataset, it) for _, it in items(dataset)[:3]]
    bad = {**GOOD, "answers": {**GOOD["answers"], "formula": "changed"}}
    # Rows come back out of order; row 1 failed at the endpoint.
    fake = Fake([batch_reply([["2", json.dumps(bad), None], ["1", None, "rate limited"],
                              ["0", json.dumps(GOOD), None]])])
    out = provider(fake, batch_size=10).assess(records, load(1))
    (call,) = fake.calls
    sql = call["body"]["statement"]
    assert "explode(from_json(:batch," in sql and "ai_query('system.ai.claude-sonnet-4-6', x.request," in sql
    (param,) = call["body"]["parameters"]
    assert param["name"] == "batch" and param["type"] == "STRING"
    assert json.loads(param["value"]) == [{"id": str(i), "request": render_prompt(r, load(1))}
                                          for i, r in enumerate(records)]
    assert all(r["execution"]["statement"] not in sql for r in records)
    assert [a.record_id for a in out] == [r["record_id"] for r in records]
    assert out[0].label == "likely_permitted" and out[0].error is None
    assert out[1].error_kind == "model error" and out[1].label == "insufficient_evidence"
    assert out[2].label == "suspected_drift"


def test_a_missing_or_duplicated_row_fails_that_record_only(dataset):
    records = [build_record(dataset, it) for _, it in items(dataset)[:3]]
    fake = Fake([batch_reply([["0", json.dumps(GOOD), None], ["2", json.dumps(GOOD), None],
                              ["2", json.dumps(GOOD), None]])])
    out = provider(fake, batch_size=3).assess(records, load(1))
    assert out[0].error is None
    assert out[1].error_kind == out[2].error_kind == "invalid response"


def test_a_refused_credential_fails_its_batch_and_stops_the_rest(dataset):
    records = [build_record(dataset, it) for _, it in items(dataset)[:4]]
    fake = Fake([batch_reply([["0", json.dumps(GOOD), None], ["1", json.dumps(GOOD), None]]),
                 (403, {"message": "no"})])
    p = provider(fake, batch_size=2)
    out = p.assess(records + [build_record(dataset, items(dataset)[4][1])], load(1))
    assert [a.error is None for a in out[:2]] == [True, True]
    assert out[2].error_kind == out[3].error_kind == "credential refused"
    assert out[4].error_kind == "not attempted" and p.stop_kind == "credential refused"
    assert len(fake.calls) == 2


def test_result_chunks_are_followed_only_under_the_statements_api(dataset):
    records = [build_record(dataset, it) for _, it in items(dataset)[:2]]
    first = (200, {"statement_id": "b1", "status": {"state": "SUCCEEDED"},
                   "result": {"data_array": [["0", json.dumps(GOOD), None]],
                              "next_chunk_internal_link":
                                  "/api/2.0/sql/statements/b1/result/chunks/1"}})
    fake = Fake([first, (200, {"data_array": [["1", json.dumps(GOOD), None]]})])
    out = provider(fake, batch_size=2).assess(records, load(1))
    assert [a.error for a in out] == [None, None]
    assert fake.calls[1]["url"].endswith("/api/2.0/sql/statements/b1/result/chunks/1")
    evil = (200, {"statement_id": "b1", "status": {"state": "SUCCEEDED"},
                  "result": {"data_array": [], "next_chunk_internal_link":
                             "//evil.example/api/2.0/sql/statements/x"}})
    fake = Fake([evil])
    out = provider(fake, batch_size=2).assess(records, load(1))
    assert all(a.error_kind == "invalid response" for a in out) and len(fake.calls) == 1
