"""What the warehouse backends (Databricks, Snowflake) share: the model runs
inside the customer's own platform, through a SQL function, on a statement
submitted over that platform's REST API.

Per record (or per batch of records) one statement is submitted, polled to a
terminal state — and canceled on any way out — and its rows parsed against
the criteria's answer schema. A subclass supplies the SQL (`statement`,
`batch_statement`), the submit-and-poll protocol (`_execute`, `_cancel`) and
its credential; everything else is here:

* the credential is held name-mangled, sent only in the Authorization header,
  and redacted — whole and in any 8-character fragment — from every error
  before it is cut or shown;
* redirects are never followed (urllib would copy the Authorization header
  to the redirected host);
* a failure is an error on the assessments it touches, never a label; a
  refused credential or an unreachable platform stops the CALLS and keeps
  what was answered.
"""

from __future__ import annotations

import json
import re
from typing import Callable
from urllib import error as urlerror
from urllib import request as urlrequest

from touchstone.assess.criteria import Criteria, aggregate
from touchstone.assess.providers import (Assessment, ProviderError, parse_answers,
                                         render_prompt)

# A profile token is refreshed this many seconds before it expires.
_REFRESH_MARGIN_S = 300.0

# transport(method, url, headers, body) -> (status, headers, body)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict, bytes]]
# token_source() -> (access token, expiry as epoch seconds or None)
TokenSource = Callable[[], tuple[str, float | None]]


class WarehouseAuthError(ProviderError):
    """The platform refused the credential or the permission. Every later
    call would fail the same way, so the provider stops CALLING — the items
    already answered are kept, the rest are failed (not attempted), and the
    run is never reported as a model that abstained on everything."""

    def __init__(self, message: str, kind: str = "credential refused"):
        super().__init__(message, kind)


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


class _SubmitInterrupted(KeyboardInterrupt):
    """Ctrl-C while the statement POST was in flight: the statement may run,
    and no id is known to cancel it."""


_TOKEN = re.compile(r"[\x21-\x7e]+")      # printable ASCII, no whitespace


def https_host(raw: str, hint: str) -> str:
    """https://<hostname>[:port] — no userinfo, path, query or fragment; `hint`
    is the ValueError's message."""
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
        raise ValueError(hint)
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname   # IPv6
    return f"https://{host}" + (f":{port}" if port else "")


class WarehouseProvider:
    name = ""
    place = "platform"            # how errors name the far side: workspace, account
    token_env = ""                # where the credential comes from, for messages
    measures_performance = True
    confidence_source = "self-reported by the model"

    def _init_credential(self, token: str = "", *, source: TokenSource | None = None,
                         wall: Callable[[], float] | None = None) -> None:
        """Hold the credential: a fixed token, or a `source` that issues
        short-lived ones (refreshed before they expire)."""
        import time

        self._wall = wall or time.time
        self.__token = ""             # name-mangled; never part of repr or output
        self.__grams: frozenset = frozenset()
        self.__expiry: float | None = None
        self.__source: TokenSource | None = source
        if source is not None:
            self.__set_token(*source())
        else:
            self.__set_token(token, None)

    def _init_calls(self, send: Transport, clock: Callable[[], float],
                    sleep: Callable[[float], None], *, max_attempts: int) -> None:
        self.max_attempts = max(1, max_attempts)
        self._send = send
        self._clock = clock
        self._sleep = sleep
        # Set when the run must stop calling — the platform refused the
        # credential, cannot be reached, or the operator interrupted — and
        # why; no call is made after it.
        self.credential_refused: str | None = None
        self.stopped: str | None = None
        self.stop_kind: str | None = None     # credential refused | unreachable | interrupted

    def _extra_headers(self) -> dict:
        return {}

    def assess(self, records: list[dict], criteria: Criteria) -> list[Assessment]:
        """One assessment per record. A failure on one record is an error on
        THAT assessment (never a label, never the end of the run). A refused
        credential stops the CALLS — every later one would fail alike — and
        the records not attempted are failed, so what was answered survives
        (an OAuth token expiring mid-run discarded the whole report)."""
        if self.batch_size > 1:
            return self._assess_batched(records, criteria)
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
        except ProviderError as e:
            answers, confidence, error, kind = {}, None, self._clean(str(e)), e.kind
            if isinstance(e, WarehouseAuthError):
                self.credential_refused = error
                self._stop(error, "credential refused")
            elif e.kind == "unreachable":
                self._stop(error, "unreachable")
        except Exception as e:  # noqa: BLE001 — one record never ends the run
            answers, confidence = {}, None
            error, kind = self._clean(f"{type(e).__name__}: {e}")[:300], type(e).__name__
        return answers, confidence, error, kind, round((self._clock() - started) * 1000, 1)

    def _assess_batched(self, records: list[dict], criteria: Criteria) -> list[Assessment]:
        """The same contract as one statement per record — a failure is an error
        on the assessments it touches, a refused credential stops the calls and
        keeps what was answered — with one statement per batch. A statement-level
        failure fails its whole batch; a row the endpoint failed fails that record
        only. Latency is the batch's wall time divided over its records."""
        sql = self.batch_statement(criteria)
        out: list[Assessment] = []

        def assessment(record, answers, confidence, error, kind, latency):
            return Assessment(
                record_id=record["record_id"], provider=self.name, model=self.model,
                criteria_version=criteria.version, answers=answers,
                label=aggregate(answers, criteria) if error is None else "insufficient_evidence",
                probabilities=None, confidence=confidence, latency_ms=latency,
                cost_estimate_usd=None, error=error, error_kind=kind)

        for start in range(0, len(records), self.batch_size):
            batch = records[start:start + self.batch_size]
            if self.stopped:
                out += [assessment(r, {}, None, f"not attempted: {self.stopped}",
                                   "not attempted", None) for r in batch]
                continue
            started = self._clock()
            payload = json.dumps([{"id": str(i), "request": render_prompt(r, criteria)}
                                  for i, r in enumerate(batch)])
            try:
                rows = self._execute(sql, [{"name": "batch", "value": payload,
                                            "type": "STRING"}])
            except KeyboardInterrupt as e:
                kind = "interrupted"
                error = ("interrupted while the statement was being submitted — it may be "
                         "running on the warehouse, and its id is not known"
                         if isinstance(e, _SubmitInterrupted) else "interrupted")
                self._stop("the run was interrupted by the operator", "interrupted")
                out += [assessment(r, {}, None, error, kind, None) for r in batch]
                continue
            except ProviderError as e:
                error = self._clean(str(e))
                if isinstance(e, WarehouseAuthError):
                    self.credential_refused = error
                    self._stop(error, "credential refused")
                elif e.kind == "unreachable":
                    self._stop(error, "unreachable")
                out += [assessment(r, {}, None, error, e.kind, None) for r in batch]
                continue
            except Exception as e:  # noqa: BLE001 — one batch never ends the run
                error = self._clean(f"{type(e).__name__}: {e}")[:300]
                out += [assessment(r, {}, None, error, type(e).__name__, None) for r in batch]
                continue
            latency = round((self._clock() - started) * 1000 / len(batch), 1)
            by_id: dict[str, list] = {}
            for row in rows:
                if isinstance(row, list) and len(row) == 3 and isinstance(row[0], str):
                    by_id.setdefault(row[0], []).append(row)
            for i, record in enumerate(batch):
                got = by_id.get(str(i), [])
                answers, confidence, error, kind = {}, None, None, None
                if len(got) != 1:
                    error, kind = (f"expected exactly one result row for this record, got "
                                   f"{len(got)}", "invalid response")
                else:
                    _, response, model_error = got[0]
                    try:
                        if model_error is not None:
                            raise ProviderError(f"the model endpoint returned an error: "
                                                  f"{self._clean(str(model_error))[:300]}",
                                                  "model error")
                        if not isinstance(response, str):
                            raise ProviderError("the model returned no response",
                                                  "invalid response")
                        answers, confidence = self._parse(response, criteria)
                    except ProviderError as e:
                        error, kind = self._clean(str(e)), e.kind
                out.append(assessment(record, answers, confidence, error, kind, latency))
        return out

    def _stop(self, why: str, kind: str) -> None:
        self.stopped, self.stop_kind = why, kind

    def __set_token(self, token: str, expiry: float | None) -> None:
        if not _TOKEN.fullmatch(token):
            # Never echoed: a token with a stray newline or a pasted curly quote
            # would otherwise surface verbatim in an "invalid header" error.
            raise ValueError(f"{self.name} provider: {self.token_env} contains whitespace or "
                             f"non-ASCII characters — re-copy the token")
        self.__token, self.__expiry = token, expiry
        # Every 8-character window of every token this run has held, for the
        # partial-echo scan: an echo of the token a refresh replaced is still
        # a secret in an error message.
        self.__grams = self.__grams | frozenset(token[i:i + 8]
                                                for i in range(len(token) - 7))

    def _headers(self) -> dict:
        if self.__source is not None and self.__expiry is not None \
                and self._wall() >= self.__expiry - _REFRESH_MARGIN_S:
            try:
                self.__set_token(*self.__source())
            except ValueError as e:
                raise WarehouseAuthError(str(e), "credential unavailable") from None
        return {"Authorization": f"Bearer {self.__token}",
                "Content-Type": "application/json", "Accept": "application/json",
                **self._extra_headers()}

    def _clean(self, text: str) -> str:
        """Redact the token — always BEFORE any truncation — and any run of
        eight or more of its characters: a server that cuts its echo of the
        Authorization value leaves a fragment the whole-token match misses.
        Linear in the text: each position is looked up once among the
        token's 8-character windows (an OAuth JWT is thousands long)."""
        text = str(text)
        if self.__token:
            text = text.replace(self.__token, "[redacted]")
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
        return self._request(method, path, body, resubmit=resubmit, attempts=attempts)[1]

    def _request(self, method: str, path: str, body: dict | None = None, *,
                 resubmit: bool = True, attempts: int | None = None,
                 ok: tuple[int, ...] = (200,)) -> tuple[int, dict]:
        """One API call: (status, JSON object) for a status in `ok`.
        `resubmit=False` (the statement POST) never re-sends after a transport
        failure that may have reached the server — that could run the model
        twice — but does after one that provably did not (connection refused,
        the name did not resolve). 429/503 are explicit refusals and retried,
        within `attempts` (default max_attempts)."""
        import gzip
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
                    # The platform cannot be reached at all: every later call
                    # would wait out the same retries, so the run stops.
                    raise ProviderError(f"{method} {path}: {type(e).__name__} — the "
                                        f"{self.place} could not be reached",
                                        "unreachable") from None
                if (not resubmit and not _never_sent(e)) or attempt == attempts:
                    raise ProviderError(f"{method} {path}: {type(e).__name__}",
                                        "transport") from None
                self._sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            if status in (429, 503) and attempt < attempts:
                self._sleep(_retry_after(headers, delay))
                delay = min(delay * 2, 30.0)
                continue
            if raw and any(str(k).lower() == "content-encoding" and "gzip" in str(v).lower()
                           for k, v in (headers or {}).items()):
                try:
                    raw = gzip.decompress(raw)
                except (OSError, EOFError):
                    raise ProviderError(f"{method} {path}: the gzip body does not decompress",
                                        "invalid response") from None
            text = raw.decode("utf-8", "replace") if raw else ""
            if status not in ok:
                # Decoded FIRST — a JSON escape can spell the token, and a
                # redaction before decoding split it — then redacted, then
                # cut. Only an error's text is ever shown.
                message = self._clean(_error_text(text)[:4096])[:300]
                if status in (401, 403):
                    raise WarehouseAuthError(f"{method} {path}: HTTP {status}: the {self.place} "
                                             f"refused the credential or permission — "
                                             f"{message}")
                raise ProviderError(f"{method} {path}: HTTP {status}: {message}",
                                    f"HTTP {status}")
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                raise ProviderError(f"{method} {path}: the response is not JSON",
                                    "invalid response") from None
            if not isinstance(data, dict):
                raise ProviderError(f"{method} {path}: the response is not a JSON object",
                                    "invalid response")
            return status, data
        raise ProviderError(f"{method} {path}: retries exhausted", "retries exhausted")

    def _run(self, sql: str, prompt: str) -> str:
        rows = self._execute(sql, [{"name": "request", "value": prompt, "type": "STRING"}])
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], list) \
                or len(rows[0]) != 2:
            raise ProviderError("expected exactly one row of (response, error_message)",
                                  "invalid response")
        response, model_error = rows[0]
        if model_error is not None:
            raise ProviderError(f"the model endpoint returned an error: "
                                  f"{self._clean(str(model_error))[:300]}", "model error")
        if not isinstance(response, str):
            raise ProviderError("the model returned no response", "invalid response")
        return response

    _parse = staticmethod(parse_answers)


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
        return " ".join(str(data.get(k)) for k in ("error_code", "code", "message")
                        if data.get(k))
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
