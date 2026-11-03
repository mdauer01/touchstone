"""Databricks backend: `ai_query` with a JSON-schema response format, run on a
SQL warehouse through the SQL Statement Execution API.

What it sends, per record:

    SELECT r.response, r.errorMessage FROM (
      SELECT ai_query('<serving endpoint>', :request,
                      responseFormat => '<json schema>',
                      failOnError => false,
                      modelParameters => named_struct('temperature', 0.0, 'max_tokens', N)) AS r)

* the prompt is `render_prompt` (the statement as it ran, its execution and
  session, the definition with its validity window, the governing variation
  policy, schemas; semantix's observations last) and travels as the named
  parameter `:request` — never spliced into SQL text;
* the endpoint, response format and model parameters are validated literals:
  Databricks documents the endpoint and failOnError as literals and
  modelParameters as constants, and parameter markers in literal positions
  are documented for Databricks Runtime 18.0+ only;
* `failOnError => false` returns STRUCT<response, errorMessage>; the two fields
  are selected as scalar columns, so no STRUCT has to be serialized by the API;
* a json_schema response format returns a JSON STRING, parsed here; the model
  answers the criteria's questions, and `criteria.aggregate` — not the model —
  computes the label.

Verified against the official docs (docs.databricks.com: ai_query, Statement
Execution API) on 2026-10-26. INFERRED, to confirm on the first live run:
that a parameter marker is accepted as ai_query's request argument on a SQL
warehouse, and that the chosen chat model accepts this response format.

Credentials: the access token is read from DATABRICKS_TOKEN (a personal
access token or an OAuth access token the operator obtained). It is sent only
in the Authorization header, and never stored on the provider's public
attributes, logged, printed, or included in an error or an assessment.
`ai_query` reports no token usage, so cost is reported as not reported —
system.ai_gateway.usage holds it for account and metastore admins.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Callable
from urllib import error as urlerror
from urllib import request as urlrequest

from semantix.assess.criteria import Criteria, aggregate
from semantix.assess.providers import Assessment, ProviderConfig, render_prompt

TOKEN_ENV = "DATABRICKS_TOKEN"
HOST_ENV = "DATABRICKS_HOST"
WAREHOUSE_ENV = "DATABRICKS_WAREHOUSE_ID"
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"})
_ENDPOINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,254}")
_WAREHOUSE = re.compile(r"[A-Za-z0-9]{1,64}")

# transport(method, url, headers, body) -> (status, headers, body)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict, bytes]]


class DatabricksError(Exception):
    """A request or statement failure, worded without any credential, with
    its KIND (`HTTP 503`, `timeout`, `transport`, `invalid response`, …) for
    the report's errors-by-kind."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


class _NoRedirect(urlrequest.HTTPRedirectHandler):
    """Never follow a redirect: urllib copies the Authorization header onto
    the redirected request, so following one to another host would hand it
    the token. A redirect is returned as the non-200 response it is."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _urllib_transport(timeout: float) -> Transport:
    opener = urlrequest.build_opener(_NoRedirect)

    def send(method, url, headers, body):
        req = urlrequest.Request(url, data=body, method=method, headers=headers)
        try:
            with opener.open(req, timeout=timeout) as resp:   # noqa: S310 — https only
                return resp.status, dict(resp.headers), resp.read()
        except urlerror.HTTPError as e:
            return e.code, dict(e.headers or {}), e.read() or b""
    return send


def response_format(criteria: Criteria) -> dict:
    """The JSON schema the model must answer in: one enum per component, plus a
    self-reported confidence. The label is not in it — fixed code computes it."""
    components = {name: {"type": "string", "enum": criteria.answers(name)}
                  for name in criteria.components}
    return {"type": "json_schema", "json_schema": {
        "name": "semantix_assessment",
        "schema": {"type": "object",
                   "properties": {
                       "answers": {"type": "object", "properties": components,
                                   "required": list(components),
                                   "additionalProperties": False},
                       "confidence": {"type": "number"}},
                   "required": ["answers", "confidence"],
                   "additionalProperties": False},
        "strict": True}}


def _sql_literal(text: str) -> str:
    """A Databricks SQL string literal for text this module generated (no
    user data). Databricks SQL escapes with a backslash (`'Spark\\'s'`); a doubled
    quote is two adjacent literals, which concatenate and LOSE the quote."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


class _SubmitInterrupted(KeyboardInterrupt):
    """Ctrl-C while the statement POST was in flight: the statement may run,
    and no id is known to cancel it."""


class DatabricksAuthError(DatabricksError):
    """The workspace refused the credential or the permission. Every later
    call would fail the same way, so the provider stops CALLING — the items
    already answered are kept, the rest are failed (not attempted), and the
    run is never reported as a model that abstained on everything."""

    def __init__(self, message: str, kind: str = "credential refused"):
        super().__init__(message, kind)


_AUTH_CODES = frozenset({"UNAUTHENTICATED", "PERMISSION_DENIED"})
_TOKEN = re.compile(r"[\x21-\x7e]+")      # printable ASCII, no whitespace


def _parse_host(raw: str) -> str:
    """https://<hostname>[:port] — no userinfo, path, query or fragment."""
    from urllib.parse import urlsplit

    raw = raw.strip()
    if raw and "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        parts, port = None, None
    if parts is None or parts.scheme != "https" or not parts.hostname \
            or parts.username or parts.password or parts.query or parts.fragment \
            or parts.path not in ("", "/"):
        raise ValueError(f"databricks provider: set {HOST_ENV} (or --host) to the workspace "
                         f"URL — https://<workspace host>, with no path, query or user part")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname   # IPv6
    return f"https://{host}" + (f":{port}" if port else "")


class DatabricksProvider:
    name = "databricks"
    measures_performance = True
    confidence_source = "self-reported by the model"

    def __init__(self, config: ProviderConfig, *, transport: Transport | None = None,
                 env: dict | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        env = os.environ if env is None else env
        options = dict(config.options or {})
        self.model = config.model
        if not self.model or not _ENDPOINT.fullmatch(self.model):
            raise ValueError("databricks provider: --model must name a serving endpoint "
                             "(for example system.ai.<model>) — letters, digits, '.', '_' "
                             "and '-' only")
        self.warehouse_id = config.endpoint or env.get(WAREHOUSE_ENV)
        if not self.warehouse_id or not _WAREHOUSE.fullmatch(self.warehouse_id):
            raise ValueError(f"databricks provider: pass the SQL warehouse id as --endpoint "
                             f"(or set {WAREHOUSE_ENV}) — the id after /sql/1.0/warehouses/")
        self.host = _parse_host(str(options.get("host") or env.get(HOST_ENV) or ""))
        token = (env.get(TOKEN_ENV) or "").strip()
        if not token:
            raise ValueError(f"databricks provider: set {TOKEN_ENV} in the environment — "
                             f"semantix never takes a credential as an argument")
        if not _TOKEN.fullmatch(token):
            # Never echoed: a token with a stray newline or a pasted curly quote
            # would otherwise surface verbatim in an "invalid header" error.
            raise ValueError(f"databricks provider: {TOKEN_ENV} contains whitespace or "
                             f"non-ASCII characters — re-copy the token")
        self.__token = token          # name-mangled; never part of repr or output
        # Every 8-character window of the token, for the partial-echo scan.
        self.__grams = frozenset(token[i:i + 8] for i in range(len(token) - 7))
        self.timeout_s = float(options.get("timeout_s", 300))
        self.max_tokens = int(options.get("max_tokens", 1024))
        if not 1 <= self.max_tokens <= 100_000:
            raise ValueError("databricks provider: max_tokens must be between 1 and 100000")
        self.max_attempts = max(1, int(options.get("max_attempts", 4)))
        self._send = transport or _urllib_transport(timeout=60)
        self._clock = clock
        self._sleep = sleep
        # Set when the run must stop calling — the workspace refused the
        # credential, cannot be reached, or the operator interrupted — and
        # why; no call is made after it.
        self.credential_refused: str | None = None
        self.stopped: str | None = None
        self.stop_kind: str | None = None     # credential refused | unreachable | interrupted

    def __repr__(self) -> str:
        return (f"DatabricksProvider(model={self.model!r}, host={self.host!r}, "
                f"warehouse_id={self.warehouse_id!r})")

    # ------------------------------------------------------------- protocol

    def statement(self, criteria: Criteria) -> str:
        fmt = json.dumps(response_format(criteria), separators=(",", ":"))
        return ("SELECT r.response AS response, r.errorMessage AS error_message FROM ("
                f"SELECT ai_query({_sql_literal(self.model)}, :request, "
                f"responseFormat => {_sql_literal(fmt)}, failOnError => false, "
                f"modelParameters => named_struct('temperature', 0.0, 'max_tokens', "
                f"{self.max_tokens})) AS r)")

    def assess(self, records: list[dict], criteria: Criteria) -> list[Assessment]:
        """One assessment per record. A failure on one record is an error on
        THAT assessment (never a label, never the end of the run). A refused
        credential stops the CALLS — every later one would fail alike — and
        the records not attempted are failed, so what was answered survives
        (an OAuth token expiring mid-run discarded the whole report)."""
        sql = self.statement(criteria)
        out = []
        for record in records:
            started = self._clock()
            try:
                answers, confidence, error, kind, latency = self._assess_one(
                    sql, record, criteria, started)
            except KeyboardInterrupt as e:
                # The operator stopped the run — wherever it was: keep what
                # was answered, call nothing more.
                answers, confidence, latency = {}, None, None
                kind = "interrupted"
                error = ("interrupted while the statement was being submitted — it may be "
                         "running on the warehouse, and its id is not known"
                         if isinstance(e, _SubmitInterrupted) else "interrupted")
                self._stop("the run was interrupted by the operator", "interrupted")
            out.append(Assessment(
                record_id=record["record_id"], provider=self.name, model=self.model,
                criteria_version=criteria.version, answers=answers,
                label=aggregate(answers, criteria) if error is None else
                "insufficient_evidence",
                probabilities=None, confidence=confidence, latency_ms=latency,
                cost_estimate_usd=None, error=error, error_kind=kind))
        return out

    def _assess_one(self, sql: str, record: dict, criteria: Criteria, started: float):
        """(answers, confidence, error, error kind, latency ms) for one record."""
        if self.stopped:
            # Never sent: no latency, and not a failed CALL.
            return {}, None, f"not attempted: {self.stopped}", "not attempted", None
        try:
            response = self._run(sql, render_prompt(record, criteria))
            answers, confidence = self._parse(response, criteria)
            error = kind = None
        except DatabricksError as e:
            answers, confidence, error, kind = {}, None, self._clean(str(e)), e.kind
            if isinstance(e, DatabricksAuthError):
                self.credential_refused = error
                self._stop(error, "credential refused")
            elif e.kind == "unreachable":
                self._stop(error, "unreachable")
        except Exception as e:  # noqa: BLE001 — one record never ends the run
            answers, confidence = {}, None
            error, kind = self._clean(f"{type(e).__name__}: {e}")[:300], type(e).__name__
        return answers, confidence, error, kind, round((self._clock() - started) * 1000, 1)

    def _stop(self, why: str, kind: str) -> None:
        self.stopped, self.stop_kind = why, kind

    # ------------------------------------------------------------ internals

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.__token}",
                "Content-Type": "application/json", "Accept": "application/json"}

    def _clean(self, text: str) -> str:
        """Redact the token — always BEFORE any truncation — and any run of
        eight or more of its characters: a server that cuts its echo of the
        Authorization value leaves a fragment the whole-token match misses.
        Linear in the text: each position is looked up once among the
        token's 8-character windows (an OAuth JWT is thousands long)."""
        text = str(text).replace(self.__token, "[redacted]")
        grams = self.__grams
        if not grams:
            return text
        marked = [False] * len(text)
        for i in range(len(text) - 7):
            if text[i:i + 8] in grams:
                marked[i:i + 8] = [True] * 8
        if not any(marked):
            return text
        out, i = [], 0
        while i < len(text):
            if marked[i]:
                while i < len(text) and marked[i]:
                    i += 1
                out.append("[redacted]")
            else:
                out.append(text[i])
                i += 1
        return "".join(out)

    def _call(self, method: str, path: str, body: dict | None = None, *,
              resubmit: bool = True, attempts: int | None = None) -> dict:
        """One API call. `resubmit=False` (the statement POST) never re-sends
        after a transport failure that may have reached the server — that
        could run ai_query twice — but does after one that provably did not
        (connection refused, the name did not resolve). 429/503 are explicit
        refusals and retried, within `attempts` (default max_attempts)."""
        import http.client

        url = f"{self.host}{path}"
        payload = json.dumps(body).encode() if body is not None else None
        delay = 1.0
        attempts = attempts or self.max_attempts
        for attempt in range(1, attempts + 1):
            try:
                status, headers, raw = self._send(method, url, self._headers(), payload)
            except (OSError, http.client.HTTPException, ValueError) as e:
                if _never_sent(e) and attempt == attempts:
                    # The workspace cannot be reached at all: every later call
                    # would wait out the same retries, so the run stops.
                    raise DatabricksError(f"{method} {path}: {type(e).__name__} — the workspace "
                                          f"could not be reached", "unreachable") from None
                if (not resubmit and not _never_sent(e)) or attempt == attempts:
                    raise DatabricksError(f"{method} {path}: {type(e).__name__}",
                                          "transport") from None
                self._sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            if status in (429, 503) and attempt < attempts:
                self._sleep(_retry_after(headers, delay))
                delay = min(delay * 2, 30.0)
                continue
            text = raw.decode("utf-8", "replace") if raw else ""
            if status != 200:
                # Decoded FIRST — a JSON escape can spell the token, and a
                # redaction before decoding split it — then redacted, then
                # cut. Only an error's text is ever shown.
                message = self._clean(_error_text(text)[:4096])[:300]
                if status in (401, 403):
                    raise DatabricksAuthError(f"{method} {path}: HTTP {status}: the workspace "
                                              f"refused the credential or permission — "
                                              f"{message}")
                raise DatabricksError(f"{method} {path}: HTTP {status}: {message}",
                                      f"HTTP {status}")
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                raise DatabricksError(f"{method} {path}: the response is not JSON",
                                      "invalid response") from None
            if not isinstance(data, dict):
                raise DatabricksError(f"{method} {path}: the response is not a JSON object",
                                      "invalid response")
            return data
        raise DatabricksError(f"{method} {path}: retries exhausted", "retries exhausted")

    def _run(self, sql: str, prompt: str) -> str:
        deadline = self._clock() + self.timeout_s
        body = {"warehouse_id": self.warehouse_id, "statement": sql,
                "parameters": [{"name": "request", "value": prompt, "type": "STRING"}],
                "wait_timeout": "30s", "on_wait_timeout": "CONTINUE",
                "disposition": "INLINE", "format": "JSON_ARRAY"}
        try:
            result = self._call("POST", "/api/2.0/sql/statements", body, resubmit=False)
        except KeyboardInterrupt:
            raise _SubmitInterrupted() from None
        statement_id = result.get("statement_id")
        state = _state(result)
        wait = 1.0
        try:
            while state not in TERMINAL:
                if not statement_id:
                    raise DatabricksError("the statement was accepted without an id",
                                          "invalid response")
                if self._clock() >= deadline:
                    raise DatabricksError(f"no result within {self.timeout_s:.0f}s", "timeout")
                self._sleep(wait)
                wait = min(wait * 2, 5.0)
                result = self._call("GET", f"/api/2.0/sql/statements/{statement_id}")
                state = _state(result)
        except BaseException as e:
            # Never abandon a statement still running — whatever ends the
            # wait, an interrupt included: request a cancel and say what was
            # OBSERVED (a cancel call only confirms receipt).
            if statement_id and state not in TERMINAL:
                note = self._cancel(statement_id)
                if isinstance(e, DatabricksError):
                    raise type(e)(f"{e} — {note}", e.kind) from None
                if isinstance(e, Exception):
                    raise DatabricksError(self._clean(f"{type(e).__name__}: {e} — {note}"),
                                          type(e).__name__) from None
            raise
        if state != "SUCCEEDED":
            err = (result.get("status") or {}).get("error") or {}
            err = err if isinstance(err, dict) else {}
            code = str(err.get("error_code") or "")
            message = f"statement {state}: {code} {err.get('message') or ''}".strip()
            if code in _AUTH_CODES:
                raise DatabricksAuthError(self._clean(message))
            raise DatabricksError(self._clean(message), f"statement {state}")
        rows = (result.get("result") or {}).get("data_array") \
            if isinstance(result.get("result"), dict) else None
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], list) \
                or len(rows[0]) != 2:
            raise DatabricksError("expected exactly one row of (response, error_message)",
                                  "invalid response")
        response, model_error = rows[0]
        if model_error is not None:
            raise DatabricksError(f"the model endpoint returned an error: "
                                  f"{self._clean(str(model_error))[:300]}", "model error")
        if not isinstance(response, str):
            raise DatabricksError("the model returned no response", "invalid response")
        return response

    def _cancel(self, statement_id: str) -> str:
        # One attempt each: a throttled cancel must not hold the run for
        # several Retry-After waits past its deadline.
        try:
            self._call("POST", f"/api/2.0/sql/statements/{statement_id}/cancel", {},
                       attempts=1)
        except DatabricksAuthError:
            return "cancel request refused"
        except DatabricksError as e:
            return f"cancel request failed ({self._clean(str(e))[:120]})"
        try:
            state = _state(self._call("GET", f"/api/2.0/sql/statements/{statement_id}",
                                      attempts=1))
        except DatabricksError:
            state = None
        if state in TERMINAL:
            return f"cancel requested; the statement is {state}"
        return "cancel requested; its terminal state was not confirmed"

    @staticmethod
    def _parse(response: str, criteria: Criteria) -> tuple[dict, float]:
        """The answers, checked against the response format that was requested:
        every component present with one of its answers, a confidence in [0, 1],
        nothing else. A violation is an error, never a partial label."""
        import math

        try:
            data = json.loads(response)
        except ValueError:
            raise DatabricksError("the model's response is not the requested JSON",
                                  "invalid response") from None
        if not isinstance(data, dict) or set(data) != {"answers", "confidence"} \
                or not isinstance(data["answers"], dict):
            raise DatabricksError("the response violates the requested schema: expected "
                                  "exactly `answers` and `confidence`", "invalid response")
        answers = data["answers"]
        problems = sorted(set(answers) - set(criteria.components))
        for name in criteria.components:
            if answers.get(name) not in criteria.answers(name):
                problems.append(name)
        if problems:
            raise DatabricksError(f"the response violates the requested schema: "
                                  f"{', '.join(problems)}", "invalid response")
        confidence = data["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
                or not math.isfinite(float(confidence)) or not 0.0 <= confidence <= 1.0:
            raise DatabricksError("the response violates the requested schema: confidence "
                                  "must be a number in [0, 1]", "invalid response")
        return {k: answers[k] for k in criteria.components}, float(confidence)


def _state(result: dict) -> str | None:
    status = result.get("status")
    state = status.get("state") if isinstance(status, dict) else None
    return state if isinstance(state, str) else None


def _retry_after(headers, default: float) -> float:
    import math

    value = None
    for key, v in (headers or {}).items():
        if str(key).lower() == "retry-after":
            value = v
    try:
        seconds = float(value) if value is not None else default
    except ValueError:
        seconds = default
    if not math.isfinite(seconds) or seconds < 0:
        seconds = default
    return min(max(seconds, default), 60.0)


def _error_text(text: str) -> str:
    """The message of an error body, DECODED (whatever JSON it is — an escape
    can spell the token) and UNCUT: the caller redacts it before cutting."""
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, dict):
        return " ".join(str(data.get(k)) for k in ("error_code", "message") if data.get(k))
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False)


def _never_sent(e: Exception) -> bool:
    """A transport failure that provably happened before any request left:
    urllib wraps what fails while CONNECTING or sending in URLError — the
    connection refused, the host name not resolved, no route, a connect that
    timed out (a blackholed private-link workspace). A read that times out
    after sending is not wrapped, and may have run the statement."""
    import errno
    import socket

    reason = getattr(e, "reason", None) if isinstance(e, urlerror.URLError) else e
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return True
    if isinstance(e, urlerror.URLError) and isinstance(reason, TimeoutError):
        return True
    return isinstance(reason, OSError) and reason.errno in (errno.ENETUNREACH,
                                                            errno.EHOSTUNREACH)
