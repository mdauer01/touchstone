"""Build the blind review sheet for the v4 fresh test, and score a filled one.

    python score.py sheet  uses.jsonl  review_sheet.csv
    python score.py score  review_sheet.csv  verdicts.jsonl

The sheet carries each judgment's statement, how it names its result and where
it ran - never a model answer. A person answers three questions separately:

    what_changed  one or more of: nothing, calculation, eligibility, source_rows,
                  declared_slice, other_slice_or_grouping, separate_measure,
                  not_a_use, cannot_tell  (separated by ";")
    allowed       declared, approved, prohibited, unspecified, not_applicable,
                  cannot_tell  (rule_id: the owner rule, e.g. A4 or P1)
    alert         alert, informational, none, cannot_tell

Primary measures: alert precision (of the model's alerts, how many the person
would alert on) and missed definition changes (the person marks a change of
calculation, eligibility or source rows and alerts; the model did not).
Also: alerts on legitimate investigations, and how much stays unresolved.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter

FIELDS = ["item", "metric", "presented_as", "where_it_ran", "sql",
          "what_changed", "allowed", "rule_id", "alert", "notes"]
CORE = {"calculation", "eligibility", "source_rows"}
SLICES = {"declared_slice", "other_slice_or_grouping"}


def where_it_ran(record: dict) -> str:
    occ = record.get("occurrences") or {}
    parts = []
    for c in occ.get("contexts") or []:
        where = ", ".join(c["assets"]) or "no asset recorded"
        parts.append(f"{c['executions']}x {c.get('client') or 'unknown client'} ({where})")
    return "; ".join(parts) or "not recorded"


def build_sheet(uses_path: str, sheet_path: str) -> int:
    records = [json.loads(line) for line in open(uses_path, encoding="utf-8") if line.strip()]
    records.sort(key=lambda r: r["record_id"])
    with open(sheet_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for n, r in enumerate(records, 1):
            names = [e.get("value") for e in r["claimed_metric"]["identity_evidence"]
                     if e.get("value")]
            w.writerow({"item": f"{n:02d}:{r['record_id']}", "metric": r["definition"]["name"],
                        "presented_as": ", ".join(names) or "(no name)",
                        "where_it_ran": where_it_ran(r), "sql": r["execution"]["statement"],
                        **{k: "" for k in FIELDS[5:]}})
    return len(records)


def _human_alert(row: dict) -> str:
    return (row.get("alert") or "").strip().lower()


def score(sheet_path: str, verdicts_path: str) -> dict:
    rows = {r["item"].split(":", 1)[-1]: r for r in csv.DictReader(open(sheet_path, encoding="utf-8"))
            if (r.get("alert") or "").strip()}
    verdicts = {v["record_id"]: v for v in map(json.loads, open(verdicts_path, encoding="utf-8"))}
    scored = [(rows[k], verdicts[k]) for k in rows if k in verdicts]
    model_alerts = [(h, v) for h, v in scored if v.get("treatment") == "alert"]
    human_alerts = [(h, v) for h, v in scored if _human_alert(h) == "alert"]
    def changed(h: dict) -> set:
        return CORE & {x.strip() for x in (h.get("what_changed") or "").split(";")}

    missed = [(h, v) for h, v in human_alerts if changed(h) and v.get("treatment") != "alert"]
    investigations = [(h, v) for h, v in scored
                      if {x.strip() for x in (h.get("what_changed") or "").split(";")} <= SLICES
                      and _human_alert(h) != "alert"]
    out = {
        "labeled": len(scored),
        "alert_precision": {"model_alerts": len(model_alerts),
                            "person_would_alert": sum(_human_alert(h) == "alert"
                                                      for h, _ in model_alerts)},
        "missed_definition_changes": {"person_alerts_on_a_change": sum(1 for h, _ in human_alerts
                                                                       if changed(h)),
                                      "model_did_not_alert": len(missed),
                                      "items": [h["item"] for h, _ in missed]},
        "alerts_on_legitimate_investigations": {
            "investigations": len(investigations),
            "model_alerted": sum(v.get("treatment") == "alert" for _, v in investigations)},
        "unresolved": {"model": sum(v.get("treatment") == "unresolved" for _, v in scored),
                       "person_cannot_tell": sum(_human_alert(h) == "cannot_tell" for h, _ in scored)},
        "treatment_vs_person": dict(Counter(f"{v.get('treatment')} / {_human_alert(h)}"
                                            for h, v in scored)),
        "disagreements": [{"item": h["item"], "person": {k: h.get(k) for k in
                                                          ("what_changed", "allowed", "rule_id", "alert")},
                           "model": {"label": v.get("label"), "treatment": v.get("treatment"),
                                     "cited": v.get("cited_rules"), "answers": v.get("answers")}}
                          for h, v in scored if (v.get("treatment") == "alert") != (_human_alert(h) == "alert")],
    }
    return out


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "sheet":
        print(build_sheet(sys.argv[2], sys.argv[3]), "rows")
    elif len(sys.argv) == 4 and sys.argv[1] == "score":
        print(json.dumps(score(sys.argv[2], sys.argv[3]), indent=1))
    else:
        print(__doc__)
