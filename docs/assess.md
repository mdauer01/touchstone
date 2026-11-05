# Advisory drift assessment (first delivery)

touchstone's own reading of SQL is fallible evidence. While positive verdicts are
suspended (`docs/proof-policy.md`), the monitor reports matches as experimental and
differences as suspected. This package is a separate **advisory** second opinion, built
and evaluated before anyone relies on it.

**Status.** Two real backends exist: Databricks `ai_query`, run against a real
workspace, and the Anthropic API, tested against a scripted client. Classification has
been measured only against **model-written labels** on synthetic metric uses (see "The
labels" below), so no figure here is an accuracy on a real estate. A run on the
deterministic `fake` backend measures nothing, and its report says so. Assessments order
triage only. They never change a finding, a funnel bucket or an exit code.

## What is in this delivery

| part | where | what it does |
|---|---|---|
| Evidence record `touchstone-comparison/1` | `src/touchstone/assess/schema.json`, `record.py` | One record per distinct judgment of a candidate metric use, produced by `touchstone monitor --export-comparisons out.jsonl` (see "From query history to verdicts" below). It holds the original statement as it ran, the authoritative definition with its raw snapshot and sha256, the schemas the statement reads, the session statements before it, and the executions it covers. touchstone's reading follows **last**, as `unverified_observations`, when the rules read the use; it can be removed for the ablation. |
| Criteria v1 | `assess/criteria/v1.yaml` | One narrow question per component (identity, formula, population, grain, source), each answerable `unknown`. Fixed code turns the answers into `suspected_drift`, `insufficient_evidence` or `likely_permitted`; the model never chooses the label. |
| Providers | `assess/providers/` | `assess(records, criteria) -> assessments`, a config model, `render_prompt` (the evidence order every model-backed provider uses), the deterministic `fake`, and four real backends: **Databricks** and **Snowflake** (the model runs inside your platform) and **Anthropic** and **OpenAI-compatible** APIs (see Backends below). |
| Labeled metric uses | `src/touchstone/benchmark/metric_uses/` | 60 synthetic items in 9 query families. Labels are written from the business definitions and policy in its README, never from touchstone output. They are model-authored and model-reviewed (a blind model pass, adjudicated in `adjudication.yaml`), and have not yet been reviewed by a human. The split is by family: tune on `dev`, report `test`. |
| Runner | `assess/runner.py`, `touchstone assess benchmark` | For drift: flagged, labeled permitted and undecided, always reported together. For permitted uses: confirmed, flagged as drift and undecided. Also abstention, calibration (reliability table and ECE), cost and latency, overall and per family. With `--with-observations` it adds the ablation and the touchstone-alone baseline. |

## The labels

**The labels are model-authored and model-reviewed**, and every report says so in its
`labels` block. A blind second model pass agreed with the authored labels on 56 of 60
items, and the four disagreements were decided by the written policy
(`adjudication.yaml`); a later blind pass agreed on all 6 `non_uses` items. Agreement
between two model passes is **not** independent validation, and no human has reviewed
the labels. Where labels feed a decision, the intended source of independent labels is
triage itself: metric owners confirming or dismissing flagged uses. The tooling for a
blind human review exists:

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

## Backends

Every backend sends the same prompt and answer schema, and fixed code computes the label,
so any two can be compared on the same benchmark. Every real backend needs `--model`:
touchstone does not pick a model (see "Choosing a model" below). Credentials always come
from the environment; touchstone never takes one as an argument, and never stores, prints
or logs it.

| backend | where the model runs | credential | run live? |
|---|---|---|---|
| `databricks` | `ai_query` in your workspace (above) | `DATABRICKS_TOKEN`, or a `databricks auth login` profile | yes (Free Edition, open models) |
| `snowflake` | Cortex `AI_COMPLETE` in your account | `SNOWFLAKE_TOKEN`: a programmatic access token, OAuth token or key-pair JWT | not yet |
| `anthropic` | Anthropic's API | `ANTHROPIC_API_KEY`, or an `ant auth login` profile | yes |
| `openai` | OpenAI's API, or any server that speaks it: a LiteLLM proxy, vLLM, Groq, Ollama | `OPENAI_API_KEY` | not yet |

The warehouse backends keep the statements and definitions inside your own platform and
bill to the contract you already have. On paid tiers both platforms serve frontier models
as well as open ones; Databricks Free Edition serves only open models. "Not yet" means
tested against scripted responses only.

### Snowflake

```bash
export SNOWFLAKE_ACCOUNT=myorg-myaccount      # or SNOWFLAKE_HOST=https://<account>.snowflakecomputing.com
export SNOWFLAKE_TOKEN=...                    # set it yourself; a programmatic access token
touchstone assess run --provider snowflake --model claude-sonnet-4-5 --endpoint ANALYTICS_WH \
  --comparisons uses.jsonl --out verdicts.jsonl --cache verdicts.cache.jsonl
```

- The prompt travels as a bind variable through the SQL API, never spliced into SQL. The
  response format is a SQL OBJECT constant generated from the criteria; the answer is
  checked against it here.
- `SNOWFLAKE_TOKEN_TYPE` names the token: `PROGRAMMATIC_ACCESS_TOKEN` (the default),
  `OAUTH` or `KEYPAIR_JWT`. The warehouse is `--endpoint` (or `SNOWFLAKE_WAREHOUSE`), the
  role `SNOWFLAKE_ROLE`; each defaults to the user's own. The role needs the
  `SNOWFLAKE.CORTEX_USER` database role, and some models (the newest Claude models among
  them) need cross-region inference enabled in the account.
- `AI_COMPLETE` has no per-row error mode, so one failing record fails its whole batch:
  keep `--batch-size` small.
- Built from Snowflake's documentation (checked 2026-11-04). Three details are inferred
  until the first live run: a bind variable as `AI_COMPLETE`'s prompt, the form the OBJECT
  result arrives in, and the shape of a result partition past the first.

### Anthropic

```bash
pip install 'touchstone[anthropic]'
ant auth login                       # or export ANTHROPIC_API_KEY yourself
touchstone assess run --provider anthropic --model claude-haiku-5-5 \
  --comparisons uses.jsonl --out verdicts.jsonl --cache verdicts.cache.jsonl
```

- `output_config.format` constrains the reply to the criteria's JSON schema. Thinking stays
  at the model's default; `--effort` is sent only when given; no sampling parameters are
  sent.
- On Opus 5.5, Opus 5, Fable 5.1 and Sonnet 5.5, server-side `fallbacks: "default"`
  re-runs a declined request on Anthropic's recommended fallback. A refusal that survives
  is an error on that assessment, never a label. Haiku has no server-side fallback.
- Records run concurrently (`--concurrency`, default 8). The cost estimate uses list
  prices; cache reads and batch discounts are not modelled.
- `--provider api` is the former name and still works.

### OpenAI and compatible servers

```bash
pip install 'touchstone[openai]'
touchstone assess run --provider openai --model <model> \
  --comparisons uses.jsonl --out verdicts.jsonl --cache verdicts.cache.jsonl
touchstone assess run --provider openai --base-url https://litellm.example.com/v1 --model <model> \
  --comparisons uses.jsonl --out verdicts.jsonl --cache verdicts.cache.jsonl
```

- `--base-url` (or `OPENAI_BASE_URL`) points it at any Chat Completions server: a LiteLLM
  proxy (and every provider behind it), vLLM, Groq, Ollama. It must be `https://`, or
  `http://` on this machine only. `OPENAI_API_KEY` holds that server's key.
- The response format is the criteria's JSON schema in strict mode. A server that cannot
  enforce it may still answer; an answer that violates the schema is an error, never a
  label.
- `--max-tokens`, `--temperature` and `--effort` are sent only when given: reasoning
  models reject sampling parameters, and not every compatible server accepts every field.
- A refusal, a content-filter stop or a truncated answer is an error. No cost is
  estimated: the same model is priced differently on each server this reaches.

## Choosing a model

Any model can be used; not every model judges well. Measured live on 2026-11-04 with
criteria v3. "Tuned" is the 180 uses of `metric_uses` and `metric_uses_exec`, which the v3
policy was refined against; "held out" is the 40 uses of `metric_uses_holdout`, written
afterwards (the not-a-use rule was added after a first held-out run, so even these are
not untouched).

| model | how | tuned: exact (of 180) | drift approved | permitted kept (of 76) | held out: exact (of 40) | per judgment |
|---|---|---|---|---|---|---|
| **gpt-6-luna** | chat (`--provider openai`) | **169** | **0** | 67 | **37** | ~$0.0005, ~3 s |
| gpt-6-luna | Decisions API (`--decisions`) | 136 | 3 | 39 | 28 | ~$0.0002, ~0.3 s |
| Claude Haiku 5.5 | Anthropic API | 160 | 5 | 67 | 33 | ~$0.0005 |
| gpt-oss-120b | Databricks Free Edition | 117 | 2 | 21 | 25 | workspace pricing |

The Haiku and gpt-oss-120b rows were measured before the not-a-use rule (two held-out
items it decides). Under criteria v2, gpt-6-luna and Haiku each matched 34 of the held-out
40 (35 under the corrected `hold-40`).

- These are agreement with model-written labels on synthetic uses, not accuracy on a real
  estate. Duplicates are collapsed before judging, so a warehouse pays per distinct
  statement, not per execution.
- **The Decisions API is fast and cheap but over-flags.** It answers each question
  without working through the SQL, and called half the permitted uses drift. Its
  per-answer probabilities do not separate right from wrong well enough to route review
  by threshold: sending everything below 0.5 to a person routes 81 of 220 judgments,
  of which only 36 were wrong.
- **gpt-oss-120b over-flags under v3** (21 of 76 permitted kept), so on Databricks Free
  Edition it is not a substitute for a frontier model.
- On the pilot (162 real statements), gpt-6-luna under v3 left nothing for a person and
  flagged every drift that was checked against the data: a join to `order_items` (+42%),
  a row-wise `amount - refund_amount` (−68%: 103 of 192 orders have a NULL refund) and a
  `DISTINCT` that merges identical orders.
- **Measure the model you choose before trusting it:**

```bash
touchstone assess benchmark --provider <backend> --model <model> --split all
touchstone assess benchmark --provider <backend> --model <model> --split all \
  --dataset src/touchstone/benchmark/metric_uses_exec
```

## A live test: Genie's SQL on Databricks sample data

The benchmark items above were written by models, and the first "pilot" replayed them
through a workspace, so neither is independent. This test is: SQL written by Databricks
Genie, judged against governed metric views on the Databricks sample data, through the
real path (query history export, `monitor --export-comparisons`, `assess run`).

- **Definitions.** `revenue` on `samples.tpch.lineitem` (net of discount, returned items
  excluded, with the order, customer, nation and region joins declared) and
  `fare_revenue` on `samples.nyctaxi.trips` (zero-distance trips excluded).
- **SQL author.** Two Genie spaces over the raw tables, each with a two-line glossary of
  the kind a team writes, asked ordinary business questions (totals, trends, rankings,
  slices, and questions that are not revenue at all).
- **Development data.** Genie round 1 on TPC-H (25 questions) was used to choose the
  configuration, together with the benchmark sets.
- **Test data.** 50 new questions (NYC taxi, and a second TPC-H round), asked after the
  configuration was frozen (commit df9b896), labeled before any verdict, scored once.

The configuration: gpt-6-luna through chat completions, criteria v3 with column types,
and the metric owner's approved variations in `pilot.yml` (`approved_variations`: NULL
guards on the measure's own columns, inner joins along the declared joins, qualified
output names such as `eu_revenue`).

| fresh test (scored once) | NYC taxi | TPC-H round 2 | total |
|---|---|---|---|
| drifts caught | 12 of 12 | 9 of 9 | **21 of 21** |
| drifts called permitted | 0 | 0 | **0** |
| false alarms | 1 of 8 | 3 of 12 | **4 of 20** |
| left for a person | 1 | 0 | 1 |

- The drifts it caught include the subtle ones: nine queries filtering
  `trip_distance != 0` where the definition requires `trip_distance > 0` (the Genie
  space's own glossary said "zero-distance trips are not counted"), an `AND`/`OR`
  precedence bug that let every urgent order of every year (returns included) into a
  1997 total, revenue by supplier nation where the governed nation is the customer's,
  and totals that include returned items or zero-distance trips.
- The four false alarms: two `DATE_TRUNC('DAY', …)` groupings read as a different grain,
  and one query grouped by year inside a single-year window (two records), whose label
  (permitted, as the year is constant there) is debatable.
- On Genie round 1, before the owner's approvals and column types, the same model raised
  5 false alarms on 11 permitted uses; with them, 1.
- What did not help, measured on development data: rendering the definition as SQL
  (false alarms rose), higher reasoning effort, voting on flags (`--votes`: fewer false
  alarms but more misses), and OpenAI's Decisions API (above). OpenAI's Flex tier
  (`--service-tier flex`) halves the price but refused 21 of 220 calls for capacity.
- Cost: under a tenth of a cent per distinct statement, and no extra warehouse compute:
  only the history export reads the warehouse.

The labels are one model author's, written from the definitions before the verdicts;
no person has reviewed them.

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
- **Not a use (criteria v2, the default).** A statement that reads a governed source but
  names no metric is paired with every metric on that source. The identity question can
  now answer `not_a_use` (an order count, a discount total), which code turns into the
  label `not_a_use` and no finding. That answer cannot silence a use: when the
  statement's own identity evidence names the metric (an output, alias, tag or asset),
  `not_a_use` becomes `insufficient_evidence` (`criteria.guarded`). The benchmark gained a
  `non_uses` family (5 non-uses, plus a count aliased as `net_revenue`), blind-labeled 6/6.
- **Steering.** The statement and the session statements reach the model as JSON-encoded
  data between markers whose nonce is a hash of their own text: a comment cannot end the
  data or forge a section. Before sending, `assess.screen` reads the comments and string
  literals (never identifiers) for prompt structure: headings, an answers object, a
  confidence value, classifier answer words, instructions to the reader, text addressed
  to a model. A hit is not sent; its verdict is held for a person with the reason. On the
  first real-history pilot it held 0 of 162 records. A plain "certified by the owner"
  comment is not a hit; it reaches the model as data.

## What comes next

- **Live runs of the Snowflake, Anthropic and OpenAI backends.** Each is tested against scripted responses only so far.
- **The plain-prompt baseline.** It needs a running backend.
- **Controlled execution comparisons.** These attach a `controlled_comparison`: the
  governed SQL run with the same slices, grouping, data snapshot and settings. A
  difference is recorded as an *observed discrepancy*, which still needs interpretation.

```bash
.venv/bin/touchstone assess benchmark --split test --with-observations --json report.json
.venv/bin/touchstone monitor --pilot <package> --sql-dir <models> --export-comparisons uses.jsonl
```
