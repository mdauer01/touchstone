# Changelog

## 0.1.2 — 2026-11-10

Correctness fixes from the second frozen test (non-additive metrics) and an outside
review. Measured only on benchmarks already seen; see
[docs/results-complex-metrics.md](docs/results-complex-metrics.md).

- **Names.** One rule decides whether an output names a metric, for candidate selection
  and for the check on a verdict (`touchstone.naming`): the metric's words occur in the
  name, in order; `avg` counts as `average`; every other word is kept (`gross_revenue`
  does not name `net_revenue`). `avg_order_value` was not read as `average_order_value`,
  so drifts presented under it were held as could not assess.
- **What the statement presents.** Records carry `presented`: the names the final output
  exposes, and an explicit tag or asset. A metric referenced only inside (a CTE's
  MEASURE(revenue) under an outer `average_monthly_revenue`) nominates the definition
  but no longer overrides the reading that no output presents it. A final `SELECT *`
  keeps the uncertainty.
- **Full names.** The governed views' names fix how many parts a full name has, so a
  short name resolves from its row's catalog and schema with or without a column
  catalog file.
- **Applicability before ambiguity.** Under `as_of_execution` a definition that did not
  apply when the statement ran is no longer a rival: two versions with separate windows
  are not ambiguous. A statement that names a view that did not apply, or (under
  `current`) a past view, is not compared with another view.
- **Metric-view DDL.** A dollar-quoted body (`CREATE VIEW … WITH METRICS … AS $$ … $$`)
  is one statement: its semicolons, quotes and dashes no longer split it into fragments
  reported as uses not assessed. A body never closed is one statement that cannot be
  read.
- **Quoted excerpts.** A quote of several parts (separate lines, `; `, an ellipsis) is
  found when every part occurs in the text; order and adjacency are no longer required.
  Two correct answers had been rejected.
- **Criteria v7, the default for `check`**: v6 with two rules made explicit. A filter on
  the rows all parts of a ratio are computed from is a slice; changing one part only is a
  departure. Safe division (`try_divide`) is compared with what the definition says a
  zero denominator gives, not by its spelling. v6 stays available (`--criteria 6`).

## 0.1.1 — 2026-11-06

Input integrity and coverage for `touchstone check`, from an outside review of 0.1.0.
No change to the classifier: the 26 prompts of the 0.1.0 fresh test are byte for byte
the same, and replaying its sealed answers gives the same states and evidence. That
preserves the 0.1.0 result for those 26 cases; it does not measure the paths new here
(the session section of the prompt, the coverage gaps), which are tested end to end
but not by a model run.

- **The right definition.** `check` builds its own comparison records from the
  snapshots: every named measure of every listed view, including ones the parked SQL
  rules cannot represent (an `APPROX_COUNT_DISTINCT` metric produced no records).
  `MEASURE(revenue)` on a view is compared with that view's `revenue`, not the first
  view that declares one; a name two views declare, with nothing to choose between
  them, is not compared.
- **The definition that applied.** Under `as_of_execution` a run with no start time,
  or one before the definition applied, is no longer compared with it (it was, with the
  first definition listed); under `current`, past versions are left out.
- **Coverage.** Each use that cannot be compared, and each unparseable statement that
  mentions a governed view, source or metric, is listed as not assessed with the reason,
  in the report and in `findings.jsonl`. The report says what was read and left out.
- The session a statement ran in (context, earlier statements) reaches the prompt when
  one was recorded.
- `check` refuses criteria other than v6 (with `--criteria 4` it rendered zero in every
  state).
- CI runs the parked benchmark and oracle as `touchstone experimental bench|oracle`.
- `touchstone monitor` and its `--export-comparisons` are marked legacy: they choose
  definitions by the older SQL rules. `assess run` takes the `comparisons.jsonl` that
  `check` writes.
- The fresh test's sealed findings and a sample report are published in
  `src/touchstone/benchmark/v6_fresh`; [examples/minimal](examples/minimal) is a complete
  minimal input. The parked product's documentation moved to `docs/experimental.md`.

## 0.1.0 — 2026-11-06

The first release of the narrow product, advisory metric drift detection: check the SQL
that AI agents and BI tools run against your approved metric definitions.

- **`touchstone check`**: definitions (Databricks metric views) and a query-history
  export in, `report.html` out. No scan or store needed. Each distinct use of a governed
  metric gets one of three review states, with evidence: review needed (the
  definition's clause beside the statement's, or "missing from the statement"), could
  not assess (what is missing), no issue flagged. Exit 0 when the assessment completes,
  1 when model requests failed, 2 when the check cannot run.
- **Criteria v6** (the default for `check`): one question per statement with evidence.
  Quoted clauses are checked against the statement and the definition; an output read
  as the metric must be named as it. Relationship facts and dimension meanings come from
  the definition's own comments; nothing is assumed. Joins other than many-to-one are
  could not assess in this release.
- **Models**: OpenAI or compatible servers, Anthropic, or inside your platform
  (Databricks `ai_query`, Snowflake Cortex).
- **Measured**: on one frozen test of 26 queries against one metric, it identified all
  seven expected drifts, raised no false alerts, and abstained on four cases
  ([src/touchstone/benchmark/v6_fresh](src/touchstone/benchmark/v6_fresh/README.md)).
  One small test, one model; not a production rate.
- **Advisory**: no issue flagged does not certify a statement correct; a quoted clause
  found in the text shows it exists, not that the model interpreted it correctly.
- **Parked** under `touchstone experimental`: the estate scan and reconciliation, the
  bitemporal store, signed receipts and MCP, relationship mining, the equivalence
  oracle, publish, propose and diff. Kept and tested, off the main path.
- Criteria v3–v5 remain available with `--criteria` for comparison.
