"""Versioned criteria and the fixed aggregation of component answers.

The model answers narrow per-component questions; the advisory label is
computed HERE, by code, so a provider can never talk its way to
`likely_permitted`: one changed component is `suspected_drift`, and an
unknown anywhere (with nothing changed) is `insufficient_evidence`. From v2
the identity question can answer `not_a_use` — and `guarded` refuses that
answer for a statement that itself names the governed metric, so it can
never be used to dismiss a use.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from touchstone.adapters.loadutil import load_yaml

_DIR = Path(__file__).parent
# The answer in each component that means "differs from the definition".
CHANGED = {"identity": "other_metric", "formula": "changed", "population": "changed",
           "grain": "changed", "source": "different"}
UNKNOWN = "unknown"
NOT_A_USE = "not_a_use"
LATEST = 3


@dataclass(frozen=True)
class Criteria:
    version: int
    components: dict[str, dict]
    labels: dict[str, str]
    # From v3: the policy every record is judged against, sent with the
    # statement and the definition only (providers.render_prompt).
    policy: str | None = None

    def answers(self, component: str) -> list[str]:
        return list(self.components[component]["answers"])


@lru_cache(maxsize=4)
def load(version: int = LATEST) -> Criteria:
    data = load_yaml(_DIR / f"v{version}.yaml")
    if data.get("version") != version:
        raise ValueError(f"criteria v{version}: file declares version {data.get('version')}")
    components = data["components"]
    if set(components) != set(CHANGED):
        raise ValueError(f"criteria v{version}: components must be {sorted(CHANGED)}")
    for name, spec in components.items():
        answers = spec["answers"]
        if UNKNOWN not in answers or CHANGED[name] not in answers:
            raise ValueError(f"criteria v{version}: {name} needs `{UNKNOWN}` and "
                             f"`{CHANGED[name]}` answers")
    return Criteria(version, components, data["labels"], data.get("policy"))


def aggregate(answers: dict[str, str], criteria: Criteria) -> str:
    """The advisory label for one record's component answers. A missing or
    unrecognized answer counts as unknown — never as a match."""
    normalized = {}
    for component in criteria.components:
        answer = answers.get(component)
        normalized[component] = answer if answer in criteria.answers(component) else UNKNOWN
    if normalized["identity"] == NOT_A_USE:
        return NOT_A_USE
    if any(normalized[c] == CHANGED[c] for c in normalized):
        return "suspected_drift"
    if any(a == UNKNOWN for a in normalized.values()):
        return "insufficient_evidence"
    return "likely_permitted"


def guarded(label: str, record: dict) -> str:
    """`not_a_use` for a statement whose OWN identity evidence names the
    governed metric (an output, alias, tag or asset) is a contradiction, not
    a dismissal: the statement says it is this metric. It becomes
    insufficient_evidence, so "not a use" can never silence a use — the
    cheapest suppression of a drift verdict would otherwise be one word."""
    if label != NOT_A_USE:
        return label
    import re

    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")

    metric = norm(str(record["definition"]["name"]))
    for evidence in record["claimed_metric"]["identity_evidence"]:
        value = norm(str(evidence.get("value") or ""))
        if evidence.get("source") != "none" and value and (
                value == metric or value.startswith(metric + "_")
                or value.endswith("_" + metric) or f"_{metric}_" in value):
            return "insufficient_evidence"
    return label
