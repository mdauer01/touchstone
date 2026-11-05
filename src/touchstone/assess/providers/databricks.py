"""Databricks backend: `ai_query` with a JSON-schema response format, run on a
SQL warehouse through the SQL Statement Execution API.

What it sends, per record:

    SELECT r.result, r.errorMessage FROM (
      SELECT ai_query('<serving endpoint>', :request,
                      responseFormat => '<json schema>',
                      failOnError => false,
                      modelParameters => named_struct('max_tokens', N)) AS r)

* the prompt is `render_prompt` (the statement as it ran, its execution and
  session, the definition with its validity window, the governing variation
  policy, schemas; touchstone's observations last) and travels as the named
  parameter `:request` — never spliced into SQL text;
* with `batch_size` > 1, one statement assesses a batch: the prompts travel
  as ONE named parameter `:batch` (a JSON array of {id, request}), exploded
  into rows, and ai_query runs over the rows — one statement per batch instead
  of one per record;
* `temperature` is sent only when configured: reasoning models (gpt-oss,
  Claude with thinking) reject or ignore it, and 0.0 never made answers
  reproducible on a hosted endpoint anyway. `max_tokens` defaults to 8192:
  reasoning tokens count against it, and 1024 truncated measured judgments;
* the endpoint, response format and model parameters are validated literals:
  Databricks documents the endpoint and failOnError as literals and
  modelParameters as constants, and parameter markers in literal positions
  are documented for Databricks Runtime 18.0+ only;
* `failOnError => false` returns STRUCT<result, errorMessage> — `result`, not
  `response`: the first live run (2026-11-04) refuted the earlier reading of
  the docs. The two fields are selected as scalar columns, so no STRUCT has to
  be serialized by the API;
* a json_schema response format returns a JSON STRING, parsed here; the model
  answers the criteria's questions, and `criteria.aggregate` — not the model —
  computes the label.

Verified against the official docs (docs.databricks.com: ai_query, Statement
Execution API) on 2026-10-26. INFERRED, to confirm on the first live run:
that a parameter marker is accepted as ai_query's request argument on a SQL
warehouse, and that the chosen chat model accepts this response format.

Credentials: the access token is read from DATABRICKS_TOKEN (a personal
access token or an OAuth access token the operator obtained) or, when that is
unset, from a Databricks CLI login profile (`databricks auth login --profile
<name>`, named by --profile or DATABRICKS_CONFIG_PROFILE): a short-lived OAuth
token from `databricks auth token`, refreshed before it expires, with the host
read from the profile. Either way it is sent only in the Authorization header,
and never stored on the provider's public attributes, logged, printed, or
included in an error or an assessment.
`ai_query` reports no token usage, so cost is reported as not reported —
system.ai_gateway.usage holds it for account and metastore admins.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from touchstone.assess.criteria import Criteria
from touchstone.assess.providers import ProviderConfig, ProviderError, response_format
from touchstone.assess.providers.warehouse import (  # noqa: F401 — re-exported
    TokenSource, Transport, WarehouseAuthError, WarehouseProvider,
    _NoRedirect, _SubmitInterrupted, _urllib_transport, https_host)

# The former names: both are the shared provider errors now.
DatabricksError = ProviderError
DatabricksAuthError = WarehouseAuthError

TOKEN_ENV = "DATABRICKS_TOKEN"
HOST_ENV = "DATABRICKS_HOST"
WAREHOUSE_ENV = "DATABRICKS_WAREHOUSE_ID"
PROFILE_ENV = "DATABRICKS_CONFIG_PROFILE"
CONFIG_FILE = Path.home() / ".databrickscfg"
TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"})
_ENDPOINT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,254}")
_WAREHOUSE = re.compile(r"[A-Za-z0-9]{1,64}")
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}")
_CHUNKS = "/api/2.0/sql/statements/"
_BATCH_TYPE = "array<struct<id:string,request:string>>"


def _sql_literal(text: str) -> str:
    """A Databricks SQL string literal for text this module generated (no
    user data). Databricks SQL escapes with a backslash (`'Spark\\'s'`); a doubled
    quote is two adjacent literals, which concatenate and LOSE the quote."""
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


_AUTH_CODES = frozenset({"UNAUTHENTICATED", "PERMISSION_DENIED"})


def _parse_host(raw: str) -> str:
    return https_host(raw, f"databricks provider: set {HOST_ENV} (or --host) to the workspace "
                           f"URL — https://<workspace host>, with no path, query or user part")


def _profile_host(profile: str, path: Path) -> str:
    """The `host` of a ~/.databrickscfg profile — and nothing else from the
    file: a PAT profile also holds its token there."""
    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error):
        return ""
    if not parser.has_section(profile):
        return ""
    return parser.get(profile, "host", fallback="").strip()


def _cli_token_source(profile: str, wall: Callable[[], float]) -> TokenSource:
    """A short-lived OAuth token from `databricks auth token` for a profile the
    operator logged in with (`databricks auth login`). The CLI refreshes it
    from its own cache. Its output holds the token, so no part of stdout ever
    reaches an error; stderr is the CLI's own words."""
    def fetch() -> tuple[str, float | None]:
        try:
            proc = subprocess.run(["databricks", "auth", "token", "--profile", profile,
                                   "-o", "json"], capture_output=True, text=True, timeout=120)
        except FileNotFoundError:
            raise DatabricksAuthError("the databricks CLI is not installed — install it, then "
                                      f"run `databricks auth login --profile {profile}`",
                                      "credential unavailable") from None
        except subprocess.TimeoutExpired:
            raise DatabricksAuthError("`databricks auth token` did not answer within 120s",
                                      "credential unavailable") from None
        if proc.returncode != 0:
            said = (proc.stderr or "").strip().splitlines()
            raise DatabricksAuthError(
                f"`databricks auth token --profile {profile}` failed"
                + (f": {said[0][:200]}" if said else "")
                + f" — run `databricks auth login --profile {profile}`",
                "credential unavailable")
        try:
            data = json.loads(proc.stdout)
            token = str(data["access_token"]).strip()
        except (ValueError, KeyError, TypeError):
            raise DatabricksAuthError("`databricks auth token` returned no access token",
                                      "credential unavailable") from None
        expires_in = data.get("expires_in")
        expiry = (wall() + float(expires_in)
                  if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)
                  else None)
        return token, expiry
    return fetch


class DatabricksProvider(WarehouseProvider):
    name = "databricks"
    place = "workspace"
    token_env = TOKEN_ENV

    def __init__(self, config: ProviderConfig, *, transport: Transport | None = None,
                 env: dict | None = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 token_source: TokenSource | None = None,
                 wall: Callable[[], float] = time.time, config_file: Path | None = None):
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
        token = (env.get(TOKEN_ENV) or "").strip()
        # A login profile is used only when no token is set: an explicit
        # DATABRICKS_TOKEN always wins, as it does for the Databricks tools.
        self.profile = None if token else (str(options.get("profile") or env.get(PROFILE_ENV)
                                                or "").strip() or None)
        if self.profile is not None and not _PROFILE.fullmatch(self.profile):
            raise ValueError("databricks provider: --profile must name a ~/.databrickscfg "
                             "profile — letters, digits, '.', '_' and '-' only")
        profile_host = (_profile_host(self.profile, config_file or CONFIG_FILE)
                        if self.profile else "")
        self.host = _parse_host(str(options.get("host") or env.get(HOST_ENV) or profile_host
                                    or ""))
        if token:
            self._init_credential(token, wall=wall)
        elif self.profile:
            try:
                self._init_credential(source=token_source or _cli_token_source(self.profile,
                                                                               wall), wall=wall)
            except ProviderError as e:
                raise ValueError(f"databricks provider: {e}") from None
        else:
            raise ValueError(f"databricks provider: set {TOKEN_ENV} in the environment, or log "
                             f"in with `databricks auth login --profile <name>` and name the "
                             f"profile with --profile or {PROFILE_ENV} — touchstone never "
                             f"takes a credential as an argument")
        self.timeout_s = float(options.get("timeout_s", 600))
        self.max_tokens = int(options.get("max_tokens", 8192))
        if not 1 <= self.max_tokens <= 100_000:
            raise ValueError("databricks provider: max_tokens must be between 1 and 100000")
        temperature = options.get("temperature")
        self.temperature = None if temperature is None else float(temperature)
        if self.temperature is not None and not 0.0 <= self.temperature <= 2.0:
            raise ValueError("databricks provider: temperature must be between 0 and 2")
        self.batch_size = int(options.get("batch_size", 1))
        if not 1 <= self.batch_size <= 500:
            raise ValueError("databricks provider: batch_size must be between 1 and 500")
        self._init_calls(transport or _urllib_transport(timeout=60), clock, sleep,
                         max_attempts=int(options.get("max_attempts", 4)))

    def __repr__(self) -> str:
        return (f"DatabricksProvider(model={self.model!r}, host={self.host!r}, "
                f"warehouse_id={self.warehouse_id!r})")

    # ------------------------------------------------------------- protocol

    def _ai_query(self, request: str, criteria: Criteria) -> str:
        fmt = json.dumps(response_format(criteria), separators=(",", ":"))
        return (f"ai_query({_sql_literal(self.model)}, {request}, "
                f"responseFormat => {_sql_literal(fmt)}, failOnError => false, "
                f"modelParameters => {self._model_parameters()})")

    def statement(self, criteria: Criteria) -> str:
        return ("SELECT r.result AS response, r.errorMessage AS error_message FROM ("
                f"SELECT {self._ai_query(':request', criteria)} AS r)")

    def batch_statement(self, criteria: Criteria) -> str:
        """One statement for a batch: `:batch` is a JSON array of {id, request};
        ai_query runs over its exploded rows, and each row comes back with its id."""
        return ("SELECT b.id AS id, b.r.result AS response, b.r.errorMessage AS error_message "
                f"FROM (SELECT x.id AS id, {self._ai_query('x.request', criteria)} AS r "
                f"FROM (SELECT explode(from_json(:batch, {_sql_literal(_BATCH_TYPE)})) AS x) AS e"
                ") AS b")


    def _model_parameters(self) -> str:
        fields = ([f"'temperature', {self.temperature!r}"] if self.temperature is not None
                  else []) + [f"'max_tokens', {self.max_tokens}"]
        return f"named_struct({', '.join(fields)})"

    def _execute(self, sql: str, parameters: list[dict]) -> list | None:
        """Submit, poll to a terminal state (canceling on any way out), and
        return every result row — following result chunks, which only a batch
        is large enough to produce."""
        deadline = self._clock() + self.timeout_s
        body = {"warehouse_id": self.warehouse_id, "statement": sql,
                "parameters": parameters,
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
        chunk = result.get("result") if isinstance(result.get("result"), dict) else {}
        rows = chunk.get("data_array")
        if rows is None or not isinstance(rows, list):
            return rows
        rows = list(rows)
        link = chunk.get("next_chunk_internal_link")
        while link:
            # Only a path under the statements API: never a host the
            # response names, which would be handed the Authorization header.
            if not isinstance(link, str) or not link.startswith(_CHUNKS) or "//" in link:
                raise DatabricksError("a result chunk link outside the statements API",
                                      "invalid response")
            chunk = self._call("GET", link)
            more = chunk.get("data_array")
            if not isinstance(more, list):
                raise DatabricksError("a result chunk without rows", "invalid response")
            rows += more
            link = chunk.get("next_chunk_internal_link")
        return rows

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



def _state(result: dict) -> str | None:
    status = result.get("status")
    state = status.get("state") if isinstance(status, dict) else None
    return state if isinstance(state, str) else None

