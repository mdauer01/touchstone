"""A deterministic screen for steering text, run before a record is sent.

The statement a classifier judges is written by the people the monitor
watches. The prompt quotes it as JSON data between hash-nonced markers
(providers.render_prompt), but a model can still be steered by what the data
SAYS: a comment shaped like a prompt section, a pre-filled answer, an
instruction to "ignore the policy". Such a record is not sent; it is held
for a person, with the reason. The screen reads only comments and string
literals — where prose lives — never identifiers, so a column called
`confidence` is not a hit.
"""

from __future__ import annotations

import re

_MARKERS = (
    (re.compile(r"(?m)^\s*#{1,6}\s+\S"), "a markdown heading"),
    (re.compile(r"\"answers\"\s*:"), "an answers object"),
    (re.compile(r"\bconfidence\b[\"']?\s*[:=]\s*[0-9.]", re.I), "a confidence value"),
    (re.compile(r"\b(likely_permitted|suspected_drift|insufficient_evidence|not_a_use|"
                r"same_metric|other_metric|permitted_variation)\b", re.I),
     "a classifier answer"),
    (re.compile(r"\b(ignore|disregard|override)\b[^\n]{0,60}\b(instruction|previous|above|"
                r"policy|rules?|definition)", re.I), "an instruction to the reader"),
    (re.compile(r"\b(you are|as an ai|language model|system prompt|the assistant)\b", re.I),
     "text addressed to a model"),
)


def prose(sql: str) -> list[str]:
    """The comments and string literals of a statement, in order."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"":
            j, buf = i + 1, []
            while j < n:
                if sql[j] == "\\" and j + 1 < n:
                    buf.append(sql[j:j + 2])
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:     # a doubled quote
                        buf.append(c)
                        j += 2
                        continue
                    break
                buf.append(sql[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
        elif c == "`":
            j = sql.find("`", i + 1)
            i = n if j < 0 else j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            out.append(sql[i + 2:n if j < 0 else j])
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            out.append(sql[i + 2:n if j < 0 else j])
            i = n if j < 0 else j + 2
        else:
            i += 1
    return out


def steering(record: dict) -> str | None:
    """Why this record must not be sent automatically, or None."""
    texts = prose(record["execution"]["statement"])
    for event in (record.get("session_evidence") or {}).get("events") or []:
        texts += prose(str(event))
    for text in texts:
        for pattern, what in _MARKERS:
            if pattern.search(text):
                return f"its comments or strings contain {what}"
    return None
