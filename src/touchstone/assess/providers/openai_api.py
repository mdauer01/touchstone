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

Credentials are resolved by the OpenAI SDK — OPENAI_API_KEY (a proxy's own key
for a LiteLLM proxy; any placeholder for a local server that checks none) —
and touchstone never reads, stores, prints or logs them. The base URL comes
from --base-url or OPENAI_BASE_URL.
"""

from __future__ import annotations

import time
from typing import Callable
from urllib.parse import urlsplit

from touchstone.assess.criteria import Criteria
from touchstone.assess.providers import ProviderConfig, ProviderError, response_format
from touchstone.assess.providers.direct import DirectProvider

_LOCAL = frozenset({"localhost", "127.0.0.1", "::1"})


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

    def _reply(self, prompt: str, criteria: Criteria) -> tuple[str, float | None]:
        kwargs: dict = {"model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": response_format(criteria)}
        if self.max_tokens is not None:
            kwargs["max_completion_tokens"] = self.max_tokens
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.effort:
            kwargs["reasoning_effort"] = self.effort
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
