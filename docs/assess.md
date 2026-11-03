# Advisory drift assessment (first delivery)

touchstone's own reading of SQL is fallible evidence. While positive verdicts are
suspended (`docs/proof-policy.md`), the monitor reports matches as experimental and
differences as suspected. This package is a separate **advisory** second opinion, built
and evaluated before anyone relies on it.

**Status.** The machinery exists and is tested against a deterministic `fake` backend.
**Classification performance is unmeasured** until a real backend runs against a
workspace, and every fake-backend report says so. Assessments order triage only. They never change a finding, a funnel bucket or
an exit code.

## What is in this delivery

| part | where | what it does |
|---|---|---|
| Evidence record `touchstone-comparison/1` | `src/touchstone/assess/schema.json`, `record.py` | One record per metric use, produced by `touchstone monitor --export-comparisons out.jsonl`. It holds the original statement as it ran, the authoritative definition with its raw snapshot and sha256, the schemas the statement reads, and the session statements before it. touchstone's reading follows **last**, as `unverified_observations`, and can be removed for the ablation. |
| Criteria v1 | `assess/criteria/v1.yaml` | One narrow question per component (identity, formula, population, grain, source), each answerable `unknown`. Fixed code turns the answers into `suspected_drift`, `insufficient_evidence` or `likely_permitted`; the model never chooses the label. |
| Providers | `assess/providers/` | `assess(records, criteria) -> assessments`, a config model, `render_prompt` (the evidence order every model-backed provider uses), the deterministic `fake`, and **Databricks** (`databricks.py`, described below). |
| Labeled metric uses | `src/touchstone/benchmark/metric_uses/` | 60 synthetic items in 9 query families. Labels are written from the business definitions and policy in its README, never from touchstone output. They are model-authored and model-reviewed (a blind model pass, adjudicated in `adjudication.yaml`), and have not yet been reviewed by a human. The split is by family: tune on `dev`, report `test`. |
| Runner | `assess/runner.py`, `touchstone assess benchmark` | For drift: flagged, labeled permitted and undecided, always reported together. For permitted uses: confirmed, flagged as drift and undecided. Also abstention, calibration (reliability table and ECE), cost and latency, overall and per family. With `--with-observations` it adds the ablation and the touchstone-alone baseline. |

## Human review of the labels

**The labels are model-authored and model-reviewed**, and every report says so in its
`labels` block. A blind second model pass agreed with the authored labels on 56 of 60
items, and the four disagreements were decided by the written policy
(`adjudication.yaml`). Agreement between two model passes is **not** independent
validation. A blind human review is the next step, especially of the four adjudications
and of the grouping policy:

```bash
touchstone assess review-sheet --out sheet.csv      # writes sheet.csv and sheet.guide.md
touchstone assess compare-labels sheet.csv
```

Label the sheet from `sheet.guide.md` (the definitions, the data, the policy) and the
rows alone. **Reviewers: stop reading this page here until the sheet is filled** — the
next section reports results by family, which would tell you labels.

## First numbers (what they are, and are not)

- **touchstone's own observations alone** is the only baseline that runs today. On the
  30-item `test` split:

  | label class | result |
  |---|---|
  | drift (15) | flagged 6 · labeled permitted 0 · **undecided 9** · failed 0 |
  | permitted (12) | confirmed 9 (all experimental matches) · flagged as drift 0 · undecided 3 |
  | insufficient evidence (3) | abstained 3 |

  The 9 undecided drifts:
  - 4 joins (`dimension_joins`);
  - 2 LIMIT queries (`top_n`);
  - 2 unknown functions (`unknown_udfs`);
  - 1 week grouping that is neither declared nor approved for the metric (`time-05`).
    It used to be wrongly permitted; now it is undecided.

  "Labeled permitted 0" measures only incorrect reassurance. It does not mean the other
  15 were caught; only 6 were.
- **The fake backend's numbers measure nothing.** The report marks them so.

The dataset is synthetic and small. These figures describe this dataset, not a
workload.

**Evidence a classifier receives.** The prompt carries:
- the statement as it ran;
- when it ran (not who ran it, nor the session's id: they decide nothing, a benchmark
  id names its family, and a warehouse user is a person);
- the statements recorded before it that can affect its session, each with a note when
  its session or its order is not certain, and the catalog/schema context they set. From
  executions whose session is not recorded, only statements that can change what its
  names read are listed. Repeated SETs of one key collapse to the last; past 20, the
  statements that decide which relation a name reads (USE, CREATE, DROP, unparseable or
  unknown statements) are kept before configuration, with a count of the rest. Every
  exported session statement passes a credential filter: a SET keeps its value only for
  known evaluation settings (`spark.sql.*`, ANSI mode, time zone), and string values in
  OPTIONS / properties clauses or after IDENTIFIED BY / PASSWORD are replaced by
  `'[not exported]'`. The statement itself is sent as it ran;
- the definition with its validity window and stated facts;
- the governing variation policy, with the permitted filter dimensions and groupings;
- the schemas of the relations it reads, resolved through the session with the monitor's
  own rule: `USE CATALOG dev` makes `finance.orders` read `dev.finance.orders` (names
  compare without case); a temp view reads what its body read; a `SET` (`SET CATALOG`
  included) or an unrecognized statement leaves short names unresolved, exactly where
  the monitor calls their identity session-dependent. The governed metric view is named
  as such, beside its source table. A relation the catalog does not describe is listed
  as unknown, never dropped; a name nothing resolved to a full name is listed in
  `session_evidence.unresolved` and described as unresolved.

touchstone's observations come last. The benchmark's records and the monitor baseline share
one execution context.

## Two datasets, kept apart

- **Engine correctness**: `tests/data/engine_counterexamples.json` and the sweep
  replays. Pairs that execute differently, used to test how touchstone reads SQL. They are
  never drift labels.
- **Metric-use drift**: this dataset. Business judgments about whether a use preserves
  a governed metric.

## The Databricks backend

`--provider databricks` runs `ai_query` on a SQL warehouse through the SQL Statement
Execution API. It is implemented and tested against mocked API responses, and has **not
yet run against a real workspace**.

```bash
export DATABRICKS_HOST=https://<workspace>.cloud.databricks.com
export DATABRICKS_TOKEN=...          # a PAT or OAuth access token; read from the environment only
touchstone assess benchmark --provider databricks --model system.ai.<chat model> \
  --endpoint <sql-warehouse-id> --split test --with-observations --json report.json
```

- **What it sends.** Per record, one statement:
  `ai_query('<endpoint>', :request, responseFormat => '<json schema>', failOnError =>
  false, modelParameters => named_struct('temperature', 0.0, 'max_tokens', N))`.
  - The prompt is the named parameter `:request`, never SQL text.
  - The response format is a JSON schema with one enum per criteria component, plus a
    self-reported confidence.
  - The label is computed from the answers by fixed code.
- **Checked against the official docs (2026-10-26).** These are asserted exactly in
  tests:
  - the request body and the parameter shape;
  - the wait-timeout range;
  - all four terminal states (SUCCEEDED, FAILED, CANCELED, CLOSED) and polling;
  - the all-strings `data_array` encoding;
  - the `failOnError` struct fields `response` and `errorMessage`;
  - that `json_schema` returns a JSON string.
- **Inferred; the first live run must confirm it.**
  - A parameter marker is accepted as `ai_query`'s request argument on a SQL warehouse.
    The docs allow markers in expressions, but no page shows one in `ai_query`.
  - The chosen chat model accepts this response format. Some Claude endpoints reject
    `response_format` over REST, and the docs don't say whether `ai_query` behaves the
    same.
  - The warehouse is Pro or Serverless. AI Functions are documented as unavailable on
    Classic.
- **Not reported.** Cost: `ai_query` returns no token usage. The JSON report has
  `cost_usd: null` and `cost_reported: false`, and the CLI prints `cost: not reported`,
  never 0. Usage is in `system.ai_gateway.usage`, which needs account and metastore
  admin.
- **Variation between runs.** Answers can vary between calls even at temperature 0, and
  the hosted model can change. Record the model name with every run, and repeat runs
  before trusting a difference.
- **Errors.** An endpoint error, an answer that violates the requested schema, or a
  failed statement is an error on that assessment.
  - In the report it is **failed**, a bucket of its own beside flagged, labeled
    permitted and undecided. It is never the model's "undecided", and it is left out of
    abstention, agreement and calibration.
  - Errors are reported **by kind** (`HTTP 503`, `timeout`, `transport`, `invalid
    response`, `credential refused`, `unreachable`, `not attempted`, …), never by
    request path. An item never sent has no latency.
  - An arm in which no call answered is marked not measured (per arm), and the CLI
    exits 1.
  - A refused credential (HTTP 401/403, `UNAUTHENTICATED`, `PERMISSION_DENIED`), a
    workspace that cannot be reached (connection refused, an unresolvable host, no
    route, or a connect that times out, after the retries), or Ctrl-C stops the CALLS:
    the items already answered are kept, the rest are failed (not attempted), the report
    is printed and written, and the CLI exits by the stop's kind — 2, or 130 on an
    interrupt. An OAuth access token that expires mid-run therefore loses nothing
    already answered. A Ctrl-C while a statement is being submitted says the statement
    may still be running on the warehouse: its id is not yet known, so it cannot be
    canceled.
  - 429 and 503 are retried, honoring `Retry-After`. The statement POST is never
    resubmitted after a transport failure that may have reached the server, so
    `ai_query` cannot run twice; it is retried only when the request provably never
    left (connection refused, the host name did not resolve).
  - A statement that is abandoned for any reason once its id is known (past its
    deadline, a failed poll, an interrupt) gets one cancel request and one confirming
    read. The error says what was
    observed, for example "cancel requested; the statement is CANCELED" or "cancel
    request failed".
  - The token is sent only in the Authorization header. An error body is decoded first
    (whatever JSON it is — an escape can spell the token), then redacted, then cut: the
    whole token and any run of eight or more of its characters are removed, so a server
    that cuts its echo leaves no fragment of eight characters or more. The scan is
    linear, so a long OAuth token costs nothing noticeable; successful responses are
    never printed and are not scanned. Leading and trailing whitespace is removed from the token; a
    token with interior whitespace or non-ASCII characters is refused without being
    echoed. Redirects are never followed.
- **Calibration** is over the answered items with a confidence in [0, 1]. The report gives
  `scored`/`of`, and the source of the confidence: for Databricks, self-reported by the
  model, defined in the prompt as its probability that every answer is correct. It is a
  self-report, not a probability the model computed.

## What comes next

- **Snowflake Cortex `AI_COMPLETE`, then a direct API adapter.**
- **The plain-prompt baseline.** It needs a running backend.
- **Controlled execution comparisons.** These attach a `controlled_comparison`: the
  governed SQL run with the same slices, grouping, data snapshot and settings. A
  difference is recorded as an *observed discrepancy*, which still needs interpretation.

```bash
.venv/bin/touchstone assess benchmark --split test --with-observations --json report.json
.venv/bin/touchstone monitor --pilot <package> --sql-dir <models> --export-comparisons uses.jsonl
```
