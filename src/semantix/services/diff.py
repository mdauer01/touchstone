"""Scan-over-scan verdict diff (§6, the CI bot's core).

The spec's CI rule: comment only *newly introduced* verdicts, so the bot is
never the messenger for pre-existing debt. `semantix scan --fail-on` gates on
the absolute state; this module supplies the relative one — what did THIS
change break or fix — which is the number a PR review can actually act on.

Verdict identity for diffing is (concept_label, verdict_type), deliberately
coarse: a CONTRADICTION whose blame sentence shifts wording is still the same
open problem, not a new one. Severity and summaries come along as detail.
"""

from __future__ import annotations

from typing import Any

from semantix.store import Store

_BADGE = {
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
    key = lambda v: (v["concept_label"], v["verdict"])  # noqa: E731
    baseline_keys = {key(v) for v in baseline}
    current_keys = {key(v) for v in current}

    new = [v for v in current if key(v) not in baseline_keys]
    resolved = [v for v in baseline if key(v) not in current_keys]
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
        lines.append(f"### semantix: {len(new)} newly introduced verdict{s}")
        lines.append("")
        lines.append("| | Verdict | Concept | Sev | Detail |")
        lines.append("|---|---|---|---:|---|")
        for v in new:
            badge = _BADGE.get(v["verdict"], "▪️")
            summary = v["summary"].replace("|", "\\|")
            lines.append(f"| {badge} | {v['verdict']} | `{v['concept_label']}` "
                         f"| {v['severity']} | {summary} |")
    else:
        lines.append("### semantix: no new semantic verdicts ✅")
    lines.append("")

    # A verdict that DISAPPEARED is not always good news: a lost CERTIFIED or
    # ALIASING is a regression, and a lost CONTRADICTION on a scan that read
    # nothing is a typo'd path, not a fix. Only problem verdicts are
    # "resolved"; the affirmative ones are reported as lost.
    fixed = [v for v in resolved if v["verdict"] not in ("CERTIFIED", "ALIASING")]
    lost = [v for v in resolved if v["verdict"] in ("CERTIFIED", "ALIASING")]
    if fixed:
        s = "s" if len(fixed) != 1 else ""
        lines.append(f"**{len(fixed)} verdict{s} resolved by this change:** "
                     + ", ".join(f"{_BADGE.get(v['verdict'], '')} {v['verdict']} "
                                 f"`{v['concept_label']}`" for v in fixed))
        lines.append("")
    if lost:
        s = "s" if len(lost) != 1 else ""
        lines.append(f"**{len(lost)} certification{s} LOST by this change:** "
                     + ", ".join(f"🔴 {v['verdict']} `{v['concept_label']}`" for v in lost)
                     + " — the estate no longer earns these; if the scan read "
                       "fewer models than before, check the source paths first")
        lines.append("")

    lines.append(f"_{diff['unchanged']} pre-existing verdict(s) unchanged · "
                 f"scan `{diff['scan_id']}` vs baseline `{diff['baseline_scan_id']}`_")
    return "\n".join(lines)
