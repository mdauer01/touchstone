"""Score the sealed v4 verdicts against the execution-built answer key.

    python score.py VERDICTS.jsonl   # e.g. out/v4_fresh/verdicts.SEALED.jsonl

The verdicts must match the sha256 in SEALED.md; the answer key is answer_key.jsonl
(answer_key.py), committed before the verdicts were opened.

Primary measures: alert precision (of the model's alerts, how many the answer key
alerts on) and missed alerts (the key alerts - a changed definition or a prohibited
variation - and the model did not). Also: alerts on statements the key does not alert
on, and how much the model leaves unresolved.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent


def score(verdicts_path: str) -> dict:
    sealed = re.search(r"\b[0-9a-f]{64}\b", (HERE / "SEALED.md").read_text()).group()
    if hashlib.sha256(Path(verdicts_path).read_bytes()).hexdigest() != sealed:
        raise SystemExit("the verdicts file does not match the sealed hash")
    key = {k["record_id"]: k for k in map(json.loads, open(HERE / "answer_key.jsonl"))}
    verdicts = {v["record_id"]: v for v in map(json.loads, open(verdicts_path))}
    if set(key) != set(verdicts):
        raise SystemExit("the verdicts and the answer key cover different statements")
    pairs = [(key[r], verdicts[r]) for r in sorted(key)]

    def alerts(t: dict) -> bool:
        return t.get("treatment") == "alert"

    model_alerts = [(k, v) for k, v in pairs if alerts(v)]
    key_alerts = [(k, v) for k, v in pairs if alerts(k)]
    return {
        "statements": len(pairs),
        "alert_precision": {"model_alerts": len(model_alerts),
                            "key_alerts_too": sum(alerts(k) for k, _ in model_alerts)},
        "missed_alerts": {
            outcome: {"key": sum(k["outcome"] == outcome for k, _ in key_alerts),
                      "model_did_not_alert": [k["record_id"] for k, v in key_alerts
                                              if k["outcome"] == outcome and not alerts(v)]}
            for outcome in ("suspected_definition_change", "prohibited_variation")},
        "alerts_the_key_does_not_make": {
            outcome: [k["record_id"] for k, v in model_alerts if k["outcome"] == outcome]
            for outcome in ("unreviewed_variation", "likely_permitted", "separate_measure",
                            "insufficient_evidence")},
        "unresolved": {"model": sum(v.get("treatment") == "unresolved" for _, v in pairs),
                       "key": sum(k["treatment"] == "unresolved" for k, _ in pairs)},
        "key_vs_model_treatment": dict(Counter(f"{k['treatment']} / {v.get('treatment')}"
                                               for k, v in pairs)),
        "outcome_agreement": sum(k["outcome"] == v.get("label") for k, v in pairs),
        "disagreements": [{"record_id": k["record_id"], "key": k["outcome"],
                           "key_variations": [x["kind"] for x in k["variations"]],
                           "key_execution": k["execution"], "model": v.get("label"),
                           "model_cited": v.get("cited_rules"), "model_answers": v.get("answers")}
                          for k, v in pairs if k["outcome"] != v.get("label")],
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(json.dumps(score(sys.argv[1]), indent=1))
