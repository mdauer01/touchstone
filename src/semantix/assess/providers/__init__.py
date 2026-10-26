"""The interface an assessment backend implements, and how one is chosen.

A provider answers the criteria's per-component questions for each record;
`criteria.aggregate` turns the answers into the advisory label. Backends:

* `fake` — deterministic, for tests and dry runs. Its "answers" are a
  function of the record id, so a run on it establishes that the machinery
  works and NOTHING about classification performance;
* `databricks` (`ai_query` with a structured response format; `ai_decide`
  optional), `snowflake` (Cortex `AI_COMPLETE`), `api` (a direct model API
  for local use) — interfaces only in this delivery. Each needs credentials
  the operator supplies; semantix never stores, prints or logs them.

`render_prompt` is shared by every model-backed provider so the evidence
order is the same everywhere: the original statement and the authoritative
definition first, semantix's observations last and labelled unverified.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Protocol

from semantix.assess.criteria import Criteria

KINDS = ("fake", "api", "databricks", "snowflake")


@dataclass(frozen=True)
class ProviderConfig:
    kind: str
    model: str | None = None
    endpoint: str | None = None      # a serving endpoint / warehouse name — never a secret
    options: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"unknown provider kind {self.kind!r} (one of {', '.join(KINDS)})")


@dataclass
class Assessment:
    record_id: str
    provider: str
    model: str | None
    criteria_version: int
    answers: dict[str, str]
    label: str                         # computed by criteria.aggregate, not by the provider
    probabilities: dict[str, float] | None = None
    confidence: float | None = None
    latency_ms: float | None = None
    cost_estimate_usd: float | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class Provider(Protocol):
    name: str
    model: str | None
    measures_performance: bool         # False for `fake`: its runs measure nothing

    def assess(self, records: list[dict], criteria: Criteria) -> list[Assessment]: ...


def get_provider(config: ProviderConfig) -> Provider:
    if config.kind == "fake":
        from semantix.assess.providers.fake import FakeProvider

        return FakeProvider(seed=str(config.options.get("seed", "")))
    raise NotImplementedError(
        f"the {config.kind} provider is an interface in this delivery: it needs credentials "
        f"this environment does not have, and its implementation arrives with them")


def render_prompt(record: dict, criteria: Criteria) -> str:
    """The text a model-backed provider sends. Evidence order is fixed: the
    statement as it ran, the authoritative definition, the schemas — then,
    only if present, semantix's unverified observations, last."""
    execution = record["execution"]
    definition = record["definition"]
    parts = [
        "You compare a SQL statement that ran against the authoritative definition of a "
        "business metric. Answer each question with exactly one of its listed answers; "
        "answer `unknown` when the evidence does not decide it.",
        f"## Statement as it ran ({execution['dialect']})\n{execution['statement']}",
        "## Authoritative definition\n" + json.dumps(
            {k: definition.get(k) for k in ("name", "view", "expr", "source",
                                             "required_filters", "dimensions",
                                             "snapshot_text")}, indent=1),
        f"## Claimed metric\n{json.dumps(record['claimed_metric'])}",
        f"## Schemas of what it reads\n{json.dumps(record['schemas'])}",
    ]
    if record.get("controlled_comparison"):
        parts.append("## Controlled execution comparison (an observed discrepancy still needs "
                     "interpretation)\n" + json.dumps(record["controlled_comparison"]))
    if record.get("unverified_observations"):
        parts.append("## semantix's observations — UNVERIFIED (its SQL interpretation is not "
                     "established; weigh the evidence above first)\n"
                     + json.dumps(record["unverified_observations"]))
    questions = "\n".join(
        f"- {name}: {spec['question'].strip()} Answers: {', '.join(spec['answers'])}"
        for name, spec in criteria.components.items())
    parts.append(f"## Questions (criteria v{criteria.version})\n{questions}\n"
                 "Reply as JSON: {\"answers\": {component: answer}, "
                 "\"confidence\": number between 0 and 1}.")
    return "\n\n".join(parts)
