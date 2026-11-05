"""The OpenAI-compatible backend (`--provider openai`): OpenAI's API, or any
server that speaks its Chat Completions API — a LiteLLM proxy (and every
provider behind it), vLLM, Groq, Ollama — named by `--base-url`.

It sends the shared prompt and answer schema (providers/direct.py):

* `response_format` is the criteria's JSON schema in strict mode; a server
  that cannot enforce it may still answer, and an answer that violates the
  schema is an error, never a label;
* `max_completion_tokens`, `temperature` and `reasoning_effort` are sent only
  when configured: reasoning models reject sampling parameters, and not every
  compatible server accepts every field;
* a refusal (`message.refusal`, or `finish_reason` `content_filter`) and a
  truncated answer (`finish_reason` `length`) are errors, never labels;
* no cost is estimated: one model name can be priced differently on every
  server this reaches.

With `decisions` (`--decisions`, OpenAI's own API only), the record goes to
the Decisions API (`POST /v1/decisions`) instead: a classifier that returns,
for each question, the chosen answer and a probability for every answer, and
generates no text. The five component questions are `choice` questions whose
choices are the criteria's answers; the label is still computed by
`criteria.aggregate`. The confidence is the product of each chosen answer's
probability (the probability that every answer is right, if the questions are
independent), and each component's probability is kept. Only criteria with a
policy (v3 on) are sent this way. Only input tokens are billed.

Credentials are resolved by the OpenAI SDK — OPENAI_API_KEY (a proxy's own key
for a LiteLLM proxy; any placeholder for a local server that checks none) —
and touchstone never reads, stores, prints or logs them. The base URL comes
from --base-url or OPENAI_BASE_URL.
"""

from __future__ import annotations

import json
import time
from typing import Callable
from urllib.parse import urlsplit

from touchstone.assess.criteria import Criteria
from touchstone.assess.providers import (ProviderConfig, ProviderError,
                                         against_definition_parts, render_prompt,
                                         response_format)
from touchstone.assess.providers.direct import DirectProvider

_LOCAL = frozenset({"localhost", "127.0.0.1", "::1"})
# Decisions API list price per million uncached input tokens (output is not
# billed), for the cost ESTIMATE only: cache reads are not modelled.
_DECISIONS_INPUT_PRICE = {"gpt-6-luna": 0.10}


def _base_url(raw: str) -> str:
    """https://… — or http://… on this machine only, where a local server
    (vLLM, Ollama) listens: a key sent over plain HTTP elsewhere is exposed."""
    try:
        parts = urlsplit(raw.strip())
        parts.port                       # noqa: B018 — raises on a bad port
    except ValueError:
        parts = None
    if parts is None or not parts.hostname or parts.username or parts.password \
            or parts.query or parts.fragment \
            or not (parts.scheme == "https"
                    or (parts.scheme == "http" and parts.hostname in _LOCAL)):
        raise ValueError("openai provider: --base-url must be an https:// URL (http:// only "
                         "for a server on this machine), with no user part, query or "
                         "fragment — for example https://litellm.example.com/v1")
    return raw.strip().rstrip("/")


class OpenAIProvider(DirectProvider):
    name = "openai"

    def __init__(self, config: ProviderConfig, *, client=None,
                 clock: Callable[[], float] = time.monotonic):
        options = self._setup(config, clock)
        raw = options.get("base_url")
        self.base_url = _base_url(str(raw)) if raw else None
        max_tokens = options.get("max_tokens")
        self.max_tokens = None if max_tokens is None else int(max_tokens)
        if self.max_tokens is not None and not 1 <= self.max_tokens <= 1_000_000:
            raise ValueError("openai provider: max_tokens must be between 1 and 1000000")
        temperature = options.get("temperature")
        self.temperature = None if temperature is None else float(temperature)
        if self.temperature is not None and not 0.0 <= self.temperature <= 2.0:
            raise ValueError("openai provider: temperature must be between 0 and 2")
        self.effort = options.get("effort")
        # "flex": OpenAI's slower, half-price processing — fine for a monitor
        # that judges query history in the background.
        self.service_tier = options.get("service_tier")
        if self.service_tier not in (None, "auto", "default", "flex"):
            raise ValueError("openai provider: service tier must be auto, default or flex")
        self.decisions = bool(options.get("decisions"))
        if self.decisions:
            if self.base_url:
                raise ValueError("openai provider: the Decisions API is OpenAI's own — "
                                 "--decisions takes no --base-url")
            if any(v is not None for v in (self.max_tokens, self.temperature, self.effort)):
                raise ValueError("openai provider: the Decisions API takes no --max-tokens, "
                                 "--temperature or --effort")
            self.name = "openai-decisions"
        if client is None:
            try:
                import openai
            except ImportError:
                raise ValueError("openai provider: install the OpenAI SDK — "
                                 "pip install 'touchstone[openai]'") from None
            try:
                # The key comes from the SDK's own resolution (OPENAI_API_KEY);
                # none is ever passed here. Without --base-url the SDK reads
                # OPENAI_BASE_URL, else OpenAI's own API.
                client = openai.OpenAI(base_url=self.base_url, max_retries=4)
            except openai.OpenAIError:
                raise ValueError("openai provider: set OPENAI_API_KEY in the environment — "
                                 "touchstone never takes a credential as an "
                                 "argument") from None
        self._client = client

    def __repr__(self) -> str:
        return f"OpenAIProvider(model={self.model!r}, base_url={self.base_url!r})"

    def _reply(self, record: dict, criteria: Criteria) -> tuple:
        if self.decisions:
            return self._decide(record, criteria)
        kwargs: dict = {"model": self.model,
                        "messages": [{"role": "user",
                                      "content": render_prompt(record, criteria)}],
                        "response_format": response_format(criteria)}
        if self.max_tokens is not None:
            kwargs["max_completion_tokens"] = self.max_tokens
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.effort:
            kwargs["reasoning_effort"] = self.effort
        if self.service_tier:
            kwargs["service_tier"] = self.service_tier
        response = self._client.chat.completions.create(**kwargs)
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise ProviderError("the reply holds no choice", "invalid response")
        choice = choices[0]
        message = getattr(choice, "message", None)
        refusal = getattr(message, "refusal", None)
        if refusal:
            raise ProviderError(f"the model declined: {str(refusal)[:200]}", "refused")
        finish = getattr(choice, "finish_reason", None)
        if finish == "content_filter":
            raise ProviderError("the reply was withheld by a content filter", "refused")
        if finish == "length":
            raise ProviderError("the answer was cut at the token limit", "truncated")
        text = getattr(message, "content", None)
        if not isinstance(text, str) or not text:
            raise ProviderError("the reply holds no text", "invalid response")
        return text, None

    def _decide(self, record: dict, criteria: Criteria) -> tuple[str, float | None, dict]:
        """One Decisions API request: the instructions, policy and definition
        as developer text, the statement as user text (DATA between nonce
        markers), and one `choice` question per component."""
        if not criteria.policy or criteria.citations:
            raise ProviderError("the Decisions API is used with criteria v3 (a policy, no "
                                "rule citations)", "unsupported criteria")
        parts = against_definition_parts(record, criteria)
        questions = [{"type": "choice", "name": name,
                      "instructions": spec["question"].strip(),
                      "choices": [{"value": answer, "description": str(meaning)}
                                  for answer, meaning in spec["answers"].items()]}
                     for name, spec in criteria.components.items()]
        response = self._client.decisions.create(
            model=self.model,
            input=[{"role": "developer", "content": [{"type": "input_text", "text": "\n\n".join(
                        [parts["intro"], parts["policy"], parts["definition"]])}]},
                   {"role": "user", "content": [{"type": "input_text",
                                                 "text": parts["statement"]}]}],
            questions=questions)
        by_name = {getattr(a, "name", None): a for a in getattr(response, "answers", None) or []}
        answers, chosen = {}, {}
        for name in criteria.components:
            a = by_name.get(name)
            if a is None:
                raise ProviderError(f"the decision holds no answer to {name}", "invalid response")
            if getattr(a, "type", None) == "refusal":
                raise ProviderError(f"the model declined to answer {name}", "refused")
            choice = getattr(a, "choice", None)
            probability = next((getattr(p, "probability", None)
                                for p in getattr(a, "probabilities", None) or []
                                if getattr(p, "value", None) == choice), None)
            if not isinstance(probability, (int, float)) or isinstance(probability, bool):
                raise ProviderError(f"the decision gives no probability for {name}",
                                    "invalid response")
            answers[name], chosen[name] = choice, float(probability)
        confidence = 1.0
        for probability in chosen.values():
            confidence *= probability
        usage = getattr(response, "usage", None)
        price = _DECISIONS_INPUT_PRICE.get(self.model)
        tokens = getattr(usage, "input_tokens", None) if usage is not None else None
        cost = (round(int(tokens) * price / 1_000_000, 6)
                if price is not None and isinstance(tokens, int) else None)
        text = json.dumps({"answers": answers, "confidence": min(1.0, max(0.0, confidence))})
        return text, cost, {name: round(p, 4) for name, p in chosen.items()}
