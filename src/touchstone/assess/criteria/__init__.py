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
# From v4 the model records facts and cites the owner's rules; `decide`
# computes the outcome. The core components must be answered preserved for a
# variation to be anything but undetermined.
V4_COMPONENTS = ("identity", "calculation", "eligibility", "source_rows", "variation")
CORE = {"calculation": ("same", "changed"), "eligibility": ("kept", "changed"),
        "source_rows": ("same", "changed")}
ALERT, INFORMATIONAL, UNRESOLVED, NONE = "alert", "informational", "unresolved", "none"


@dataclass(frozen=True)
class Criteria:
    version: int
    components: dict[str, dict]
    labels: dict[str, str]
    # From v3: the policy every record is judged against, sent with the
    # statement and the definition only (providers.render_prompt).
    policy: str | None = None
    # From v4: the reply cites owner rules (approved_rules, prohibited_rules)
    # and each outcome has a treatment (alert, informational, unresolved, none).
    citations: bool = False
    treatment: tuple = ()

    def treatment_of(self, label: str | None) -> str | None:
        return dict(self.treatment).get(label) if label else None

    def answers(self, component: str) -> list[str]:
        return list(self.components[component]["answers"])


@lru_cache(maxsize=4)
def load(version: int = LATEST) -> Criteria:
    data = load_yaml(_DIR / f"v{version}.yaml")
    if data.get("version") != version:
        raise ValueError(f"criteria v{version}: file declares version {data.get('version')}")
    components = data["components"]
    if version >= 4:
        if tuple(components) != V4_COMPONENTS:
            raise ValueError(f"criteria v{version}: components must be {list(V4_COMPONENTS)}")
        for name, (kept, changed) in CORE.items():
            if not {kept, changed, UNKNOWN} <= set(components[name]["answers"]):
                raise ValueError(f"criteria v{version}: {name} needs {kept}, {changed}, unknown")
        if set(data.get("treatment") or {}) != set(data["labels"]):
            raise ValueError(f"criteria v{version}: every label needs a treatment")
    else:
        if set(components) != set(CHANGED):
            raise ValueError(f"criteria v{version}: components must be {sorted(CHANGED)}")
        for name, spec in components.items():
            answers = spec["answers"]
            if UNKNOWN not in answers or CHANGED[name] not in answers:
                raise ValueError(f"criteria v{version}: {name} needs `{UNKNOWN}` and "
                                 f"`{CHANGED[name]}` answers")
    return Criteria(version, components, data["labels"], data.get("policy"),
                    bool(data.get("citations")), tuple((data.get("treatment") or {}).items()))


def aggregate(answers: dict[str, str], criteria: Criteria) -> str:
    """The advisory label for one record's component answers. A missing or
    unrecognized answer counts as unknown — never as a match."""
    if criteria.version >= 4:
        return decide(answers, criteria)[0]
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


def owner_rules(record: dict | None) -> dict[str, str]:
    """The owner's rules a record carries, by the ids the prompt lists them
    under: A1… for approved variations, P1… for prohibited ones."""
    d = (record or {}).get("definition") or {}
    return {**{f"A{i}": t for i, t in enumerate(d.get("approved_variations") or [], 1)},
            **{f"P{i}": t for i, t in enumerate(d.get("prohibited_variations") or [], 1)}}


def decide(answers: dict, criteria: Criteria, record: dict | None = None) -> tuple[str, dict]:
    """Criteria v4: (outcome, cited rules) from the recorded facts. With the
    record, a cited rule counts only if the record lists it, so a model can
    never invent an approval or a prohibition; without it (a provider's
    provisional label) citations are taken at their word, and `finalize`
    decides again with the record."""
    def answer(name: str) -> str:
        a = answers.get(name)
        return a if a in criteria.answers(name) else UNKNOWN

    rules = owner_rules(record) if record is not None else None

    def valid(ids, prefix: str) -> list[str]:
        ids = [i for i in (ids or []) if isinstance(i, str) and i.startswith(prefix)]
        return [i for i in ids if rules is None or i in rules]

    approved = valid(answers.get("approved_rules"), "A")
    prohibited = valid(answers.get("prohibited_rules"), "P")
    cited = {i: (rules or {}).get(i) for i in approved + prohibited}
    identity = answer("identity")
    if identity == NOT_A_USE:
        return NOT_A_USE, {}
    if identity == "separate_measure":
        return "separate_measure", {}
    if identity != "governed":
        return "insufficient_evidence", {}
    core = {name: answer(name) for name in CORE}
    if any(core[name] == changed for name, (_, changed) in CORE.items()):
        return "suspected_definition_change", {}
    if any(a == UNKNOWN for a in core.values()):
        return "insufficient_evidence", {}
    if prohibited:
        return "prohibited_variation", {i: cited[i] for i in prohibited}
    variation = answer("variation")
    if variation in ("none", "declared"):
        return "likely_permitted", {}
    if variation == "approved":
        if approved:
            return "likely_permitted", {i: cited[i] for i in approved}
        return "unreviewed_variation", {}
    if variation == "other":
        return "unreviewed_variation", {}
    return "insufficient_evidence", {}


def finalize(label: str | None, answers: dict, record: dict,
             criteria: Criteria) -> tuple[str | None, dict]:
    """The outcome for one answered record, decided with the record: v4
    validates cited owner rules against the record, and guards an outcome
    that would let a statement escape review under the metric's own name.
    Before v4 only `guarded` applies."""
    if label is None:
        return None, {}
    if criteria.version < 4:
        return guarded(label, record), {}
    outcome, cited = decide(answers, criteria, record)
    if outcome == NOT_A_USE:
        return guarded(outcome, record), {}
    if outcome == "separate_measure" and _named_exactly(record):
        # Its own output carries the governed metric's exact name: it presents
        # itself as the metric, so it cannot be set aside as another measure.
        return "insufficient_evidence", {}
    return outcome, cited


def _named_exactly(record: dict) -> bool:
    import re

    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")

    metric = norm(str(record["definition"]["name"]))
    return any(e.get("source") != "none" and norm(str(e.get("value") or "")) == metric
               for e in record["claimed_metric"]["identity_evidence"])
