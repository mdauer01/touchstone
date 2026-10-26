"""Advisory drift assessment: a separate classifier, evaluated before it is trusted.

semantix's own reading of SQL is fallible evidence (positive verdicts are
suspended; negatives are suspected — semantix.conclusions). This package is
the second opinion the round-seventeen plan calls for, kept deliberately
apart from the engine:

* `record` — the `semantix-comparison/1` evidence record: the ORIGINAL
  statement and the authoritative definition first; semantix's extracted
  differences and refusal reasons only as explicitly UNVERIFIED observations,
  removable for an ablation;
* `criteria` — versioned questions, one narrow question per component
  (identity, formula, population, grain, source), each with an `unknown`
  answer, and the FIXED code that aggregates answers into an advisory label;
* `providers` — the interface a backend implements (Databricks `ai_query`,
  Snowflake `AI_COMPLETE`, a direct API) and a deterministic `fake` for
  tests. Concrete backends arrive in a later delivery;
* `runner` — evaluation against independently labeled metric uses
  (`semantix/benchmark/metric_uses`): missed drift, false alerts, abstention,
  calibration, cost and latency.

Two things this package never does: it never changes a finding's kind,
funnel bucket or exit code, and it never reports classification performance
it has not measured. A run on the `fake` provider establishes only that the
machinery works; every report says so.
"""

ADVISORY_LABELS = ("suspected_drift", "insufficient_evidence", "likely_permitted")
