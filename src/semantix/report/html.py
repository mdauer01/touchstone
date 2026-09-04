"""Static HTML concept-map report (§6): per-concept dossier with verdict
badges, member-by-member component diffs, and derived valid-time history.

Self-contained by construction — inline CSS, no external assets — so the
file can be opened from disk, attached to a ticket, or published as-is.
"""

from __future__ import annotations

import html
import json
from collections import Counter

from semantix.models import Concept, Verdict, VERDICT_PRECEDENCE
from semantix.store import Store

_BADGE_META = {
    "CONTRADICTION": ("#b3261e", "#fde7e5", "Same name, different math"),
    "CERTIFIED": ("#1e6b34", "#e2f4e6", "Declared and computed forms provably equivalent"),
    "ALIASING": ("#155e94", "#e3f0fa", "Same math, different names"),
    "ORPHAN_CLAIM": ("#8a5a00", "#fdf2dc", "Declared but never computed"),
    "SHADOW_METRIC": ("#5b3d8f", "#efe8fa", "Computed but never declared"),
    "UNRESOLVED": ("#5f6368", "#eceff1", "Static analysis could not decide"),
}

_COMPONENT_ROWS = ["expression", "filters", "grain", "join graph", "physical lineage"]

_LIGHT_TOKENS = """
  --bg: #f7f7f5; --card: #ffffff; --ink: #1d1f23; --muted: #6b7075;
  --line: #e3e4e6; --accent: #0f4c81; --code-bg: #f2f3f5; --diff-bg: #fdeceb;
  --diff-line: #e5b8b4; --ok: #1e6b34;
"""

_DARK_TOKENS = """
  --bg: #16181d; --card: #1f2228; --ink: #e8eaed; --muted: #9aa0a6;
  --line: #33373e; --accent: #7ab3e0; --code-bg: #272b33; --diff-bg: #3d2422;
  --diff-line: #6e3a36; --ok: #7cc78f;
"""

# Theme resolution: OS preference via the media query, overridden in both
# directions when a host page stamps data-theme on the root element.
_CSS = (
    ":root {" + _LIGHT_TOKENS + "}\n"
    "@media (prefers-color-scheme: dark) { :root {" + _DARK_TOKENS + "} }\n"
    ':root[data-theme="light"] {' + _LIGHT_TOKENS + "}\n"
    ':root[data-theme="dark"] {' + _DARK_TOKENS + "}\n"
) + """
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; padding: 32px 24px 64px; }
header h1 { font-size: 26px; margin: 0 0 4px; letter-spacing: -0.02em; }
header .sub { color: var(--muted); font-size: 14px; margin-bottom: 20px; }
.tiles { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 28px; }
.tile {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 10px 16px; min-width: 110px;
}
.tile .n { font-size: 22px; font-weight: 650; }
.tile .l { font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 20px 22px; margin-bottom: 22px;
}
.card h2 { margin: 0; font-size: 19px; display: inline-block; }
.badge {
  display: inline-block; font-size: 11px; font-weight: 700; letter-spacing: 0.06em;
  padding: 3px 9px; border-radius: 20px; margin-left: 8px; vertical-align: 2px;
}
.chip {
  display: inline-block; font-size: 11px; color: var(--muted); border: 1px solid var(--line);
  padding: 2px 8px; border-radius: 20px; margin-left: 8px; vertical-align: 2px;
}
.summary { margin: 10px 0 4px; }
.summary li { margin: 2px 0; }
.desc { color: var(--muted); font-size: 14px; margin: 6px 0 0; }
.sev { float: right; font-size: 12px; color: var(--muted); }
.tablewrap { overflow-x: auto; margin-top: 14px; }
table.members { border-collapse: collapse; width: 100%; font-size: 13px; }
table.members th, table.members td {
  border: 1px solid var(--line); padding: 8px 10px; text-align: left; vertical-align: top;
  min-width: 180px;
}
table.members th:first-child, table.members td:first-child {
  min-width: 110px; width: 110px; color: var(--muted); font-size: 12px;
  text-transform: uppercase; letter-spacing: 0.04em; background: var(--code-bg);
}
table.members thead th { background: var(--code-bg); }
.mhead .nm { font-weight: 650; font-size: 13px; text-transform: none; letter-spacing: 0; }
.mhead .kind { font-size: 10px; font-weight: 700; letter-spacing: 0.08em; }
.mhead .kind.declared { color: var(--accent); }
.mhead .kind.computed { color: var(--muted); }
.mhead .where { font-size: 11px; color: var(--muted); font-weight: 400; text-transform: none; letter-spacing: 0; }
code, .code {
  font: 12px/1.5 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  background: var(--code-bg); border-radius: 4px; padding: 1px 5px;
  white-space: pre-wrap; word-break: break-word;
}
td.diff { background: var(--diff-bg); border-color: var(--diff-line); }
td.diff code { background: transparent; padding-left: 0; }
.rowmark { font-size: 11px; margin-left: 6px; }
.rowmark.ne { color: #b3261e; font-weight: 700; }
.rowmark.eq { color: var(--ok); }
.hist { margin-top: 14px; font-size: 13px; }
.hist h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin: 0 0 6px; }
.hist li { margin: 2px 0; }
footer { color: var(--muted); font-size: 12px; margin-top: 36px; border-top: 1px solid var(--line); padding-top: 14px; }
.legend { font-size: 12px; color: var(--muted); margin-bottom: 24px; }
.legend .badge { vertical-align: 0; margin-left: 0; margin-right: 4px; }
"""


def render_report(
    concepts: list[Concept],
    verdicts: list[Verdict],
    store: Store,
    scan_meta: dict,
    embed: bool = False,
) -> str:
    by_concept: dict[str, list[Verdict]] = {}
    for v in verdicts:
        by_concept.setdefault(v.concept_id, []).append(v)

    def concept_severity(c: Concept) -> int:
        return max((v.severity for v in by_concept.get(c.concept_id, [])), default=0)

    ordered = sorted(concepts, key=lambda c: (-concept_severity(c), c.label))
    verdict_counts = Counter(v.verdict_type.value for v in verdicts)

    tiles = [
        ("artifacts scanned", scan_meta.get("artifacts", "—")),
        ("measures fingerprinted", scan_meta.get("fingerprints", "—")),
        ("concepts", len(concepts)),
        ("coverage", f"{scan_meta.get('coverage_pct', '—')}%"),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="n">{html.escape(str(n))}</div>'
        f'<div class="l">{html.escape(label)}</div></div>'
        for label, n in tiles
    )
    for vt in VERDICT_PRECEDENCE:
        count = verdict_counts.get(vt.value, 0)
        if count:
            fg, bg, _ = _BADGE_META[vt.value]
            tile_html += (
                f'<div class="tile"><div class="n" style="color:{fg}">{count}</div>'
                f'<div class="l">{vt.value.replace("_", " ").lower()}</div></div>'
            )

    legend = " ".join(
        f'<span class="badge" style="color:{fg};background:{bg}">{name}</span> {desc}.'
        for name, (fg, bg, desc) in _BADGE_META.items()
        if verdict_counts.get(name)
    )

    cards = "".join(_concept_card(c, by_concept.get(c.concept_id, []), store) for c in ordered)

    body = f"""
<div class="wrap">
  <header>
    <h1>semantix · semantic verification report</h1>
    <div class="sub">
      scan <code>{html.escape(scan_meta.get("scan_id", "?"))}</code>
      {("· " + html.escape(scan_meta["label"])) if scan_meta.get("label") else ""}
      · observed {html.escape(scan_meta.get("observed_at", "?"))}
      · dialect {html.escape(scan_meta.get("dialect", "?"))}
    </div>
    <div class="tiles">{tile_html}</div>
    <div class="legend">{legend}</div>
  </header>
  {cards}
  <footer>
    Generated by semantix v0.1.0 (Phase 0 — static reconciliation).
    Canonical-form equality is proof of sameness; canonical difference is a hypothesis
    pending the dynamic execution harness (Phase 1). Every verdict references immutable
    fingerprint ids and can be replayed from the store.
  </footer>
</div>
"""
    if embed:
        return f"<title>semantix report</title>\n<style>{_CSS}</style>\n{body}"
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>semantix report</title>\n<style>{_CSS}</style>\n</head>\n<body>{body}</body>\n</html>\n"
    )


def _concept_card(concept: Concept, verdicts: list[Verdict], store: Store) -> str:
    if not verdicts:
        return ""
    primary = min(verdicts, key=lambda v: VERDICT_PRECEDENCE.index(v.verdict_type))
    badges = ""
    for v in sorted(verdicts, key=lambda v: VERDICT_PRECEDENCE.index(v.verdict_type)):
        fg, bg, _ = _BADGE_META[v.verdict_type.value]
        badges += f'<span class="badge" style="color:{fg};background:{bg}">{v.verdict_type.value}</span>'
    declared_chip = '<span class="chip">declared</span>' if concept.declared else ""

    summaries = "".join(f"<li>{html.escape(v.summary)}</li>" for v in verdicts)
    desc = ""
    for claim in concept.claims:
        if claim.description:
            desc = f'<p class="desc">“{html.escape(claim.description)}” — {html.escape(claim.provenance.system)}</p>'
            break

    members = primary.evidence.get("members", [])
    table = _members_table(members) if members else ""
    # The declarations that BLOCKED comparison. They have no canonical core, so
    # they cannot appear in the members table — and without this a reader is
    # told the concept could not be certified and given no way to see why.
    table += _inexpressible_table(primary.evidence.get("inexpressible_claims", []))

    history = _history_html(store, concept.label)

    return f"""
<section class="card">
  <span class="sev">severity {primary.severity}</span>
  <h2>{html.escape(concept.label)}</h2>{badges}{declared_chip}
  {desc}
  <ul class="summary">{summaries}</ul>
  {table}
  {history}
</section>
"""


def _inexpressible_table(claims: list[dict]) -> str:
    if not claims:
        return ""
    rows = ""
    for c in claims:
        rows += (
            "<tr>"
            f"<td><code>{html.escape(str(c.get('name', '')))}</code></td>"
            f"<td>{html.escape(str(c.get('system', '')))}</td>"
            f"<td><code>{html.escape(str(c.get('expr_raw') or '—'))}</code></td>"
            f"<td>{html.escape(str(c.get('reason', '')))}</td>"
            "</tr>"
        )
    return (
        '<div class="tablewrap"><table class="members">'
        "<thead><tr><th>declaration not compared</th><th>system</th>"
        "<th>as written</th><th>why</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></div>"
    )


def _members_table(members: list[dict]) -> str:
    heads = ""
    for m in members:
        kind = m["kind"]
        where = m.get("where") or ""
        path = m.get("path") or ""
        heads += (
            f'<th class="mhead"><div class="kind {kind}">{kind.upper()}</div>'
            f'<div class="nm">{html.escape(m["name"])}</div>'
            f'<div class="where">{html.escape(where)}</div>'
            f'<div class="where">{html.escape(m.get("system") or "")} · '
            f'{html.escape(path)} · {html.escape(m.get("canon_level", ""))} · '
            f'<code>{html.escape(m.get("core_hash", ""))}</code></div></th>'
        )

    rows = ""
    for comp in _COMPONENT_ROWS:
        values = [m["components"].get(comp) for m in members]
        rendered = [_render_component(v) for v in values]
        all_equal = len({repr(v) for v in values}) <= 1
        mark = (
            '<span class="rowmark eq">✓</span>'
            if all_equal
            else '<span class="rowmark ne">✗ differs</span>'
        )
        cells = ""
        # Baseline for highlighting: the declared member if present, else majority.
        baseline = _baseline(values, members)
        for v, r in zip(values, rendered):
            cls = "" if all_equal or repr(v) == baseline else ' class="diff"'
            cells += f"<td{cls}><code>{r}</code></td>"
        rows += f"<tr><td>{comp}{mark}</td>{cells}</tr>"

    return (
        '<div class="tablewrap"><table class="members">'
        f'<thead><tr><th></th>{heads}</tr></thead><tbody>{rows}</tbody></table></div>'
    )


def _baseline(values: list, members: list[dict]) -> str:
    for v, m in zip(values, members):
        if m["kind"] == "declared":
            return repr(v)
    counts = Counter(repr(v) for v in values)
    return counts.most_common(1)[0][0]


def _render_component(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, list):
        if not value:
            return "—"
        return "<br>".join(html.escape(str(v)) for v in value)
    return html.escape(str(value))


def _history_html(store: Store, label: str) -> str:
    intervals = store.history(label)
    if not intervals:
        return ""
    multi = any(iv.valid_to is not None for iv in intervals)
    items = ""
    for iv in sorted(intervals, key=lambda i: (i.object_key, i.valid_from)):
        until = iv.valid_to or "now"
        changed = " — <strong>superseded</strong>" if iv.valid_to else ""
        items += (
            f"<li><code>{html.escape(iv.semantic_hash[:12])}</code> at "
            f"{html.escape(iv.relation or iv.object_key)} · valid "
            f"{html.escape(iv.valid_from)} → {html.escape(until)}{changed}</li>"
        )
    note = (
        "<li><strong>⚠ definition changed within recorded history — comparisons "
        "spanning the boundary are semantic restatements.</strong></li>"
        if multi
        else ""
    )
    return f'<div class="hist"><h3>definition history (derived valid time)</h3><ul>{items}{note}</ul></div>'


def report_json(verdicts: list[Verdict], scan_meta: dict) -> str:
    return json.dumps(
        {
            "scan": scan_meta,
            "verdicts": [
                {
                    "concept": v.concept_label,
                    "verdict": v.verdict_type.value,
                    "severity": v.severity,
                    "summary": v.summary,
                    "evidence": v.evidence,
                }
                for v in verdicts
            ],
        },
        indent=2,
    )
