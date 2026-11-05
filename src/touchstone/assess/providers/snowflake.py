"""Snowflake backend: Cortex `AI_COMPLETE` with a JSON-schema response format,
run on a warehouse through the Snowflake SQL API — the model runs inside the
account, so the statements and definitions never leave it.

What it sends, per record:

    SELECT AI_COMPLETE(model => '<model>', prompt => ?,
                       model_parameters => {'max_tokens': N},
                       response_format => {'type': 'json', 'schema': {...}}) AS response,
           NULL AS error_message

* the prompt is `render_prompt` — the same as every backend — and travels as
  bind variable 1 (`?`), never spliced into SQL text;
* with `batch_size` > 1, one statement assesses a batch: the prompts travel
  as ONE bind variable (a JSON array of {id, request}), flattened into rows;
* the model, model parameters and response format are literals this module
  generates: `response_format` is a SQL OBJECT constant, not a string, as the
  docs require;
* AI_COMPLETE has no per-row error mode (the TRY_ variant is the legacy
  TRY_COMPLETE, deprecated at the end of 2026), so ONE failing row fails its
  whole statement: keep batches small;
* the answer is parsed against the criteria's schema here; the model answers
  the questions, and `criteria.aggregate` — not the model — computes the label.

Verified against the official docs (docs.snowflake.com: AI_COMPLETE single
string, AI_COMPLETE structured outputs, SQL API reference, submitting
requests, authenticating, programmatic access tokens) on 2026-11-04.
INFERRED, to confirm on the first live run: that a bind variable is accepted
as AI_COMPLETE's prompt; that the OBJECT result comes back as its JSON text
(a JSON string holding it is unwrapped too); and that a result partition past
the first is `{"data": [...]}`.

Credentials: SNOWFLAKE_TOKEN — a programmatic access token (the default), an
OAuth access token, or a key-pair JWT the operator generated, named by
SNOWFLAKE_TOKEN_TYPE. It is sent only in the Authorization header, and never
stored on the provider's public attributes, logged, printed, or included in an
error or an assessment. The account comes from --host / SNOWFLAKE_HOST (the
account URL) or SNOWFLAKE_ACCOUNT; the warehouse from --endpoint /
SNOWFLAKE_WAREHOUSE (else the user's default); the role from SNOWFLAKE_ROLE
(else the user's default). Cortex bills tokens in credits, not reported here.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Callable

from touchstone.assess.criteria import Criteria
from touchstone.assess.providers import (ProviderConfig, ProviderError, parse_answers,
                                         response_format)
from touchstone.assess.providers.warehouse import (Transport, WarehouseAuthError,
                                                   WarehouseProvider, _SubmitInterrupted,
                                                   _urllib_transport, https_host)

TOKEN_ENV = "SNOWFLAKE_TOKEN"
TOKEN_TYPE_ENV = "SNOWFLAKE_TOKEN_TYPE"
HOST_ENV = "SNOWFLAKE_HOST"
ACCOUNT_ENV = "SNOWFLAKE_ACCOUNT"
WAREHOUSE_ENV = "SNOWFLAKE_WAREHOUSE"
ROLE_ENV = "SNOWFLAKE_ROLE"
TOKEN_TYPES = ("PROGRAMMATIC_ACCESS_TOKEN", "OAUTH", "KEYPAIR_JWT")
_STATEMENTS = "/api/v2/statements"
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}")
_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,254}")
_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,255}")       # sent as a JSON field, not SQL
_HANDLE = re.compile(r"[A-Za-z0-9\-]{1,128}")
_OK = (200, 202, 422)


def _sql_literal(text: str) -> str:
    """A Snowflake string constant for text this module generated (no user
    data): backslash doubled, quote doubled."""
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def _constant(value) -> str:
    """A Snowflake OBJECT / ARRAY constant for a JSON value this module
    generated — the response format and model parameters."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_sql_literal(str(k))}: {_constant(v)}"
                               for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_constant(v) for v in value) + "]"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _sql_literal(value)
    raise TypeError(f"no Snowflake constant for {type(value).__name__}")


class SnowflakeProvider(WarehouseProvider):
    name = "snowflake"
    place = "account"
    token_env = TOKEN_ENV

    def __init__(self, config: ProviderConfig, *, transport: Transport | None = None,
                 env: dict | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 request_id: Callable[[], str] = lambda: str(uuid.uuid4())):
        env = os.environ if env is None else env
        options = dict(config.options or {})
        self.model = config.model
        if not self.model or not _MODEL.fullmatch(self.model):
            raise ValueError("snowflake provider: --model must name a Cortex model (for "
                             "example claude-sonnet-4-5) — letters, digits, '.', '_' and "
                             "'-' only")
        host = str(options.get("host") or env.get(HOST_ENV) or "").strip()
        account = str(env.get(ACCOUNT_ENV) or "").strip()
        if not host and account:
            if not _ACCOUNT.fullmatch(account):
                raise ValueError(f"snowflake provider: {ACCOUNT_ENV} must be an account "
                                 f"identifier, such as myorg-myaccount")
            # Underscores are not valid in a host name; Snowflake's account
            # URLs use hyphens in their place.
            host = f"{account.replace('_', '-').lower()}.snowflakecomputing.com"
        self.host = https_host(host, f"snowflake provider: set {HOST_ENV} (or --host) to the "
                                     f"account URL — https://<account>.snowflakecomputing.com "
                                     f"— or {ACCOUNT_ENV} to the account identifier")
        self.warehouse = (config.endpoint or env.get(WAREHOUSE_ENV) or "").strip() or None
        self.role = (env.get(ROLE_ENV) or "").strip() or None
        for what, value in (("--endpoint (the warehouse)", self.warehouse),
                            (ROLE_ENV, self.role)):
            if value is not None and not _NAME.fullmatch(value):
                raise ValueError(f"snowflake provider: {what} must be a name of at most 255 "
                                 f"printable characters")
        self.token_type = (env.get(TOKEN_TYPE_ENV) or TOKEN_TYPES[0]).strip().upper()
        if self.token_type not in TOKEN_TYPES:
            raise ValueError(f"snowflake provider: {TOKEN_TYPE_ENV} must be one of "
                             f"{', '.join(TOKEN_TYPES)}")
        token = (env.get(TOKEN_ENV) or "").strip()
        if not token:
            raise ValueError(f"snowflake provider: set {TOKEN_ENV} in the environment to a "
                             f"programmatic access token (or an OAuth token or key-pair JWT, "
                             f"naming it with {TOKEN_TYPE_ENV}) — touchstone never takes a "
                             f"credential as an argument")
        self._init_credential(token)
        self.timeout_s = float(options.get("timeout_s", 600))
        self.max_tokens = int(options.get("max_tokens", 8192))
        if not 1 <= self.max_tokens <= 128_000:
            raise ValueError("snowflake provider: max_tokens must be between 1 and 128000")
        temperature = options.get("temperature")
        self.temperature = None if temperature is None else float(temperature)
        if self.temperature is not None and not 0.0 <= self.temperature <= 1.0:
            raise ValueError("snowflake provider: temperature must be between 0 and 1")
        self.batch_size = int(options.get("batch_size", 1))
        if not 1 <= self.batch_size <= 500:
            raise ValueError("snowflake provider: batch_size must be between 1 and 500")
        self._request_id = request_id
        self._init_calls(transport or _urllib_transport(timeout=60), clock, sleep,
                         max_attempts=int(options.get("max_attempts", 4)))

    def __repr__(self) -> str:
        return (f"SnowflakeProvider(model={self.model!r}, host={self.host!r}, "
                f"warehouse={self.warehouse!r})")

    def _extra_headers(self) -> dict:
        from importlib.metadata import PackageNotFoundError, version

        try:
            release = version("touchstone")
        except PackageNotFoundError:
            release = "0"
        return {"X-Snowflake-Authorization-Token-Type": self.token_type,
                "User-Agent": f"touchstone/{release}"}

    # ------------------------------------------------------------- protocol

    def _ai_complete(self, prompt: str, criteria: Criteria) -> str:
        schema = response_format(criteria)["json_schema"]["schema"]
        parameters = {"max_tokens": self.max_tokens}
        if self.temperature is not None:
            parameters["temperature"] = self.temperature
        return (f"AI_COMPLETE(model => {_sql_literal(self.model)}, prompt => {prompt}, "
                f"model_parameters => {_constant(parameters)}, "
                f"response_format => {_constant({'type': 'json', 'schema': schema})})")

    def statement(self, criteria: Criteria) -> str:
        return (f"SELECT {self._ai_complete('?', criteria)} AS response, "
                f"NULL AS error_message")

    def batch_statement(self, criteria: Criteria) -> str:
        """One statement for a batch: bind variable 1 is a JSON array of
        {id, request}; AI_COMPLETE runs over its flattened rows, and each row
        comes back with its id."""
        return (f"SELECT f.value:id::STRING AS id, "
                f"{self._ai_complete('f.value:request::STRING', criteria)} AS response, "
                f"NULL AS error_message FROM TABLE(FLATTEN(input => PARSE_JSON(?))) f")

    @staticmethod
    def _parse(response: str, criteria: Criteria) -> tuple[dict, float]:
        # The OBJECT arrives as its JSON text; should it arrive as a JSON
        # string holding that text, unwrap it once.
        try:
            inner = json.loads(response)
        except ValueError:
            inner = None
        return parse_answers(inner if isinstance(inner, str) else response, criteria)

    # ------------------------------------------------------------ internals

    def _execute(self, sql: str, parameters: list[dict]) -> list:
        """Submit, poll to a terminal state (canceling on any way out), and
        return every result row — following result partitions, which only a
        batch is large enough to produce."""
        deadline = self._clock() + self.timeout_s
        body: dict = {"statement": sql, "timeout": max(1, int(self.timeout_s)),
                      "bindings": {str(i): {"type": "TEXT", "value": p["value"]}
                                   for i, p in enumerate(parameters, 1)}}
        if self.warehouse:
            body["warehouse"] = self.warehouse
        if self.role:
            body["role"] = self.role
        try:
            status, result = self._request("POST", f"{_STATEMENTS}?requestId="
                                           f"{self._request_id()}", body, resubmit=False,
                                           ok=_OK)
        except KeyboardInterrupt:
            raise _SubmitInterrupted() from None
        handle = result.get("statementHandle")
        if handle is not None and (not isinstance(handle, str) or not _HANDLE.fullmatch(handle)):
            raise ProviderError("the statement handle is not a valid id", "invalid response")
        wait = 1.0
        try:
            while status == 202:
                if not handle:
                    raise ProviderError("the statement was accepted without a handle",
                                        "invalid response")
                if self._clock() >= deadline:
                    raise ProviderError(f"no result within {self.timeout_s:.0f}s", "timeout")
                self._sleep(wait)
                wait = min(wait * 2, 5.0)
                status, result = self._request("GET", f"{_STATEMENTS}/{handle}", ok=_OK)
        except BaseException as e:
            # Never abandon a statement still running — whatever ends the
            # wait, an interrupt included: request a cancel and say so.
            if handle and status == 202:
                note = self._cancel(handle)
                if isinstance(e, ProviderError):
                    raise type(e)(f"{e} — {note}", e.kind) from None
                if isinstance(e, Exception):
                    raise ProviderError(self._clean(f"{type(e).__name__}: {e} — {note}"),
                                        type(e).__name__) from None
            raise
        if status == 422:
            message = f"statement FAILED: {result.get('code') or ''} " \
                      f"{result.get('message') or ''}".strip()
            raise ProviderError(self._clean(message)[:400], "statement FAILED")
        rows = result.get("data")
        if not isinstance(rows, list):
            raise ProviderError("the result holds no rows", "invalid response")
        rows = list(rows)
        meta = result.get("resultSetMetaData")
        partitions = meta.get("partitionInfo") if isinstance(meta, dict) else None
        for n in range(1, len(partitions) if isinstance(partitions, list) else 0):
            if not handle:
                raise ProviderError("a partitioned result without a handle",
                                    "invalid response")
            more = self._call("GET", f"{_STATEMENTS}/{handle}?partition={n}").get("data")
            if not isinstance(more, list):
                raise ProviderError("a result partition without rows", "invalid response")
            rows += more
        return rows

    def _cancel(self, handle: str) -> str:
        # One attempt: a throttled cancel must not hold the run for several
        # Retry-After waits past its deadline.
        try:
            self._request("POST", f"{_STATEMENTS}/{handle}/cancel", {}, attempts=1,
                          ok=(200, 422))
        except WarehouseAuthError:
            return "cancel request refused"
        except ProviderError as e:
            return f"cancel request failed ({self._clean(str(e))[:120]})"
        return "cancel requested; its terminal state was not confirmed"
