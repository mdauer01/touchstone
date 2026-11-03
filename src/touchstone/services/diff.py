"""Scan-over-scan verdict diff (§6, the CI bot's core).

The spec's CI rule: comment only *newly introduced* verdicts, so the bot is
never the messenger for pre-existing debt. `touchstone scan --fail-on` gates on
the absolute state; this module supplies the relative one — what did THIS
change break or fix — which is the number a PR review can actually act on.

Verdict identity for diffing is (concept_label, engine verdict), deliberately
coarse: a CONTRADICTION whose blame sentence shifts wording is still the same
open problem, not a new one. Severity and summaries come along as detail.
"""

from __future__ import annotations

from typing import Any

from touchstone.store import Store

_BADGE = {
    "SUSPECTED_DIVERGENCE": "🔴",
    "CONTRADICTION": "🔴",
    "UNRESOLVED": "🟠",
    "SHADOW_METRIC": "🟡",
    "ORPHAN_CLAIM": "🟡",
    "ALIASING": "🔵",
    "CERTIFIED": "🟢",
}


def diff_verdicts(store: Store, scan_id: str | None = None,
                  baseline_scan_id: str | None = None) -> dict[str, Any] | None:
    """Diff a scan's verdicts against a baseline scan in the same store.
    Defaults to the two most recent scans. Returns None when there is no
    baseline to compare against (first scan of an estate)."""
    if scan_id is None or baseline_scan_id is None:
        # Only completed scans, and skip back over any that observed the SAME
        # estate: re-running a job (a CI retry, a flaky rerun) otherwise makes
        # the baseline the PR's own previous scan, so the regression appears
        # in both, `new` is empty, and the gate goes green on a real problem.
        completed = store.completed_scans()
        if scan_id is None:
            scan_id = completed[0]["scan_id"] if completed else None
        if scan_id is None:
            return None
        if baseline_scan_id is None:
            current_digest = store.snapshot_digest(scan_id)
            same_estate = None
            for candidate in completed:
                if candidate["scan_id"] == scan_id:
                    continue
                if current_digest and store.snapshot_digest(
                    candidate["scan_id"]
                ) == current_digest:
                    same_estate = same_estate or candidate["scan_id"]
                    continue  # same estate — a re-run, not a baseline
                baseline_scan_id = candidate["scan_id"]
                break
            # Every earlier scan saw the same estate: nothing can have been
            # introduced, so diff against one of them and report cleanly
            # rather than claiming there is nothing to compare.
            baseline_scan_id = baseline_scan_id or same_estate
    if scan_id is None or baseline_scan_id is None:
        return None

    current = store.verdicts_for_scan(scan_id)
    baseline = store.verdicts_for_scan(baseline_scan_id)
    # Identity is what the ENGINE concluded, so a certification-then-break is
    # a lost experimental equality — not one UNRESOLVED "resolved" — and
    # CERTIFIED + ALIASING stay two verdicts. Rows are shown as the policy
    # reports them (touchstone.conclusions).
    from touchstone.conclusions import engine_row

    for v in (*current, *baseline):
        v["engine_verdict"] = engine_row(v)["verdict"]
    key = lambda v: (v["concept_label"], v["engine_verdict"])  # noqa: E731
    baseline_keys = {key(v) for v in baseline}
    current_keys = {key(v) for v in current}

    new = [v for v in current if key(v) not in baseline_keys]
    resolved = [v for v in baseline if key(v) not in current_keys]
    # A problem that disappeared because the experimental rules now find the
    # definitions EQUAL is not fixed — that equality is suspended as a
    # conclusion. Name what replaced it so no surface says "resolved".
    positive = ("CERTIFIED", "ALIASING")
    for v in resolved:
        if v["engine_verdict"] not in positive:
            replaced = sorted({c["engine_verdict"] for c in current
                               if c["concept_label"] == v["concept_label"]
                               and c["engine_verdict"] in positive})
            if replaced:
                v["replaced_by_experimental"] = replaced
    return {
        "scan_id": scan_id,
        "baseline_scan_id": baseline_scan_id,
        "new": sorted(new, key=lambda v: -v["severity"]),
        "resolved": sorted(resolved, key=lambda v: -v["severity"]),
        "unchanged": len(current_keys & baseline_keys),
    }


def markdown_comment(diff: dict[str, Any]) -> str:
    """PR-comment markdown: what this change introduced, what it fixed."""
    lines: list[str] = []
    new, resolved = diff["new"], diff["resolved"]

    if new:
        s = "s" if len(new) != 1 else ""
        lines.append(f"### touchstone: {len(new)} newly introduced verdict{s}")
        lines.append("")
        lines.append("| | Verdict | Concept | Sev | Detail |")
        lines.append("|---|---|---|---:|---|")
        for v in new:
            badge = _BADGE.get(v["verdict"], "▪️")
            summary = v["summary"].replace("|", "\\|")
            lines.append(f"| {badge} | {v['verdict']} | `{v['concept_label']}` "
                         f"| {v['severity']} | {summary} |")
    else:
        lines.append("### touchstone: no new semantic verdicts ✅")
    lines.append("")

    # A verdict that DISAPPEARED is not always good news: a lost CERTIFIED or
    # ALIASING is a regression, and a lost CONTRADICTION on a scan that read
    # nothing is a typo'd path, not a fix. Only problem verdicts are
    # "resolved"; the affirmative ones are reported as lost.
    #
    # Under the conclusions policy nothing is "resolved" by the rules alone:
    # a problem the experimental rules now find EQUAL is replaced by an
    # experimental equality, which is not a conclusion that it was fixed.
    positive = ("CERTIFIED", "ALIASING")
    gone = [v for v in resolved if v.get("engine_verdict", v["verdict"]) not in positive]
    replaced = [v for v in gone if v.get("replaced_by_experimental")]
    fixed = [v for v in gone if not v.get("replaced_by_experimental")]
    lost = [v for v in resolved if v.get("engine_verdict", v["verdict"]) in positive]
    if fixed:
        s = "s" if len(fixed) != 1 else ""
        lines.append(f"**{len(fixed)} verdict{s} no longer reported after this change:** "
                     + ", ".join(f"{_BADGE.get(v['verdict'], '')} {v['verdict']} "
                                 f"`{v['concept_label']}`" for v in fixed))
        lines.append("")
    if replaced:
        s = "s" if len(replaced) != 1 else ""
        lines.append(f"**{len(replaced)} verdict{s} replaced by an experimental equality "
                     f"(not a conclusion — positive verdicts are suspended; review the "
                     f"change):** "
                     + ", ".join(f"{_BADGE.get(v['verdict'], '')} {v['verdict']} "
                                 f"`{v['concept_label']}` → "
                                 f"{'/'.join(v['replaced_by_experimental'])} under the "
                                 f"experimental rules" for v in replaced))
        lines.append("")
    if lost:
        s = "s" if len(lost) != 1 else ""
        lines.append(f"**{len(lost)} experimental equalit{'ies' if s else 'y'} LOST by "
                     f"this change:** "
                     + ", ".join(f"🔴 {v.get('engine_verdict', v['verdict'])} under the "
                                 f"experimental rules `{v['concept_label']}`" for v in lost)
                     + " — the rules no longer find these equal; if the scan read "
                       "fewer models than before, check the source paths first")
        lines.append("")

    lines.append(f"_{diff['unchanged']} pre-existing verdict(s) unchanged · "
                 f"scan `{diff['scan_id']}` vs baseline `{diff['baseline_scan_id']}`_")
    return "\n".join(lines)
