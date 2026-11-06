# Changelog

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
