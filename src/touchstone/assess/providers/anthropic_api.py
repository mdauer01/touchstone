"""The Anthropic backend: Claude through the Anthropic API
(`--provider anthropic`; `api` is its former name).

On the 174-use study, Claude judges agreed with 165 (Haiku) to 167 (Opus 5.5)
labels; the open models a Databricks Free Edition workspace serves, 125-132.
This backend calls the Messages API directly, with the shared prompt and
answer schema (providers/direct.py):

* `output_config.format` constrains the reply to the criteria's JSON schema;
  the label is still computed by `criteria.aggregate`, never by the model;
* thinking is left at the model's default (adaptive on current models; it
  cannot be disabled on Opus 5.5) and `effort` is sent only when configured;
  no sampling parameters are sent;
* on models that support it, server-side `fallbacks: "default"` re-runs a
  declined request on Anthropic's recommended fallback model; a refusal that
  survives is an error on that assessment, never a label.

Credentials are resolved by the SDK — ANTHROPIC_API_KEY, or an `ant auth
login` profile — and touchstone never reads, stores, prints or logs them.
"""

from __future__ import annotations

import time
from typing import Callable

from touchstone.assess.criteria import Criteria
from touchstone.assess.providers import (ProviderConfig, ProviderError, render_prompt,
                                         response_format)
from touchstone.assess.providers.direct import DirectProvider

# Models that take server-side `fallbacks: "default"` on the Claude API.
_FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1",
                              "claude-sonnet-5-5"})
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# List prices per million tokens (input, output), for the cost ESTIMATE only:
# cache reads, batch discounts, fallback hops and Haiku 5.5's higher rate for
# prompts over 100K tokens are not modelled.
_PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0),
           "claude-haiku-5-5": (0.10, 0.50), "claude-fable-5-1": (10.0, 50.0)}
_EFFORTS = ("low", "medium", "high", "xhigh", "max")


class AnthropicProvider(DirectProvider):
    name = "anthropic"

    def __init__(self, config: ProviderConfig, *, client=None,
                 clock: Callable[[], float] = time.monotonic):
        options = self._setup(config, clock)
        self.max_tokens = int(options.get("max_tokens") or 16000)
        if not 1 <= self.max_tokens <= 128_000:
            raise ValueError("anthropic provider: max_tokens must be between 1 and 128000")
        self.effort = options.get("effort")
        if self.effort is not None and self.effort not in _EFFORTS:
            raise ValueError(f"anthropic provider: effort must be one of {', '.join(_EFFORTS)}")
        if options.get("temperature") is not None:
            raise ValueError("anthropic provider: sampling parameters are not sent — current "
                             "Claude models reject temperature")
        self.fallbacks = self.model in _FALLBACK_MODELS
        if client is None:
            try:
                import anthropic
            except ImportError:
                raise ValueError("anthropic provider: install the Anthropic SDK — "
                                 "pip install 'touchstone[anthropic]'") from None
            # Credentials come from the SDK's own resolution (ANTHROPIC_API_KEY
            # or an `ant auth login` profile); none is ever passed here.
            client = anthropic.Anthropic(max_retries=4)
        self._client = client

    def _reply(self, record: dict, criteria: Criteria) -> tuple[str, float | None]:
        response = self._call(render_prompt(record, criteria), criteria)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise ProviderError(
                "the model declined"
                + (f" ({details.category})" if getattr(details, "category", None) else ""),
                "refused")
        if response.stop_reason == "max_tokens":
            raise ProviderError(f"the answer was cut at max_tokens={self.max_tokens}",
                                "truncated")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ProviderError("the reply holds no text block", "invalid response")
        return text, _cost(self.model, getattr(response, "usage", None))

    def _call(self, prompt: str, criteria: Criteria):
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
