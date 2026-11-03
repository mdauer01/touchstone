"""Versioned criteria and the fixed aggregation of component answers.

The model answers narrow per-component questions; the advisory label is
computed HERE, by code, so a provider can never talk its way to
`likely_permitted`: one changed component is `suspected_drift`, and an
unknown anywhere (with nothing changed) is `insufficient_evidence`.
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


@dataclass(frozen=True)
class Criteria:
    version: int
    components: dict[str, dict]
    labels: dict[str, str]

    def answers(self, component: str) -> list[str]:
        return list(self.components[component]["answers"])


@lru_cache(maxsize=4)
def load(version: int = 1) -> Criteria:
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
    return Criteria(version, components, data["labels"])


def aggregate(answers: dict[str, str], criteria: Criteria) -> str:
    """The advisory label for one record's component answers. A missing or
    unrecognized answer counts as unknown — never as a match."""
    normalized = {}
    for component in criteria.components:
        answer = answers.get(component)
        normalized[component] = answer if answer in criteria.answers(component) else UNKNOWN
    if any(normalized[c] == CHANGED[c] for c in normalized):
        return "suspected_drift"
    if any(a == UNKNOWN for a in normalized.values()):
        return "insufficient_evidence"
    return "likely_permitted"
