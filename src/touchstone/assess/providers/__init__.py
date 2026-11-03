"""The interface an assessment backend implements, and how one is chosen.

A provider answers the criteria's per-component questions for each record;
`criteria.aggregate` turns the answers into the advisory label. Backends:

* `fake` — deterministic, for tests and dry runs. Its "answers" are a
  function of the record id, so a run on it establishes that the machinery
  works and NOTHING about classification performance;
* `databricks` — `ai_query` with a JSON-schema response format on a SQL
  warehouse (providers/databricks.py), tested against mocked API responses;
* `snowflake` (Cortex `AI_COMPLETE`) and `api` (a direct model API for local
  use) — not implemented yet. Every backend reads its credential from the
  environment; touchstone never stores, prints or logs it.

`render_prompt` is shared by every model-backed provider so the evidence
order is the same everywhere: the original statement and the authoritative
definition first, touchstone's observations last and labelled unverified.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Protocol

from touchstone.assess.criteria import Criteria

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
    # What KIND of failure `error` is (`HTTP 503`, `timeout`, `credential
    # refused`, …) — errors are reported by kind, never by request path.
    error_kind: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class Provider(Protocol):
    name: str
    model: str | None
    measures_performance: bool         # False for `fake`: its runs measure nothing

    def assess(self, records: list[dict], criteria: Criteria) -> list[Assessment]: ...


def get_provider(config: ProviderConfig) -> Provider:
    if config.kind == "fake":
        from touchstone.assess.providers.fake import FakeProvider

        return FakeProvider(seed=str(config.options.get("seed", "")))
    if config.kind == "databricks":
        from touchstone.assess.providers.databricks import DatabricksProvider

        return DatabricksProvider(config)
    raise NotImplementedError(
        f"the {config.kind} provider is not implemented yet: Databricks (ai_query) is the "
        f"one real backend in this release")


def _unknown_relation(name: str, definition: dict, session: dict) -> str:
    if name == str(definition.get("view") or "").lower():
        return (f"the governed metric view of the definition above — it reads "
                f"{definition.get('source') or 'its source'}, listed with its columns")
    if name in (session.get("unresolved") or []):
        return ("not resolved to a catalog.schema.table name (no session context fixes it) — "
                "what it reads is not known")
    return "not in the catalog — its columns are not known"


def render_prompt(record: dict, criteria: Criteria) -> str:
    """The text a model-backed provider sends. Evidence order is fixed: the
    statement as it ran, WHEN and in what session it ran (the statements
    before it can change what its names read), the authoritative definition
    with its validity window, the governing variation policy, the schemas —
    then, only if present, touchstone's unverified observations, last. Every
    field that can decide an answer is rendered: a review found two items
    whose only difference was a session statement rendered identically."""
    execution = record["execution"]
    definition = record["definition"]
    session = record.get("session_evidence") or {}
    events = session.get("events") or []
    context = session.get("context")
    policy = definition.get("variation_policy") or {}
    parts = [
        "You compare a SQL statement that ran against the authoritative definition of a "
        "business metric, under the governing variation policy given below. Answer each "
        "question with exactly one of its listed answers; answer `unknown` when the "
        "evidence does not decide it.",
        f"## Statement as it ran ({execution['dialect']})\n{execution['statement']}",
        # WHEN it ran and from what decide answers; WHO ran it and the
        # session's id do not, and are not sent (a benchmark id names its
        # family; a warehouse user is a person).
        "## Execution\n" + json.dumps(
            {k: execution.get(k) for k in ("start_time", "client", "asset")}),
        "## Session before this statement\n" + (
            ("context: " + json.dumps(context) + "\n" if context else "")
            + ("statements recorded before this one that can affect its session, in order "
               "(a note in parentheses says when its session or order is not certain):\n"
               + "\n".join(f"{i}. {e}" for i, e in enumerate(events, 1))
               if events else "no earlier statement of this session was recorded")),
        "## Authoritative definition\n" + json.dumps(
            {k: definition.get(k) for k in ("name", "view", "expr", "source",
                                             "required_filters", "dimensions", "applies",
                                             "stated_facts", "snapshot_text")}, indent=1),
        "## Governing variation policy" + (f" ({policy['source']})" if policy.get("source")
                                           else "") + "\n"
        + (policy.get("text") or "no variation policy was supplied: treat every departure "
                                 "from the definition as undecided")
        + ("\npermitted filter dimensions: " + ", ".join(policy["permitted_filter_dimensions"])
           if policy.get("permitted_filter_dimensions") else "")
        + ("\npermitted groupings: " + "; ".join(policy["permitted_groupings"])
           if policy.get("permitted_groupings") else ""),
        f"## Claimed metric\n{json.dumps(record['claimed_metric'])}",
        f"## Schemas of what it reads\n{json.dumps(record['schemas'])}"
        + ("".join(f"\n{r}: {_unknown_relation(r, definition, session)}"
                   for r, cols in record["schemas"].items() if not cols)),
    ]
    if record.get("controlled_comparison"):
        parts.append("## Controlled execution comparison (an observed discrepancy still needs "
                     "interpretation)\n" + json.dumps(record["controlled_comparison"]))
    if record.get("unverified_observations"):
        parts.append("## touchstone's observations — UNVERIFIED (its SQL interpretation is not "
                     "established; weigh the evidence above first)\n"
                     + json.dumps(record["unverified_observations"]))
    questions = "\n".join(
        f"- {name}: {spec['question'].strip()} Answers: {', '.join(spec['answers'])}"
        for name, spec in criteria.components.items())
    parts.append(f"## Questions (criteria v{criteria.version})\n{questions}\n"
                 "Reply as JSON: {\"answers\": {component: answer}, \"confidence\": c}, where "
                 "c is your probability, between 0 and 1, that EVERY answer you gave is "
                 "correct.")
    return "\n\n".join(parts)
