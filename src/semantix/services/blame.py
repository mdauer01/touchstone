"""Semantic blame (§5, sharpened): name the exact clause that diverges.

"Diverges on expression, filters" tells a reader *that* two definitions
disagree; the sentence people forward names *what*: the missing
`status = 'completed'` filter, the dropped `refund_amount` term, the staging
table read where the mart should be. Every ingredient is already canonical —
filters are sorted conjunct strings, measures are canonical expressions,
lineage is physical terminals — so the minimal diff is set arithmetic plus an
identifier-level look inside the measure expression, not new inference.

Baseline choice mirrors reconciliation's authority order: the declared
(expressible) definition when one exists, else the majority canonical core.
Blame is *descriptive*, not a verdict — it decorates CONTRADICTION evidence;
the verdict itself is still decided by canonical-hash comparison alone.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ALIAS_RE = re.compile(r"\bt\d+\.")

# Tokens that are SQL machinery, not data references.
_NOISE = frozenset("""
    sum avg min max count distinct case when then else end and or not in is null
    like between cast as coalesce nullif abs round floor ceil date_trunc extract
    interval true false left right inner outer join on from where group by
""".split())


def _refs(expression: str) -> set[str]:
    """Column-ish identifiers referenced by a canonical expression."""
    return {tok.lower() for tok in _IDENT_RE.findall(expression)
            if tok.lower() not in _NOISE and not re.fullmatch(r"t\d+", tok.lower())}


def _readable(clause: str) -> str:
    """Strip canonical table aliases and identifier quoting for human
    sentences; evidence keeps the raw canonical clause."""
    return _ALIAS_RE.sub("", clause).replace('"', "")


def _pick_baseline(members: list[dict]) -> dict | None:
    declared = [m for m in members if m["kind"] == "declared"]
    if declared:
        return declared[0]
    if not members:
        return None
    majority_hash, _ = Counter(m["core_hash"] for m in members).most_common(1)[0]
    return next(m for m in members if m["core_hash"] == majority_hash)


def _set_delta(baseline: list, actual: list) -> dict[str, list]:
    b, a = set(baseline), set(actual)
    return {"missing": sorted(b - a), "extra": sorted(a - b)}


def blame_site(member: dict) -> str:
    """Where a reader should go to FIX this member.

    For computed members that is the relation. For a declared one it is the
    ontology entry — never the model the claim reads, which is working code
    that would be corrected for a fault in someone's YAML.
    """
    if member.get("kind") == "declared":
        source = member.get("system") or member.get("path") or "the ontology"
        return f"the {source} declaration of {member.get('name')}"
    return member["where"]


def blame_members(members: list[dict]) -> dict[str, Any]:
    """Per-divergent-member minimal diffs against the baseline definition.
    Members are the reconciliation evidence rows (each carries components())."""
    baseline = _pick_baseline(members)
    if baseline is None:
        return {"baseline": None, "divergent": []}
    base = baseline["components"]

    divergent = []
    for m in members:
        if m["core_hash"] == baseline["core_hash"] or m is baseline:
            continue
        comp = m["components"]
        delta: dict[str, Any] = {
            "member_id": m["member_id"],
            "where": blame_site(m),
            "kind": m["kind"],
            "filters": _set_delta(base["filters"], comp["filters"]),
            "grain": _set_delta(base["grain"], comp["grain"]),
            "sources": _set_delta(base["join graph"], comp["join graph"]),
            "lineage": _set_delta(base["physical lineage"], comp["physical lineage"]),
        }
        if base["expression"] != comp["expression"]:
            b_refs, a_refs = _refs(base["expression"]), _refs(comp["expression"])
            delta["expression"] = {
                "baseline": base["expression"],
                "actual": comp["expression"],
                "missing_refs": sorted(b_refs - a_refs),
                "extra_refs": sorted(a_refs - b_refs),
            }
        delta["sentence"] = _sentence(delta)
        divergent.append(delta)

    return {
        "baseline": {"member_id": baseline["member_id"], "where": blame_site(baseline),
                     "kind": baseline["kind"]},
        "divergent": divergent,
    }


def _sentence(delta: dict[str, Any]) -> str:
    """One human-forwardable clause list: what this member does differently."""
    parts: list[str] = []
    f = delta["filters"]
    for clause in f["missing"]:
        parts.append(f"missing filter {_readable(clause)}")
    for clause in f["extra"]:
        parts.append(f"adds filter {_readable(clause)}")

    expr = delta.get("expression")
    if expr:
        if expr["missing_refs"]:
            parts.append("expression omits " + ", ".join(expr["missing_refs"]))
        if expr["extra_refs"]:
            parts.append("expression adds " + ", ".join(expr["extra_refs"]))
        if not expr["missing_refs"] and not expr["extra_refs"]:
            actual, baseline = _readable(expr["actual"]), _readable(expr["baseline"])
            if actual != baseline:
                parts.append(f"computes {actual} instead of {baseline}")
            else:
                # Same identifiers, and identical once table aliases and
                # quoting are stripped — so the raw canonical expressions
                # differ only in what those names BIND to (`t1.amount` over a
                # different relation). Rendering that as "computes SUM(amount)
                # instead of SUM(amount)" reads as a rendering bug and tells
                # the reader nothing; show the qualified forms that differ.
                parts.append(f"computes {actual} over different inputs "
                             f"({expr['baseline']} vs {expr['actual']})")

    s = delta["sources"]
    if s["extra"] and s["missing"]:
        parts.append(f"reads {', '.join(s['extra'])} instead of {', '.join(s['missing'])}")
    elif s["extra"]:
        parts.append(f"also reads {', '.join(s['extra'])}")
    elif s["missing"]:
        parts.append(f"does not read {', '.join(s['missing'])}")

    g = delta["grain"]
    if g["missing"] or g["extra"]:
        parts.append("different grain "
                     f"({_readable(', '.join(g['extra']) or '(none)')} "
                     f"vs {_readable(', '.join(g['missing']) or '(none)')})")

    if not parts:
        lin = delta["lineage"]
        if lin["missing"] or lin["extra"]:
            bits = []
            if lin["missing"]:
                bits.append(f"drops {', '.join(lin['missing'])}")
            if lin["extra"]:
                bits.append(f"adds {', '.join(lin['extra'])}")
            parts.append("physical lineage " + "; ".join(bits))

    return "; ".join(parts) or "differs below clause granularity (see canonical SQL)"
