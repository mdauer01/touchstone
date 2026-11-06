"""Advisory drift assessment: a separate classifier, evaluated before it is trusted.

touchstone's own reading of SQL is fallible evidence (positive verdicts are
suspended; negatives are suspected — touchstone.conclusions). This package is
the second opinion the round-seventeen plan calls for, kept deliberately
apart from the engine:

* `record` — the `touchstone-comparison/1` evidence record: the ORIGINAL
  statement and the authoritative definition first; touchstone's extracted
  differences and refusal reasons only as explicitly UNVERIFIED observations,
  removable for an ablation;
* `criteria` — versioned questions, one narrow question per component
  (identity, formula, population, grain, source), each with an `unknown`
  answer, and the FIXED code that aggregates answers into an advisory label;
* `providers` — the interface a backend implements, a deterministic `fake`
  for tests, and Databricks (`ai_query` on a SQL warehouse); Snowflake
  `AI_COMPLETE` and a direct API come later;
* `runner` — evaluation against labeled metric uses
  (`touchstone/benchmark/metric_uses`; model-authored and model-reviewed labels,
  no human review yet): drift flagged / labeled permitted / undecided /
  failed, the same for permitted uses, abstention, calibration, cost and
  latency.

Two things this package never does: it never changes a finding's kind,
funnel bucket or exit code, and it never reports classification performance
it has not measured. A run on the `fake` provider establishes only that the
machinery works; every report says so.
"""

# `not_a_use` (criteria v2+): the statement is not a use of the governed metric.
# Criteria v1–v3 outcomes, and those v4 adds (facts first; see criteria/v4.yaml).
ADVISORY_LABELS_V3 = ("suspected_drift", "insufficient_evidence", "likely_permitted",
                      "not_a_use")
ADVISORY_LABELS_V4 = ("likely_permitted", "unreviewed_variation", "separate_measure",
                      "suspected_definition_change", "prohibited_variation",
                      "insufficient_evidence", "not_a_use")
# Criteria v6: one assessment, three review states (see criteria/v6.yaml).
ADVISORY_LABELS_V6 = ("review_needed", "no_issue_flagged", "could_not_assess")
ADVISORY_LABELS = tuple(dict.fromkeys(ADVISORY_LABELS_V3 + ADVISORY_LABELS_V4
                                      + ADVISORY_LABELS_V6))
