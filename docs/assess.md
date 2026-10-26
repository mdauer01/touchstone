# Advisory drift assessment (first delivery)

semantix's own reading of SQL is fallible evidence. While positive verdicts are
suspended (`docs/proof-policy.md`), the monitor reports matches as experimental and
differences as suspected. This package is a separate **advisory** second opinion, built
and evaluated before anyone relies on it.

**Status.** The machinery exists and is tested against a deterministic `fake` backend.
**Classification performance is unmeasured** until a real backend runs, and every report
says so. Assessments order triage only. They never change a finding, a funnel bucket or
an exit code.

## What is in this delivery

| part | where | what it does |
|---|---|---|
| Evidence record `semantix-comparison/1` | `src/semantix/assess/schema.json`, `record.py` | One record per metric use, produced by `semantix monitor --export-comparisons out.jsonl`. It holds the original statement as it ran, the authoritative definition with its raw snapshot and sha256, the schemas the statement reads, and the session statements before it. semantix's reading follows **last**, as `unverified_observations`, and can be removed for the ablation. |
| Criteria v1 | `assess/criteria/v1.yaml` | One narrow question per component (identity, formula, population, grain, source), each answerable `unknown`. Fixed code turns the answers into `suspected_drift`, `insufficient_evidence` or `likely_permitted`; the model never chooses the label. |
| Provider interface | `assess/providers/` | `assess(records, criteria) -> assessments`, a config model, the deterministic `fake`, and `render_prompt`, which keeps the evidence order for every model-backed provider. |
| Labeled metric uses | `src/semantix/benchmark/metric_uses/` | 60 synthetic items in 9 query families. Labels are written from the business definitions in its README, never from semantix output, and an independent blind pass is adjudicated in `adjudication.yaml`. The split is by family: tune on `dev`, report `test`. |
| Runner | `assess/runner.py`, `semantix assess benchmark` | Reports missed drift, false alerts, abstention, calibration (reliability table and ECE), cost and latency, overall and per family. With `--with-observations` it adds the ablation and the semantix-alone baseline. |

## First numbers (what they are, and are not)

- **The labels.** The independent blind pass agreed with the authored labels on 56 of
  60 items. All four disagreements were decided for the blind label, because the
  written policy settles each one (`adjudication.yaml`).
- **semantix's own observations alone.** This is the only baseline that runs today,
  measured on the 30-item `test` split, with drift in 15 items and permitted uses in
  12:
  - missed drift: 1 of 15. The miss was week grouping: the monitor's rules accept a
    truncation of a declared date dimension, which the dataset's policy does not;
  - false alerts: 0 of 12;
  - abstention: 14 of 30, mostly shapes outside its rule set (joins, LIMIT, windows,
    unknown functions, unshown session state).
- **The fake backend's numbers measure nothing.** The report marks them so.

The dataset is synthetic and small, and both label passes were made by models. These
figures describe this dataset, not a workload.

## Two datasets, kept apart

- **Engine correctness**: `tests/data/engine_counterexamples.json` and the sweep
  replays. Pairs that execute differently, used to test how semantix reads SQL. They are
  never drift labels.
- **Metric-use drift**: this dataset. Business judgments about whether a use preserves
  a governed metric.

## What comes next

- **Backends.** Databricks first (`ai_query` with a structured response format,
  `ai_decide` optional), then Snowflake Cortex `AI_COMPLETE`, then a direct API adapter
  for local use. Each needs credentials the operator supplies; semantix never stores,
  prints or logs them. The CLI already accepts `--provider databricks|snowflake|api` and
  says the backend is not implemented yet.
- **The plain-prompt baseline.** It needs one of those backends.
- **Controlled execution comparisons.** These attach a `controlled_comparison`: the
  governed SQL run with the same slices, grouping, data snapshot and settings. A
  difference is recorded as an *observed discrepancy*, which still needs interpretation.

```bash
.venv/bin/semantix assess benchmark --split test --with-observations --json report.json
.venv/bin/semantix monitor --pilot <package> --sql-dir <models> --export-comparisons uses.jsonl
```
