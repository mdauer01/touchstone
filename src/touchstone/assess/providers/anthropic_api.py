"""The direct API backend: Claude through the Anthropic API (`--provider api`).

A frontier model is what measured well on the drift benchmark (174 labeled
uses: Claude Opus 5.5 167 exact; the open models a Databricks Free Edition
workspace serves, 125-132), and not every workspace serves one through
ai_query. This backend calls the Messages API directly, with the same prompt
(`render_prompt`) and the same answer schema as the Databricks backend, so
the two are comparable:

* `output_config.format` constrains the reply to the criteria's JSON schema;
  the label is still computed by `criteria.aggregate`, never by the model;
* thinking is left at the model's default (adaptive on current models; it
  cannot be disabled on Opus 5.5) and `effort` is sent only when configured;
  no sampling parameters are sent;
* on models that support it, server-side `fallbacks: "default"` re-runs a
  declined request on Anthropic's recommended fallback model; a refusal that
  survives is an error on that assessment, never a label;
* records are assessed concurrently (`concurrency`, default 8).

Credentials are resolved by the SDK — ANTHROPIC_API_KEY, or an `ant auth
login` profile — and touchstone never reads, stores, prints or logs them.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from touchstone.assess.criteria import Criteria, aggregate
from touchstone.assess.providers import Assessment, ProviderConfig, render_prompt

DEFAULT_MODEL = "claude-opus-5-5"
# Models that take server-side `fallbacks: "default"` on the Claude API.
_FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1",
                              "claude-sonnet-5-5"})
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# List prices per million tokens (input, output), for the cost ESTIMATE only:
# cache reads, batch discounts and fallback hops are not modelled.
_PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0),
           "claude-haiku-5-5": (0.10, 0.50), "claude-fable-5-1": (10.0, 50.0)}
_EFFORTS = ("low", "medium", "high", "xhigh", "max")


class AnthropicAPIError(Exception):
    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


class AnthropicProvider:
    name = "api"
    measures_performance = True
    confidence_source = "self-reported by the model"

    def __init__(self, config: ProviderConfig, *, client=None,
                 clock: Callable[[], float] = time.monotonic):
        options = dict(config.options or {})
        self.model = config.model or DEFAULT_MODEL
        self.max_tokens = int(options.get("max_tokens") or 16000)
        if not 1 <= self.max_tokens <= 128_000:
            raise ValueError("api provider: max_tokens must be between 1 and 128000")
        self.effort = options.get("effort")
        if self.effort is not None and self.effort not in _EFFORTS:
            raise ValueError(f"api provider: effort must be one of {', '.join(_EFFORTS)}")
        if options.get("temperature") is not None:
            raise ValueError("api provider: sampling parameters are not sent — current Claude "
                             "models reject temperature")
        self.concurrency = max(1, int(options.get("concurrency") or 8))
        self.fallbacks = self.model in _FALLBACK_MODELS
        if client is None:
            try:
                import anthropic
            except ImportError:
                raise ValueError("api provider: install the Anthropic SDK — "
                                 "pip install 'touchstone[api]'") from None
            # Credentials come from the SDK's own resolution (ANTHROPIC_API_KEY
            # or an `ant auth login` profile); none is ever passed here.
            client = anthropic.Anthropic(max_retries=4)
        self._client = client
        self._clock = clock
        self.stopped: str | None = None
        self.stop_kind: str | None = None
        self.credential_refused: str | None = None

    def __repr__(self) -> str:
        return f"AnthropicProvider(model={self.model!r})"

    def assess(self, records: list[dict], criteria: Criteria) -> list[Assessment]:
        with ThreadPoolExecutor(self.concurrency) as pool:
            return list(pool.map(lambda r: self._one(r, criteria), records))

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
            response = self._call(render_prompt(record, criteria), criteria)
            latency = round((self._clock() - started) * 1000, 1)
            cost = _cost(self.model, getattr(response, "usage", None))
            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                raise AnthropicAPIError(
                    "the model declined"
                    + (f" ({details.category})" if getattr(details, "category", None) else ""),
                    "refused")
            if response.stop_reason == "max_tokens":
                raise AnthropicAPIError(f"the answer was cut at max_tokens={self.max_tokens}",
                                        "truncated")
            text = next((b.text for b in response.content if b.type == "text"), None)
            if text is None:
                raise AnthropicAPIError("the reply holds no text block", "invalid response")
            answers, confidence = _parse(text, criteria)
            return result(answers, confidence, latency=latency, cost=cost)
        except AnthropicAPIError as e:
            return result({}, None, str(e), e.kind,
                          round((self._clock() - started) * 1000, 1))
        except Exception as e:  # noqa: BLE001 — one record never ends the run
            kind = _error_kind(e)
            if kind == "credential refused":
                self.credential_refused = f"{type(e).__name__}: the API refused the credential"
                self.stopped, self.stop_kind = self.credential_refused, kind
            return result({}, None, f"{type(e).__name__}: {str(e)[:300]}", kind,
                          round((self._clock() - started) * 1000, 1))

    def _call(self, prompt: str, criteria: Criteria):
        from touchstone.assess.providers.databricks import response_format

        schema = response_format(criteria)["json_schema"]["schema"]
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if self.effort:
            output_config["effort"] = self.effort
        kwargs = {"model": self.model, "max_tokens": self.max_tokens,
                  "messages": [{"role": "user", "content": prompt}],
                  "output_config": output_config}
        if self.fallbacks:
            return self._client.beta.messages.create(
                **kwargs, betas=[_FALLBACK_BETA], fallbacks="default")
        return self._client.messages.create(**kwargs)


def _parse(text: str, criteria: Criteria) -> tuple[dict, float]:
    from touchstone.assess.providers.databricks import DatabricksError, DatabricksProvider

    try:
        return DatabricksProvider._parse(text, criteria)
    except DatabricksError as e:
        raise AnthropicAPIError(str(e), e.kind) from None


def _cost(model: str, usage) -> float | None:
    price = _PRICES.get(model)
    if price is None or usage is None:
        return None
    try:
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    except (TypeError, ValueError):
        return None
    return round((tokens_in * price[0] + tokens_out * price[1]) / 1_000_000, 6)


def _error_kind(e: Exception) -> str:
    name = type(e).__name__
    if name in ("AuthenticationError", "PermissionDeniedError", "CredentialsError"):
        return "credential refused"
    status = getattr(e, "status_code", None)
    if isinstance(status, int):
        return f"HTTP {status}"
    if name in ("APIConnectionError", "APITimeoutError"):
        return "transport"
    return name

