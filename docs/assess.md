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
| Evidence record `touchstone-comparison/1` | `src/touchstone/assess/schema.json`, `record.py` | One record per distinct judgment of a candidate metric use, produced by `touchstone monitor --export-comparisons out.jsonl` (see "From query history to verdicts" below). It holds the original statement as it ran, the authoritative definition with its raw snapshot and sha256, the schemas the statement reads, the session statements before it, and the executions it covers. touchstone's reading follows **last**, as `unverified_observations`, when the rules read the use; it can be removed for the ablation. |
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
Execution API. It is tested against mocked API responses and has run against one real
workspace (Databricks Free Edition, serverless starter warehouse, 2026-11-04).

```bash
databricks auth login --host https://<workspace>.cloud.databricks.com --profile touchstone
export DATABRICKS_CONFIG_PROFILE=touchstone     # a profile name, not a secret
touchstone assess benchmark --provider databricks --model databricks-gpt-oss-120b \
  --endpoint <sql-warehouse-id> --batch-size 20 --split test --json report.json
```

- **Credentials.** `DATABRICKS_TOKEN` (a PAT or OAuth access token) when it is set;
  otherwise a `databricks auth login` profile (`--profile` or
  `DATABRICKS_CONFIG_PROFILE`): the host comes from the profile, and a short-lived OAuth
  token from `databricks auth token`, refreshed five minutes before it expires. No
  long-lived token has to exist.
- **What it sends.** Per record, one statement:
  `ai_query('<endpoint>', :request, responseFormat => '<json schema>', failOnError =>
  false, modelParameters => named_struct('max_tokens', N))`. With `--batch-size N`, one
  statement per batch: the prompts travel as ONE named parameter, a JSON array of
  `{id, request}`, which the statement explodes into rows for `ai_query`.
  - The prompt is a named parameter, never SQL text.
  - The response format is a JSON schema with one enum per criteria component, plus a
    self-reported confidence.
  - The label is computed from the answers by fixed code.
  - `max_tokens` defaults to 8192, because reasoning tokens count against it; 1024
    truncated measured judgments. `temperature` is sent only with `--temperature`:
    reasoning endpoints reject it, and 0 never made a hosted endpoint reproducible.
  - In a batch, a statement that fails fails its whole batch; a row the endpoint failed
    fails that record only. Latency is the batch's time divided over its records.
- **Checked against the official docs (2026-10-26).** These are asserted exactly in
  tests:
  - the request body and the parameter shape;
  - the wait-timeout range;
  - all four terminal states (SUCCEEDED, FAILED, CANCELED, CLOSED) and polling;
  - the all-strings `data_array` encoding;
  - that `json_schema` returns a JSON string.
- **Confirmed on the first live run (2026-11-04, `databricks-gpt-oss-120b`).**
  - The `failOnError` struct fields are `result` and `errorMessage`. The docs had been
    read as `response`; the live run refuted that, and the statement now selects
    `result`.
  - A parameter marker is accepted as `ai_query`'s request, directly (`:request`) and
    inside an expression (`from_json(:batch, …)`).
  - gpt-oss-120b accepts the JSON-schema response format.
  - The serverless starter warehouse runs AI Functions. Classic warehouses are
    documented as unable to.
  - Free Edition serves only open models (gpt-oss, Qwen, Llama, Gemma), not Claude.
- **Not reported.** Cost: `ai_query` returns no token usage. The JSON report has
  `cost_usd: null` and `cost_reported: false`, and the CLI prints `cost: not reported`,
  never 0. Usage is in `system.ai_gateway.usage`, which needs account and metastore
  admin.
- **Variation between runs.** Answers can vary between calls, at any temperature, and
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

## From query history to verdicts

```bash
touchstone monitor --pilot <package> --sql-dir <models> --export-comparisons uses.jsonl
touchstone assess run --provider databricks --model <endpoint> --endpoint <warehouse-id> \
  --batch-size 20 --comparisons uses.jsonl --out verdicts.jsonl --cache verdicts.cache.jsonl
```

- **Which statements are candidates.** A query that aggregates and either names a
  governed metric (a query tag or asset, `MEASURE(m)`, or an output named for it, such as
  `orders_net_revenue`) or reads a governed source or metric view. Whether the rules
  could extract a measure does not matter, and neither does how often it ran. Before
  this, the export held only recurring uses the rules had read. On the first real-history
  pilot (Free Edition, 2026-11-04) that was 47 of 161 distinct statements: none of the 24
  that compute the metric in a CTE, and 8 of the 102 that ran once. Now it is 161 of 161.
- **One record per judgment.** The key is the statement without comments and with
  whitespace collapsed outside quotes, its dialect and session evidence, and the
  definition it is compared with. Every execution sharing it is counted in `occurrences`.
  A BI tool's per-run context comment no longer makes each run a new statement. A
  changed literal is a new judgment.
- **`assess run`.** One call per record, so per distinct judgment. A cache keyed on the
  judgment, the exact prompt, the provider, the model and the criteria version reuses
  every unchanged judgment; failed calls are not cached. Verdicts are advisory: they
  never become a finding, a positive verdict or an exit code.
- **A known gap.** A statement that reads a governed source but names no metric is
  paired with every metric defined on that source. Criteria v1 has no "not a use of this
  metric" answer, so such pairs can read as drift.

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
