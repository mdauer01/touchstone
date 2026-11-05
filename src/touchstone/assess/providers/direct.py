"""What the direct API backends (Anthropic, OpenAI-compatible) share: one
request per record, `concurrency` at once, with the same prompt
(`render_prompt`) and answer schema (`response_format`) as the warehouse
backends, so every backend is comparable on the same benchmark.

* `--model` is required: touchstone does not pick a model. Which ones were
  measured, and how they did, is in docs/assess.md (Choosing a model) — and
  `touchstone assess benchmark` measures the one you choose;
* a refusal, a truncated answer or a reply that violates the schema is an
  error on that assessment, never a label;
* a refused credential stops the calls; what was answered is kept.

Credentials are resolved by each vendor's SDK from the environment;
touchstone never reads, stores, prints or logs them.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from touchstone.assess.criteria import Criteria, aggregate
from touchstone.assess.providers import (Assessment, ProviderConfig, ProviderError,
                                         parse_answers, render_prompt)


class DirectProvider:
    name = ""
    measures_performance = True
    confidence_source = "self-reported by the model"

    def _setup(self, config: ProviderConfig, clock: Callable[[], float]) -> dict:
        options = dict(config.options or {})
        if not config.model:
            raise ValueError(f"{self.name} provider: --model is required — touchstone does "
                             f"not pick a model; docs/assess.md (Choosing a model) lists the "
                             f"measured ones, and `touchstone assess benchmark` measures yours")
        self.model = config.model
        self.concurrency = max(1, int(options.get("concurrency") or 8))
        self._clock = clock
        self.stopped: str | None = None
        self.stop_kind: str | None = None
        self.credential_refused: str | None = None
        return options

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r})"

    def assess(self, records: list[dict], criteria: Criteria) -> list[Assessment]:
        with ThreadPoolExecutor(self.concurrency) as pool:
            return list(pool.map(lambda r: self._one(r, criteria), records))

    def _reply(self, prompt: str, criteria: Criteria) -> tuple[str, float | None]:
        """(the reply's text, its estimated cost or None); a refusal, a
        truncation or a reply without text raises ProviderError."""
        raise NotImplementedError

    def _one(self, record: dict, criteria: Criteria) -> Assessment:
        def result(answers, confidence, error=None, kind=None, latency=None, cost=None):
            return Assessment(
                record_id=record["record_id"], provider=self.name, model=self.model,
                criteria_version=criteria.version, answers=answers,
                label=aggregate(answers, criteria) if error is None else "insufficient_evidence",
                probabilities=None, confidence=confidence, latency_ms=latency,
                cost_estimate_usd=cost, error=error, error_kind=kind)

        if self.stopped:
            return result({}, None, f"not attempted: {self.stopped}", "not attempted")
        started = self._clock()
        try:
            text, cost = self._reply(render_prompt(record, criteria), criteria)
            latency = round((self._clock() - started) * 1000, 1)
            answers, confidence = parse_answers(text, criteria)
            return result(answers, confidence, latency=latency, cost=cost)
        except ProviderError as e:
            return result({}, None, str(e), e.kind,
                          round((self._clock() - started) * 1000, 1))
        except Exception as e:  # noqa: BLE001 — one record never ends the run
            kind = error_kind(e)
            if kind == "credential refused":
                self.credential_refused = f"{type(e).__name__}: the API refused the credential"
                self.stopped, self.stop_kind = self.credential_refused, kind
            return result({}, None, f"{type(e).__name__}: {str(e)[:300]}", kind,
                          round((self._clock() - started) * 1000, 1))


def error_kind(e: Exception) -> str:
    """The kind of an SDK exception, by the names both vendors' SDKs use."""
    name = type(e).__name__
    if name in ("AuthenticationError", "PermissionDeniedError", "CredentialsError"):
        return "credential refused"
    status = getattr(e, "status_code", None)
    if isinstance(status, int):
        return f"HTTP {status}"
    if name in ("APIConnectionError", "APITimeoutError"):
        return "transport"
    return name
