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
# From v6 (`mode: single`) the model answers one question with evidence;
# `decide_single` maps it to one of three review states.
SINGLE_ANSWERS = ("departs", "preserved", "undetermined")
SINGLE_LABELS = ("review_needed", "no_issue_flagged", "could_not_assess")
EVIDENCE = ("presented_as", "statement_clause", "definition_clause", "explanation")


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
    # From v6: "single" — one comparison answer plus free-text evidence
    # (`texts`: name -> what to write), no owner rules.
    mode: str = "facts"
    texts: tuple = ()

    def treatment_of(self, label: str | None) -> str | None:
        return dict(self.treatment).get(label) if label else None

    def answers(self, component: str) -> list[str]:
        return list(self.components[component]["answers"])


@lru_cache(maxsize=8)
def load(version: int = LATEST) -> Criteria:
    data = load_yaml(_DIR / f"v{version}.yaml")
    if data.get("version") != version:
        raise ValueError(f"criteria v{version}: file declares version {data.get('version')}")
    components = data["components"]
    mode = data.get("mode", "facts")
    texts = tuple((data.get("texts") or {}).items())
    if mode == "single":
        if tuple(components) != ("comparison",) \
                or tuple(components["comparison"]["answers"]) != SINGLE_ANSWERS:
            raise ValueError(f"criteria v{version}: a single assessment answers `comparison` "
                             f"with {list(SINGLE_ANSWERS)}")
        if tuple(name for name, _ in texts) != EVIDENCE:
            raise ValueError(f"criteria v{version}: texts must be {list(EVIDENCE)}")
        if tuple(data["labels"]) != SINGLE_LABELS or set(data.get("treatment") or {}) \
                != set(SINGLE_LABELS):
            raise ValueError(f"criteria v{version}: labels and treatments must be "
                             f"{list(SINGLE_LABELS)}")
    elif version >= 4:
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
                    bool(data.get("citations")), tuple((data.get("treatment") or {}).items()),
                    mode, texts)


def aggregate(answers: dict[str, str], criteria: Criteria) -> str:
    """The advisory label for one record's component answers. A missing or
    unrecognized answer counts as unknown — never as a match."""
    if criteria.mode == "single":
        return decide_single(answers, criteria)
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
    # From v5 a prohibited variation is decided first: it is the more serious
    # finding, whatever the core facts are. v4 decides it after them.
    if prohibited and criteria.version >= 5:
        return "prohibited_variation", {i: cited[i] for i in prohibited}
    core = {name: answer(name) for name in CORE}
    if any(core[name] == changed for name, (_, changed) in CORE.items()):
        return "suspected_definition_change", {}
    if any(a == UNKNOWN for a in core.values()):
        return "insufficient_evidence", {}
    if prohibited:
        return "prohibited_variation", {i: cited[i] for i in prohibited}
    variation = answer("variation")
    if variation in ("none", "declared"):
        # From v5 an owner rule can settle a core fact (an INNER JOIN it makes
        # lossless), so the outcome carries the approvals cited.
        return "likely_permitted", ({i: cited[i] for i in approved}
                                    if criteria.version >= 5 else {})
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
    if criteria.mode == "single":
        outcome = decide_single(answers, criteria, record)
        if outcome == "no_issue_flagged" and not _text(answers, "presented_as") \
                and _named_exactly(record):
            # Its own output carries the metric's exact name: "nothing presents
            # the metric" cannot set it aside.
            return "could_not_assess", {}
        return outcome, {}
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


def decide_single(answers: dict, criteria: Criteria, record: dict | None = None) -> str:
    """Criteria v6: the review state from one comparison answer and its
    evidence. A departure needs an explanation and at least one excerpt that
    occurs in the text it quotes — the definition's clause alone is enough
    when what departs is missing from the statement. Without the record (a
    provider's provisional label) excerpts are taken at their word, and
    `finalize` decides again with the record."""
    comparison = answers.get("comparison")
    if comparison not in SINGLE_ANSWERS:
        comparison = "undetermined"
    if not _text(answers, "presented_as") or comparison == "preserved":
        return "no_issue_flagged"
    if record is not None and not presented_by_name(answers, record):
        # The model reads an output as this metric that nothing names as it:
        # not dismissed, not accused — a reviewer decides.
        return "could_not_assess"
    if comparison == "departs" and _text(answers, "explanation"):
        found = excerpts_found(answers, record) if record is not None else {
            k: bool(_text(answers, k)) for k in ("statement_clause", "definition_clause")}
        if any(found.values()):
            return "review_needed"
    return "could_not_assess"


def excerpts_found(answers: dict, record: dict) -> dict[str, bool | None]:
    """Whether each quoted excerpt occurs in the text it quotes — the
    statement, or the definition's snapshot — ignoring whitespace, case and
    identifier quoting; parts around an ellipsis must occur in order. None:
    no excerpt was given."""
    import re

    def norm(text: str) -> str:
        # The prompt shows the statement JSON-encoded: a quote may carry its
        # escapes (\n, \") literally.
        text = re.sub(r"\\[nrt]", " ", text).replace('\\"', '"')
        return re.sub(r"[\s`\"]+", "", text.lower()).rstrip(";")

    definition = record.get("definition") or {}
    sources = {"statement_clause": (record.get("execution") or {}).get("statement") or "",
               "definition_clause": definition.get("snapshot_text") or " ".join(
                   [str(definition.get("expr") or ""),
                    *map(str, definition.get("required_filters") or [])])}
    out: dict[str, bool | None] = {}
    for key, source in sources.items():
        quoted = _text(answers, key)
        if not quoted:
            out[key] = None
            continue
        haystack, at = norm(source), 0
        # Parts quoted together ("…" or "; " between them) must occur in order.
        for part in filter(None, (norm(p) for p in re.split(r"\.\.\.|…|;\s", quoted))):
            at = haystack.find(part, at)
            if at < 0:
                break
            at += len(part)
        out[key] = at >= 0
    return out


def presented_by_name(answers: dict, record: dict) -> bool:
    """Whether the output the model reads as this metric is named as it: the
    metric's name occurs in the output's name (revenue, eu_revenue,
    revenue_1996), or a query tag or asset names the metric."""
    import re

    def norm(text) -> str:
        return "_" + re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_") + "_"

    metric = norm(record["definition"]["name"])
    names = [_text(answers, "presented_as")] + [
        e.get("value") for e in (record.get("claimed_metric") or {}).get("identity_evidence") or []
        if e.get("source") not in (None, "none")]
    return metric != "__" and any(metric in norm(n) for n in names if n)


def evidence(answers: dict, record: dict) -> dict:
    """Criteria v6: the evidence a verdict shows a reviewer — what the model
    wrote, whether each excerpt was found in the text it quotes, and whether
    the output it reads as the metric is named as it."""
    return {**{k: _text(answers, k) for k in EVIDENCE},
            "excerpts_found": excerpts_found(answers, record),
            "presented_by_name": presented_by_name(answers, record)
            if _text(answers, "presented_as") else None}


def _text(answers: dict, key: str) -> str:
    value = answers.get(key)
    return value.strip() if isinstance(value, str) else ""


def _named_exactly(record: dict) -> bool:
    import re

    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")

    metric = norm(str(record["definition"]["name"]))
    return any(e.get("source") != "none" and norm(str(e.get("value") or "")) == metric
               for e in record["claimed_metric"]["identity_evidence"])
