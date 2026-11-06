"""The `touchstone check` report: one self-contained HTML page a reviewer can
read without the JSONL. Review needed first, each finding with the
definition's clause beside the statement's and where the statement ran; then
what could not be assessed and why; then what was not assessed at all (a
failed call, a statement held for a person); then the uses with no issue.

Inline CSS, no external assets: open it from disk, attach it to a ticket.
"""

from __future__ import annotations

import html

_STATES = [("review_needed", "Review needed"), ("could_not_assess", "Could not assess"),
           ("no_issue_flagged", "No issue flagged")]

_TOKENS_LIGHT = ("--bg:#f7f7f5;--card:#fff;--ink:#1d1f23;--muted:#6b7075;--line:#e3e4e6;"
                 "--code:#f2f3f5;--alert:#b3261e;--alert-bg:#fde7e5;--warn:#8a5a00;"
                 "--warn-bg:#fdf2dc;--ok:#1e6b34;--ok-bg:#e6f4ea;")
_TOKENS_DARK = ("--bg:#16181d;--card:#1f2228;--ink:#e8eaed;--muted:#9aa0a6;--line:#33373e;"
                "--code:#272b33;--alert:#f2b8b5;--alert-bg:#3d2422;--warn:#f6d38a;"
                "--warn-bg:#3a2f17;--ok:#7cc78f;--ok-bg:#1d3324;")
_CSS = (
    f":root{{{_TOKENS_LIGHT}}}"
    f"@media (prefers-color-scheme: dark){{:root:not([data-theme=light]){{{_TOKENS_DARK}}}}}"
    f":root[data-theme=dark]{{{_TOKENS_DARK}}}"
    """
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:28px 16px 64px}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:18px;margin:32px 0 10px}
.sub,.note{color:var(--muted);font-size:13px}
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;
  min-width:130px}
.tile b{display:block;font-size:22px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;
  margin:10px 0}
.badge{display:inline-block;font-size:12px;font-weight:600;border-radius:4px;padding:1px 7px;
  margin-right:6px}
.review_needed{color:var(--alert);background:var(--alert-bg)}
.could_not_assess,.not_assessed{color:var(--warn);background:var(--warn-bg)}
.no_issue_flagged{color:var(--ok);background:var(--ok-bg)}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0}
@media (max-width:640px){.pair{grid-template-columns:1fr}}
.pair div{background:var(--code);border-radius:6px;padding:8px 10px;overflow-x:auto}
.pair span{display:block;font-size:12px;color:var(--muted);margin-bottom:2px}
.pair code{white-space:pre-wrap}
.tile{flex:1 1 120px}
code,pre{font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
pre{background:var(--code);border-radius:6px;padding:10px;overflow-x:auto;white-space:pre-wrap}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{text-align:left;border-bottom:1px solid var(--line);padding:6px 4px;vertical-align:top}
details summary{cursor:pointer;color:var(--muted);font-size:13px}
""")


def _e(value) -> str:
    return html.escape("" if value is None else str(value))


def _state(v: dict) -> str:
    return v.get("label") or "not_assessed"


def _where(v: dict) -> str:
    rows = []
    for c in v.get("contexts") or []:
        assets = ", ".join(c.get("assets") or []) or "unattributed"
        rows.append(f"<tr><td>{_e(c.get('client'))}</td><td>{_e(assets)}</td>"
                    f"<td>{_e(c.get('executions'))}</td><td>{_e(c.get('first_start_time'))}"
                    f" &ndash; {_e(c.get('last_start_time'))}</td></tr>")
    if not rows:
        return ""
    return ("<table><tr><th>Where it ran</th><th>Assets</th><th>Runs</th><th>When</th></tr>"
            + "".join(rows) + "</table>")


def _excerpt(text: str, found, missing: str) -> str:
    if not text:
        return f"<em>{_e(missing)}</em>"
    mark = "" if found is not False else " <em>(not found verbatim in the text)</em>"
    return f"<code>{_e(text)}</code>{mark}"


def _finding(v: dict) -> str:
    ev = v.get("evidence") or {}
    found = ev.get("excerpts_found") or {}
    state = _state(v)
    title = {"review_needed": "Review needed", "could_not_assess": "Could not assess",
             "not_assessed": "Not assessed"}.get(state, state)
    head = (f"<span class='badge {state}'>{title}</span>"
            f"<b>{_e(v.get('metric') or 'a statement')}</b>"
            + (f" as <code>{_e(ev.get('presented_as'))}</code>" if ev.get("presented_as") else "")
            + f" <span class='note'>{_e(v.get('view'))} &middot; {_e(v.get('executions'))} "
              f"execution(s)</span>")
    if state == "not_assessed":
        body = f"<p>{_e(v.get('error'))}</p>"
    else:
        body = f"<p>{_e(ev.get('explanation') or 'No explanation given.')}</p>"
        if ev.get("presented_by_name") is False:
            body += ("<p class='note'>The model reads this output as the metric, but neither "
                     "its name nor a tag or asset names the metric.</p>")
        if ev.get("definition_clause") or ev.get("statement_clause"):
            body += ("<div class='pair'><div><span>Definition</span>"
                     + _excerpt(ev.get("definition_clause"), found.get("definition_clause"),
                                "not cited")
                     + "</div><div><span>Statement</span>"
                     + _excerpt(ev.get("statement_clause"), found.get("statement_clause"),
                                "missing from the statement")
                     + "</div></div>")
    return (f"<div class='card'>{head}{body}{_where(v)}"
            f"<details><summary>SQL</summary><pre>{_e(v.get('statement'))}</pre></details>"
            "</div>")


def _quiet(v: dict) -> str:
    ev = v.get("evidence") or {}
    presented = (f"<code>{_e(ev['presented_as'])}</code>" if ev.get("presented_as")
                 else "<em>not presented as the metric</em>")
    return (f"<tr><td>{_e(v.get('metric'))}</td><td>{presented}</td>"
            f"<td>{_e(ev.get('explanation'))}</td><td>{_e(v.get('executions'))}</td>"
            f"<td><details><summary>SQL</summary><pre>{_e(v.get('statement'))}</pre>"
            "</details></td></tr>")


def _coverage(history: dict) -> str:
    """What was read and what was left out, so an empty queue can be told
    apart from missing coverage."""
    queries = history.get("queries", 0)
    parts = [f"Read {history.get('statements', 0)} statement(s) from "
             f"{history.get('rows', 0)} history row(s): {queries} "
             f"{'query' if queries == 1 else 'queries'}, "
             f"{history.get('candidate_statements', 0)} of them computing or naming a "
             f"governed metric, against {history.get('definitions', 0)} definition(s)."]
    if history.get("unparseable"):
        parts.append(f"{history['unparseable']} could not be parsed; "
                     f"{history.get('unparseable_unrelated', 0)} of them mention no governed "
                     f"view, source or metric, and the rest are listed as not assessed.")
    if history.get("executions_outside_window"):
        parts.append(f"{history['executions_outside_window']} execution(s) fell outside the "
                     f"package's window and were not read.")
    return " ".join(parts)


def render_check(result: dict) -> str:
    verdicts = result["verdicts"]
    by = {s: [v for v in verdicts if _state(v) == s]
          for s in [s for s, _ in _STATES] + ["not_assessed"]}
    history = result.get("history") or {}
    tiles = [("Statements in history", history.get("rows")),
             ("Metric uses assessed", result.get("uses")),
             *((title, len(by[s])) for s, title in _STATES),
             ("Not assessed", len(by["not_assessed"]))]
    out = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>Touchstone check</title><style>{_CSS}</style></head><body><div class='wrap'>",
        "<h1>Touchstone check</h1>",
        f"<div class='sub'>{_e(result.get('pilot'))} &middot; {_e(result.get('comparison'))}"
        f" &middot; {_e(result.get('provider'))} {_e(result.get('model') or '')} &middot; "
        f"criteria v{_e(result.get('criteria_version'))}</div>",
        "<p class='note'>Advisory: a model's reading of each statement against the approved "
        "definition, with the clauses it relies on. It orders review; it is not a verdict "
        "that any number is right or wrong. No issue flagged does not certify a statement "
        "correct, and a quoted clause found in the text shows it exists, not that the "
        "model interpreted it correctly.</p>",
    ]
    if not result.get("measures_performance", True):
        out.append("<p class='note'><b>Fake provider:</b> these answers are a hash of the "
                   "record id. The report shows the machinery works and nothing about "
                   "any statement.</p>")
    out.append("<div class='tiles'>" + "".join(
        f"<div class='tile'><b>{_e(n)}</b>{_e(t)}</div>" for t, n in tiles) + "</div>")
    out.append(f"<p class='note'>{_e(_coverage(history))}</p>")
    for state, title in [("review_needed", "Review needed"),
                         ("could_not_assess", "Could not assess"),
                         ("not_assessed", "Not assessed")]:
        if by[state]:
            out.append(f"<h2>{title} ({len(by[state])})</h2>")
            out += [_finding(v) for v in by[state]]
    if by["no_issue_flagged"]:
        out.append(f"<h2>No issue flagged ({len(by['no_issue_flagged'])})</h2>"
                   "<table><tr><th>Metric</th><th>Presented as</th><th>Reading</th>"
                   "<th>Runs</th><th></th></tr>"
                   + "".join(_quiet(v) for v in by["no_issue_flagged"]) + "</table>")
    out.append("</div></body></html>")
    return "\n".join(out)
