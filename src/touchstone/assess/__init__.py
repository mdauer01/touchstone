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

ADVISORY_LABELS = ("suspected_drift", "insufficient_evidence", "likely_permitted")
