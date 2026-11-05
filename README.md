# touchstone

touchstone checks that the SQL people actually run computes your business metrics the
way they are defined. It reads governed definitions and the SQL that computes or uses
them, and reports where the two appear to disagree, clause by clause: a dropped filter,
a changed formula, a different source table, a grouping the definition does not allow.

- **Scan** compares computed definitions (dbt projects, SQL directories, view DDL) with
  declared ones (metrics YAML, the dbt Semantic Layer, Unity Catalog metric views,
  Apache Ossie, Agents Schema): suspected divergences, aliases, orphan and shadow metrics,
  each with clause-level blame.
- **Monitor** reads warehouse query history and finds uses of governed metrics that
  depart from their definition, including ad-hoc queries and long BI or notebook SQL.
- **Advisory assessment** can have a model (Databricks `ai_query` or the Anthropic API)
  judge every candidate metric use, once per distinct query, cached. It orders triage;
  it never changes a finding or an exit code.
- **Receipts and agents.** Signed receipts, an MCP server agents can check before quoting
  a number, a dbt package and a scan-over-scan diff for CI.

Status: a research preview. touchstone reports *suspected* differences and
*experimental* matches, not verdicts to rely on: see [Status](#status).

- Concept: [docs/concept-one-pager.md](docs/concept-one-pager.md)
- Architecture: [docs/architecture-spec.md](docs/architecture-spec.md)
- License: [Apache-2.0](LICENSE)

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                      # full suite

.venv/bin/touchstone scan \
  --dbt examples/acme/dbt_project \
  --metrics examples/acme/metrics.yml \
  --catalog examples/acme/catalog.yml \
  --execute examples/acme/seed.sql \
  --db out/touchstone.db \
  --report out/acme-report.html \
  --label "acme demo estate"

open out/acme-report.html
```

(`--execute` is optional; estates without dbt scan via `--sql-dir <dir>` — one SELECT
per `.sql` file, `-- touchstone: relation=…` header to override naming — and/or
`--views dump.sql` for a file of `CREATE VIEW` statements.)

The demo estate is seven compiled dbt models plus a three-metric declared ontology,
engineered so one scan produces every verdict type:

| Concept | Verdict | Why |
| --- | --- | --- |
| `net_revenue` | **SUSPECTED_DIVERGENCE** (sev 94) | Finance's model appears to match the declared definition; the engine's own blame sentence (unverified observations): *"growth_net_revenue diverges: missing filter status = 'completed'; adds filter status IN ('completed', 'pending'); expression omits refund_amount; reads analytics.stg_orders instead of analytics.fct_orders."* With `--execute`, an observed discrepancy on seed data: totals 114,000 vs baseline 84,000 (+35.7%). |
| `gmv` | **UNRESOLVED** — experimental comparison | Under the experimental rules, `total_gmv` and `gross_merchandise_value` are equal to the declared metric (the engine would have said CERTIFIED + ALIASING) — CTE vs. inline, flipped predicate order, `NOT =` vs. `<>` and `SELECT *`. Not a conclusion while positive verdicts are suspended. |
| `customer_ltv` | **ORPHAN_CLAIM** | Declared, never computed anywhere. |
| `refund_rate` | **SHADOW_METRIC** | Computed in ops, absent from the ontology. |

## Status

> **Positive verdicts are suspended.** Nine successive review rounds found new false
> certifications, the last ones in how SQL is *parsed*: the parser binds `%` and `IS`
> differently from DuckDB and Spark, and a guard operating on a misparsed tree may
> never see the dangerous construct. So, until the re-enable criteria in
> [docs/proof-policy.md](docs/proof-policy.md) are met:
>
> - an equality the engine finds is reported as an **experimental comparison**,
>   never as CERTIFIED / ALIASING / conforming;
> - a difference that rests on the same interpretation is a **suspected divergence**
>   or **suspected difference**, never a definitive contradiction or violation;
> - **no receipt authorizes reliance.** Verification reports three separate answers —
>   signature authentic, estate unchanged, claim usable — and the claim is never
>   usable now, however authentic the receipt and however unchanged the estate.
>
> Stores and receipts written earlier are read under the same policy; tables
> published earlier are brought under it with `touchstone publish --refresh-policy`.

## Supported scope

**What it reads.** SQL in four dialects: DuckDB, Databricks, Snowflake and BigQuery.
Computed definitions come from dbt manifests, directories of `.sql` files, or
`CREATE VIEW` dumps; declared definitions from the formats listed above. The only
shipped warehouse driver is DuckDB, so other warehouses are read through exported
files.

**What an experimental equality means.** Both definitions are inside the proof
grammar of policy [`touchstone-proof/2`](docs/proof-policy.md), and their proof forms
are equal. A proof form comes from a short list of stated transformations (CTE
inlining, `*` expansion, conjunct and operand ordering, `BETWEEN`, parentheses …),
never from a general optimizer. That is what the engine checks; it is **not reported
as a conclusion** while positive verdicts are suspended — reviews showed the parse the
rules operate on can differ from the engine's. See
[What an experimental equality does not establish](#what-an-experimental-equality-does-not-establish).

**What it refuses rather than guesses.** Everything outside the grammar is
unverifiable, and the reason names the construct:

- joins, set operations, `LIMIT` / `OFFSET`, `DISTINCT` and window functions;
- subqueries in expressions, `ROLLUP` / `CUBE` / `GROUPING SETS`;
- aggregating CTE bodies;
- unknown functions, `TABLESAMPLE` and bind parameters;
- a relation the catalog does not describe under the name the SQL resolves to, or
  an unqualified reference whose database depends on the session.

The general optimizer's reading of such SQL is still used for diffs, blame and
candidate matching, but only as a hypothesis. It never certifies, aliases or conforms,
and nothing derived from it is stated as a definitive difference.

**What it does not cover.** Semantics the SQL text does not carry (session time
zones, collations, user-defined functions), engine runtime behaviour beyond the
DuckDB execution oracle, calculations done client-side that never reach the
warehouse, and definitions the scan did not read.

**How the claims are tested.**

| Check | What it does |
| --- | --- |
| `pytest` | The suite, including one regression file per external review round |
| `touchstone bench` | 32 canonicalization pairs in four dialects; near-misses must never hash-equal; pairs outside the proof grammar are listed as refused, with reasons |
| `touchstone oracle` | Executes every pair the engine would certify equal, and every proof-grade query against its own proof form, on adversarial DuckDB data, compared exactly. It then executes every query the monitor's rules call conforming against its definition plus the identified variation |

## What is implemented

This repository implements **Phase 0** of the architecture spec (§7): adapters (dbt,
raw SQL directories, view-DDL dumps) → extraction & canonicalization → bitemporal
fingerprint store → concept clustering → static reconciliation with **clause-level
blame** → HTML report — plus the Phase 1/2 surfaces that were cheap to pull forward:
derived valid-time history, as-of queries, restatement detection, a **DuckDB execution
slice** that attaches measured dollar divergence to contradictions, **Ed25519-signed
semantic receipts** (`attest` / `verify-receipt`), a scan-over-scan **`diff`** for CI
(comment only newly introduced verdicts), an **attestation MCP server** so agents check
the receipt before quoting a number, and an **open equivalence benchmark** (`bench`).

### The bitemporal store in action

```bash
# change a model's SQL, rescan into the same db, then:
.venv/bin/touchstone history net_revenue --db out/touchstone.db   # valid-time intervals per site
.venv/bin/touchstone asof net_revenue --at <timestamp after your first scan> --db out/touchstone.db
.venv/bin/touchstone attest net_revenue --db out/touchstone.db    # semantic receipt (JSON)
```

`history` flags definition changes as restatement boundaries; `asof` answers "what did
this metric *mean* on this date"; `attest` issues the §6 receipt — status, immutable
definition versions, physical lineage, known divergences — **Ed25519-signed over its
canonical payload**, so it verifies offline:

```bash
.venv/bin/touchstone attest net_revenue --db out/touchstone.db > receipt.json
.venv/bin/touchstone verify-receipt receipt.json          # tamper-evidence
.venv/bin/touchstone pubkey --db out/touchstone.db          # publish; verifiers pin it
.venv/bin/touchstone verify-receipt receipt.json --public-key <that key>
```

**Verification decays honestly.** A verdict describes the estate *at scan time*, so
every receipt carries `verified_as_of` plus a `freshness` block. Pass the estate's
sources to `attest` (`--dbt` / `--sql-dir` / `--views` / `--metrics` /
`--agents-schema` / `--osi` / `--uc-metrics`) and it re-hashes them against the scan's recorded snapshot before
answering:

```bash
.venv/bin/touchstone attest gmv --db out/touchstone.db \
  --dbt examples/acme/dbt_project --metrics examples/acme/metrics.yml
# freshness: fresh — estate matches the scanned snapshot
#   ...or, after someone edits a model without rescanning:
# freshness: stale — estate has drifted since the scan: 1 changed, 0 added,
#   0 removed — rescan to re-verify   (drift names the exact artifacts)
```

Statuses: `fresh` (everything re-checked matches), `partial` (checked systems clean,
some scanned systems not re-checked — never claims more than was verified), `stale`
(the estate drifted; the receipt says exactly where), `unknown` (a check was attempted
and could not answer — blocks certification), `unchecked` (no sources passed — disclosed,
does not block). The freshness block is embedded **before signing**, so
"fresh as of T" is itself tamper-evident. Freshness answers only whether the estate is
unchanged; it never makes a receipt's claim usable (see the suspension above).

Trust model, honestly: the embedded key makes receipts *tamper-evident*; *identity*
comes from pinning a published key. Phase 0 keeps the private key in the store's
SQLite; production custody is a KMS behind the same two functions.

### Agents check the receipt (MCP)

```bash
.venv/bin/touchstone mcp --db out/touchstone.db \
  --dbt examples/acme/dbt_project --metrics examples/acme/metrics.yml
```

Exposes `attest` / `asof` / `history` / `concepts` / `relationships` / `verify_receipt` as MCP tools —
the §6 answer-path check: an agent asked "what was net revenue in March" first learns
the definition is CONTRADICTED and by how much, then answers with the receipt id.
With estate sources configured (optional, same source flags as `attest`), every
attest re-checks freshness against the live estate — agents get told when the
verdicts have drifted out from under them.

### Relationships: mined from practice, checked against observed data

Joins are the ontology one level down, and almost nobody declares them — FKs
go undeclared or unenforced, so the only honest record of how tables connect is the
SQL the BI tool actually ran. touchstone mines that history, then refuses to trust it:

```bash
.venv/bin/touchstone mine-relationships \
  --history-file examples/acme/query_history.sql \
  --catalog examples/acme/catalog.yml --db out/touchstone.db
# or pull history from the warehouse itself:
#   --warehouse wh.duckdb --history-query "SELECT query_text FROM ..."

.venv/bin/touchstone verify-relationships \
  --seed examples/acme/seed.sql --dbt examples/acme/dbt_project --db out/touchstone.db
```

```
  [CHECKS PASSED] declared+used  analytics.fct_orders.customer_id = raw.customers.customer_id  · 3 queries
      N:1, parent key raw.customers.customer_id
      as of 2026-10-26T06:31:58.593Z · full table scan
  [FAILED       ] shadow         analytics.fct_orders.order_id = raw.payments.order_id  · 2 queries  · competing keys
      1:N, parent key analytics.fct_orders.order_id — 2 orphan row(s) violate referential integrity
      as of 2026-10-26T06:31:58.595Z · full table scan
  [FAILED       ] shadow         analytics.fct_orders.channel = raw.payments.method  · 1 query  · competing keys
      N:M — no unique side (N:M) — join semantics undefined; note: no rows join across this edge
      as of 2026-10-26T06:31:58.596Z · full table scan
  [CHECKS PASSED] orphan         raw.channels.channel_code = raw.orders.channel  · never used
      1:N, parent key raw.channels.channel_code
      as of 2026-10-26T06:31:58.597Z · full table scan

  checked 4 edge(s): 2 checks passed on observed data, 2 failed, 0 unverified
```

**Used is not valid.** Query history is the richest source of join knowledge in the
estate and also a faithful record of every *popular wrong join* — frequency-weighting
it would rebuild the popularity-ranked context layer this product exists to audit. So
mining produces candidates, declarations produce claims, and only warehouse checks
(key uniqueness, orphan counts, cardinality class) move an edge off `unverified`. Usage decides what gets
verified **first**, never what is true — `--top N` spends the check budget where the
dashboards are.

Every edge therefore carries two orthogonal axes. **Status** says what the warehouse
showed on the data it checked: `checks_passed_on_observed_data`, `failed` or
`unverified`. **Usage** compares declaration with practice: `declared+used`, `shadow` or
`orphan`. A passed shadow edge is a documentation gap; a failed heavily-used edge is an
active bug; a passed orphan means the ERD's keys held and nobody joins it.

**What a passed check shows.** Each check records:
- the tables, keys and join direction;
- every check performed, with its measured counts;
- its scope: full scan or sample, filters, NULL handling;
- when it ran, and the data snapshot or version when the warehouse reports one.

Zero orphans and a unique parent key support those properties **on the checked rows**.
They do not establish that the join is the right business relationship, that it preserves
a metric's meaning, or that it will remain valid. The check SQL is built from the stated
edge with quoted identifiers, never through the SQL-interpretation path whose readings
are suspended. Checks recorded before scopes were stored are shown as the historical
record they are. In the demo above, the payments↔orders join *the shipped query history uses* is
broken: a payment for a cancelled order and one for a deleted order both survive into
`fct_orders` joins. Mining is alias- and direction-free — flipped operands and implicit
`FROM a, b WHERE a.x = b.y` joins collapse into one edge — and unparseable history lines
are skipped, not fatal.

Agents get the graph through the same MCP server (`relationships(table)`), which hands
back edges whose checks passed first, each with its scope, checks, as-of time and that
limitation, and labels everything else as hypothesis rather than fact.

**Warehouses are parameterized.** `--warehouse` accepts a URL; v0 ships the DuckDB
driver (zero-infra, fully tested, `--seed` builds an ephemeral one from a seed script
plus your estate). A Snowflake/BigQuery/Databricks driver is `query(sql) -> rows`,
`close()`, and a dialect attribute — [warehouse.py](src/touchstone/warehouse.py) names the
seam, and unsupported schemes say so instead of silently degrading.

### CI: only newly introduced problems

```bash
.venv/bin/touchstone scan ... --db ci.db        # on main, then again on the PR branch
.venv/bin/touchstone diff --db ci.db --markdown pr-comment.md --fail-on-new contradiction
```

`diff` compares the latest scan to the previous one and reports only *newly
introduced* verdicts (plus what the change resolved) — per spec §6, the bot is never
the messenger for pre-existing debt. `scan --fail-on` remains the absolute gate.

### Monitor: governed use vs. changed definitions in the query stream

The scan verifies the governed estate. `monitor` reads what the warehouse actually
executed — dashboards, notebooks, Genie, agents — and judges it against definitions an
operator declared **authoritative**:

```bash
# A pilot package: approved metric views + their snapshots and definition windows,
# a history export, and any session context you can vouch for (docs/pilot.md)
.venv/bin/touchstone monitor --db out/touchstone.db --pilot pilot/ --dbt my_project

# Or against the scan's declared definitions, from a history file or the warehouse
.venv/bin/touchstone monitor --db out/touchstone.db --history-file history.jsonl \
  --dbt my_project --catalog catalog.yml [--authority uc_metrics,dbt_metrics] \
  [--min-runs 2] [--fail-on-alt]
```

Most of what it sees is a governed metric used correctly: net revenue filtered to the US,
grouped by channel. Some of it is a definition quietly changed. It tells them apart with
**bounded rules** (`monitor-conformance/1`, [conformance.py](src/touchstone/services/conformance.py)):

Positive verdicts are suspended, so the rules' conclusions are reported one step weaker:

| Finding | Meaning |
| --- | --- |
| UNVERIFIABLE — experimental match | What the rules would call conforming: exact equality with an authoritative definition under the experimental rules, **or** one of the listed shapes (the definition's own measure over its base table, every required filter kept as a top-level AND condition, extra conditions only on *supported declared dimensions*, grouping only by them — or a bare `MEASURE(m)` / `AGG(m)` over an approved metric view, filtered and grouped by its dimensions). Kept under `experimental`; not a conclusion |
| SUSPECTED | What the rules would call a violation, for a query inside the supported boundary: the measure changed or transformed, a required filter absent / weakened through OR / broadened / replaced, a slice on a column the model does not declare as a dimension, a different source, or a governed measure presented under another metric's name. The reasons are observations from SQL interpretation that is not verified; `--fail-on-alt` gates on these as a triage signal |
| CANDIDATE | The same measure over the same columns, unnamed, that would be a violation — a hypothesis, ranked lower |
| UNVERIFIABLE | Everything outside the rules, with the reason: SQL outside the proof grammar (joins, LIMIT, DISTINCT, windows, subqueries …), a governed calculation consumed by a CTE or join, HAVING, unsupported dimension expressions, a short table name with no evidenced session context, no definition evidenced for the execution time, or a query below proof grade. A select-project-filter CTE is inlined and judged as the query it is. Outside the grammar, a would-be violation is shown only as `suspected` ("under the optimizer's reading (a hypothesis)") |
| UNREGISTERED | A recurring measure no authoritative definition covers |

Referencing an approved metric is tracked **separately** from preserving it:
`MEASURE(net_revenue) * 2 AS net_revenue` is a governed reference *and* a suspected
difference.
A query is judged as the metric it **claims** to compute: two analysts running the same
`SUM(amount)` as `gross_revenue` and as `net_revenue` get separate findings, each with only
their own executions, and the second one's match to gross revenue's definition is
evidence on its suspected difference (`its calculation: …`), never a match that could
stand in for it. Each use's headline bucket is its most severe finding. A
filter on `amount` is permitted only when the governed model declares `amount` a
dimension. A short table name resolves only from its own history row's catalog/schema, or
from a context asserted to cover every execution (`--assert-context-for-all-executions`,
or `execution_context.asserted_for_all_executions` in the pilot); otherwise it is
unverifiable. Executions are counted once per statement id, and each finding carries who
ran it and from which dashboard or Genie space.

Every measure use lands in exactly one bucket, reported against an explicit denominator:

```
measure uses: 12 — every use counted once below; governed references counted alongside
  governed references (MEASURE/AGG, approved view)        4/12 (33%)
  positive verdicts are suspended: a match is an experimental comparison, a difference is suspected — nothing below is a conclusion
  suspected differences (named metric)                    4/12 (33%)
  candidates (unnamed near misses)                        2/12 (17%)
  unregistered                                            0/12 (0%)
  unverifiable                                            6/12 (50%)  execution context 2 · experimental match (positive verdicts suspended) 4
  below recurrence threshold (≥2 runs; not judged)        0/12 (0%)
  (of the unverifiable: 4 matched under the experimental rules — exact 0 · raw-table slice 2 · metric view 2; not conclusions)
```

An experimental match asserts nothing beyond its rule set, which states what it does
not cover; execution testing challenges the rules (see the execution oracle below) but
does not establish them. Client-side math (spreadsheet pivots, non-pushdown calculated
fields) never reaches the warehouse and is out of scope. v1 is read-only: findings are
reported, not recorded.

### Advisory drift assessment (in development)

`touchstone monitor --export-comparisons uses.jsonl` writes one `touchstone-comparison/1`
evidence record per distinct judgment of a candidate metric use: any query that
aggregates and names a governed metric or reads its source, whether or not the rules
could read it, with the executions it covers. The statement and the authoritative
definition come first, as quoted data; touchstone's own reading follows as explicitly
unverified observations. A model answers versioned per-component questions, and fixed
code aggregates the answers into `suspected_drift`, `insufficient_evidence`,
`likely_permitted` or `not_a_use`.

```bash
touchstone assess run --provider databricks --model <endpoint> --endpoint <warehouse> \
  --batch-size 20 --comparisons uses.jsonl --out verdicts.jsonl --cache verdicts.cache.jsonl
```

Bring your own model: inside your data platform through Databricks (`ai_query`, run
against a real workspace) or Snowflake (Cortex `AI_COMPLETE`), or with an API key through
Anthropic or any OpenAI-compatible server (OpenAI, a LiteLLM proxy, vLLM, Groq, Ollama).
`--model` is always yours to choose; [docs/assess.md](docs/assess.md) lists how the measured
models did, and the Snowflake and API backends are tested against scripted responses only so
far. A deterministic fake measures nothing. Records whose comments carry prompt-like steering text are held for a
person, not sent. `touchstone assess benchmark` scores a backend on 66 labeled metric
uses; a further 114 uses in `benchmark/metric_uses_exec` carry the queries their labels
were checked against by execution.

**What has been measured, and against what.** On 174 synthetic uses, the rules decided
55, with no drift wrongly called permitted; Claude judges matched 165 (Haiku, the default)
to 167 (Opus 5.5); the open models a Databricks Free Edition workspace serves matched 125
to 132. The labels
are model-written (execution-checked where execution can decide), and no human has
reviewed them, so these figures describe agreement with those labels on this synthetic
set, not accuracy on a real estate. See [docs/assess.md](docs/assess.md).

### Agents Schema: claims in, receipts out

[Agents Schema](https://www.opendatainfrastructure.com/agents-schema) publishes
declared semantics as `AGENTS.*` tables inside the warehouse so AI agents can
discover business context with plain SQL. By its own spec it carries no
verification, no history, and no conflict resolution — a distribution rail for
claims, which makes it both a touchstone input and a touchstone output:

```bash
# In: OSI metrics + LookML measures become claims alongside the metrics YAML.
.venv/bin/touchstone scan --dbt examples/acme/dbt_project \
  --metrics examples/acme/metrics.yml \
  --agents-schema examples/acme/agents_schema.json \
  --catalog examples/acme/catalog.yml --db out/touchstone.db

# Out: signed receipts written back into AGENTS.ROOT (spec §7 Phase 2), each with an
# assessment/<concept> row saying whether its claim may be relied on (not while
# positive verdicts are suspended), a policy/conclusions row, a skill/verify-metric
# row teaching agents the check — and a structured touchstone.verdicts projection
# (policy_status, usable_for_reliance) beside it. Rows published before the
# suspension: `touchstone publish --refresh-policy --warehouse …`.
.venv/bin/touchstone publish --db out/touchstone.db --warehouse out/agents.duckdb
```

### Act on findings — inside rails you already triage

touchstone grows no alert channel of its own. `publish` projects verdicts into
`touchstone.verdicts` (one row per verdict per affected relation), and the
estate's existing immune system gates on it: the shipped **dbt package**
([integrations/dbt/touchstone](integrations/dbt/touchstone)) turns findings into
ordinary tests (`touchstone.no_contradictions`, which fails on suspected
divergences; `touchstone.certified`, which fails closed while positive verdicts
are suspended; `touchstone.fresh_scan`) with dbt-native severity that stay red
until the finding is resolved — state, not a one-shot alert. Non-dbt estates gate on
the same table through Snowflake/Databricks SQL alerts, Dagster asset
checks, Soda/Great Expectations checks, or the CLI's exit codes in any
scheduler — copy-paste templates in
[docs/action-rails.md](docs/action-rails.md). The doctrinal line every rail
shares: touchstone routes, proposes, and gates; it never auto-decides which
side of a suspected divergence is the truth.

The *propose* half is `touchstone propose --out proposals/`: for every
suspected divergence it emits the definition the code actually computes as a
declaration in the ontology's own format (`--format metrics|ossie|uc-metrics`)
— Option A, the ontology drifted — and describes the code change that would
match the declaration instead — Option B, the code drifted — in a
`RESOLUTIONS.md` written to be a PR body. SHADOW_METRICs get their missing
declaration drafted from the fingerprint: accept it or delete the model.
Nothing is applied; the human picks in the review rail, and a declaration
emitted from a fingerprint re-scans to an experimental match (tested round-trip).
The same emitter will back the planned Ossie export (`publish --definitions`,
still open).

The demo export deliberately carries the standard's characteristic
underspecifications — an OSI metric with no filters, a LookML measure with no
grain — and the scan surfaces both as cross-provider contradictions with blame
("different grain (none vs date_trunc('month', order_date))"). Intake accepts a
`.json` export or a DuckDB file with the AGENTS schema (live Snowflake is the
same queries behind a connector, later); LookML `${TABLE}` substitution and
`[field: "value"]` filters are translated, anything stranger degrades at
canonicalization rather than guessing. Positioning in one line: **Agents Schema
tells the agent what a metric means; touchstone tells it whether that meaning is
true — in the same tables, over the same SQL.**

### Apache Ossie: the interchange format, verified

[Apache Ossie](https://github.com/apache/ossie) (incubating; formerly Open
Semantic Interchange) is the vendor-neutral spec for exchanging semantic
models, and dbt Core v1.12+ both reads `osi/` documents and emits
`target/osi_document.json`. Interchange is exactly where definitions drift, so
Ossie documents are a first-class claims source:

```bash
.venv/bin/touchstone scan --dbt my_project \
  --osi my_project/osi \        # or my_project/target/osi_document.json
  --catalog catalog.yml --db out/touchstone.db
```

Spec versions 0.1.0/0.1.1 (the released line dbt accepts) in JSON or YAML.
Three behaviors are specific to this adapter:

* **Cross-dialect variants are compared, not trusted.** An Ossie metric may
  carry one expression per dialect (`ANSI_SQL`, `SNOWFLAKE`, `DATABRICKS`,
  `BIGQUERY`). Each SQL variant is parsed under its own grammar and becomes a
  sibling claim on the same concept, so the Snowflake text quietly diverging
  from the ANSI text surfaces as a CONTRADICTION naming the difference.
  Non-SQL variants (`MDX`/`TABLEAU`/`MAQL`) become inexpressible claims that
  block certification — a variant nobody read is not a variant that agrees.
* **Deny by default.** Unmodelled keys anywhere above a metric taint it;
  `custom_extensions` on a metric (where a vendor round-trip stashes what the
  spec cannot say) make it inexpressible with the vendor named. Relationships
  and key declarations are disclosed as unverified rather than dropped.
* **What dbt drops, we report.** dbt skips unsupported Ossie constructs with
  a log warning (event `I078`); reading the source documents directly turns
  those into refusals with reasons in the report.

### Unity Catalog metric views + generated catalogs

Databricks metric views (GA April 2026, and what dbt-databricks v1.12
materializes) are governed measure declarations — a claims source:

```bash
.venv/bin/touchstone catalog --warehouse duckdb://wh.db --out catalog.yml
.venv/bin/touchstone scan --sql-dir models/ \
  --uc-metrics metric_views/ --catalog catalog.yml --db out/touchstone.db
```

Each `measures[]` entry becomes a claim over the view's `source`, carrying
the view-level `filter`; expressions are parsed as Databricks SQL and
rendered for the scan dialect. Semantics that would change the metric's
meaning are refused with the reason, not approximated: `window:` measures
(trailing/cumulative/semiadditive), expressions reaching through join
aliases, `:parameter` references, and measures naming a declared field whose
expression is not the identity column. Joins (with cardinality),
parameters, and materialization schedules are disclosed as unverified.
`touchstone catalog` generates the physical catalog from any warehouse's
ANSI `information_schema.columns` — regenerate it instead of hand-
maintaining YAML that drifts.

### Prove the prover — across dialects

```bash
.venv/bin/touchstone bench
# equivalence corpus: 32 pairs · dialects: duckdb, databricks, snowflake, bigquery · proof-grade coverage 78.1%
#     duckdb      10/10 detected · near-misses 11/20 · coverage 78.1% · false certs 0
#     ...
#   equivalence detection: 40/40 (100%)
#   known gaps: 2 confirmed
#   near-misses distinguished: 44/80
#   outside the proof grammar (touchstone-proof/2), refused: 0 equivalence / 36 near-miss run(s)
#     join-type-inner-vs-left: uses a join
#     limit-is-not-neutral: uses LIMIT
#     ...
#   false equalities: 0 ✓ (experimental rules; not a certification)
```

The equivalence corpus ships in the repo (`src/touchstone/benchmark/equivalence.yaml`):
same-math rewrite pairs the canonicalizer must prove identical, `known_gap` rewrites it
deliberately misses toward the safe side (difference-hypothesis), and near-miss pairs
that must never hash-equal — one false certification in **any dialect** fails the run,
because falsely certifying two different definitions is the failure a verifier cannot
survive. The dialect matrix (`--dialects`, default above) is what turns "system-
agnostic" from a declared property into a certified one — dialect quirks are exactly
where canonicalizers break, so the corpus must hold everywhere a real estate speaks.
Honesty rules:

- A near-miss pair that fails to reach proof grade for any reason other than the
  grammar is reported as `UNPROVEN`, never as distinguished: a broken prover must not
  look like a passing one.
- A pair outside the proof grammar is `refused`, with the construct named. It counts
  as neither detected nor distinguished; it is the scope's measured cost.
- Unknown dialect names are rejected rather than silently run as generic SQL.

**Nine of those near-misses exist because they once passed.** An adversarial review of
this engine on 2026-07-25 found six live false-certification paths — CTE bodies, join
type, HAVING, LIMIT, grouping sets, `SELECT DISTINCT` — while `bench` reported
`false certifications: 0 ✓`. The corpus had been written by the implementer and so
tested only the implementer's imagination. Each is now a corpus case that goes red
without its fix.

**It happened again, and a corpus cannot stop it.** An external review of `ec4baff`
(2026-10) reproduced two more false certifications while `bench` was green: reads of
`prod.raw.orders` and `dev.raw.orders` certified identical because both matched a
short catalog entry `raw.orders` (executed, they returned 100 and 900), and
`SUM(a + b + c)` certified equal to `SUM(a + c + b)` over DOUBLE values where they
return 1 and 0, because the normalizer reassociated floating-point addition. Fixing
that exposed a third: sqlglot renders `a + (b + c)` and `(a + b) + c` as the same
text, so equality over rendered SQL could not see the grouping. Neither pair had
been written into the corpus. A benchmark of hand-picked pairs judged by the
canonicalizer's own output tests the same imagination that wrote the canonicalizer.

### The execution oracle

```bash
.venv/bin/touchstone oracle            # ~740 queries, ~2,500 pairs + ~5,100 proof-form executions, ~8s
.venv/bin/touchstone oracle --full     # ~24,500 queries, ~111,500 pairs + ~139,000 proof-form executions
```

The oracle does not ask the canonicalizer whether it is right. It generates queries
across every rewrite the engine performs (arithmetic, predicates, grouping
constructs, joins, relation qualification, order-sensitive consumers, subqueries),
buckets them by core hash among the proof-grade fingerprints — exactly the set the
engine would certify equal — and EXECUTES every member of every bucket in DuckDB
against adversarial datasets: NULLs, float cancellation and overflow, integer
overflow, ties, empty tables, and separate databases holding different data under the
same short table name. Any two members whose results differ are a false
certification. Results are compared exactly: integers and decimals as exact values,
floats as their exact binary values, with no tolerance of any kind; execution is
single-threaded, so summation order is the scan order. Every query is compared with every other, so cross-family collisions
surface too. A self-test requires the datasets to tell apart pairs known to
differ, so a quiet oracle has demonstrably been able to speak. Run against `ec4baff`,
it reports 45 false certifications covering every class above.

Under [`touchstone-proof/2`](docs/proof-policy.md) it also executes **every proof-grade
query against its own proof form**, the SQL the stated rules produced from it. Each
rule is therefore challenged one query at a time, not only through the pairs that
happen to bucket together. Inlining variants carry traps: swapped and renamed
columns, a computed projection under a tighter operator, NOT over a substituted
comparison, and filters on both sides.

On the current code it reports no disagreements under exact comparison, which is
exact even beyond the 28 significant digits a default decimal context keeps:

| Run | Pair executions | Proof-form executions | Disagreements |
| --- | --- | --- | --- |
| Default | ~2,500 | ~5,100 | 0 |
| `--full` | ~111,500 | ~139,000 | 0 |

Its scope is DuckDB execution of DuckDB-dialect SQL. The other dialects in the matrix
are canonicalized by `bench`, not executed.

The same command then challenges the monitor's conformance rules. It generates
authority × slice × grouping variants plus near misses (required filters dropped,
weakened through OR, broadened or replaced; transformed and changed measures; slices on
undeclared columns; HAVING, LIMIT, joins). Every query the rules call conforming is
executed against the expected query — the authority's measure under its required filters
AND the variation the rules identified — on every dataset, exactly; no near miss may
conform, and no legitimate variant may be called a violation. On its first run it found
a real defect: contradictory slices fold a WHERE to `FALSE`, and the rules reported the
required filter as missing; that case is now unverifiable. Current results:

| Run | Conforming verdicts | Executions | Disagreements | Near misses conformed |
| --- | --- | --- | --- | --- |
| Default | ~480 | ~3,800 | 0 | 0 |
| `--full` | ~7,750 | ~62,000 | 0 | 0 | Agreement on these datasets challenges the rules; it does not
prove them, and whether a slice is *permitted* is policy, tested by the acceptance cases.
`MEASURE()` cannot run in DuckDB; `tests/test_databricks_integration.py` checks it on a
real workspace when `TOUCHSTONE_DATABRICKS_*` is set (opt-in, not yet run).

### What an experimental equality does not establish

While positive verdicts are suspended, no output is a certification. When they are
re-enabled (criteria in [docs/proof-policy.md](docs/proof-policy.md): an explicitly
defined grammar, justified transformations, and evidence that parsing matches each
target engine — not one clean review or pilot), a `CERTIFIED` verdict would mean all
of the following, and today an experimental equality means only that the engine's
checks below held on the tree sqlglot parsed:

- Both definitions are inside the proof grammar of [`touchstone-proof/2`](docs/proof-policy.md),
  and their proof forms are equal. Those forms come only from the policy's stated
  rules, each with its preconditions.
- No boolean simplification, constant folding, re-association or predicate movement
  took part.
- Relation identity came from names the SQL wrote or from execution context the
  operator supplied (`--default-database` / `--default-schema`). It never came from a
  shared short catalog entry, whose columns are never borrowed for a longer name.
- The parser was a qualified sqlglot version (`touchstone/toolchain.py`). An
  unqualified version withholds certification, and a new one qualifies only through
  `scripts/qualify_sqlglot.sh`, which runs the suite, the benchmark and the full
  oracle.

The verdict's evidence names the policy, the rules and the assumptions it rests on.
A scan made under an earlier policy, including one with every file unchanged, is
reported `stale`, and a receipt issued from it is `UNRESOLVED`. It does not cover
semantics the SQL text does not
carry (session time zones, collations, user-defined functions), engine-specific
runtime behaviour beyond the DuckDB oracle, or definitions the scan did not reach. A
receipt verification answers separately whether it is a valid record of the instant
it describes, and — for current use — three questions: is the signature authentic (this
store issued exactly this payload), is the estate unchanged (a LIVE re-check at
verification time against the snapshot of the receipt's own scan re-hashes every
scanned source and finds them unchanged), and is the claim usable under the policy in
force. Current use needs all three; while positive verdicts are suspended the third is
always no. The freshness recorded at issuance says what
was true then, not now: offline verification establishes signature integrity and
historical validity only. `verify-receipt --require-current --db … <estate sources>`
and the MCP tool on a server configured with the estate perform the live check; an
`--as-of` receipt is never valid for current use. An as-of receipt is valid for *its*
stated use only if it is an authentic record whose claim is usable, so while positive
verdicts are suspended `verify-receipt` exits 1 for every receipt, as-of included, and
MCP `verify_receipt` returns `valid: false`. The signed status is printed as signed
(`CERTIFIED (as signed; suspended …)`), never as a current conclusion. A receipt that
the store passed with `--db` did not issue is not a historical record of anything.

What the live check requires:

- **Every source the scan read is re-checked**, including the `--catalog`. Verdicts
  depend on the catalog's columns and types, so an unchecked catalog leaves freshness
  `partial`.
- **Files the scan's loaders could not read count too.** A file that becomes unreadable
  is drift.
- **The receipt was issued by the store doing the checking.** A receipt re-signed under
  another key, however valid its signature, is never current there.
- **The estate is re-hashed in the scan's own dialect** unless `--dialect` is given.

### The completeness gate

That review exposed a design flaw deeper than any single bug: **the `SemanticCore` is a
whitelist**, so any construct it does not read defaults to *absent*, and two definitions
differing only in an unread construct hash identically. A blind spot became a
certification.

The default is now inverted ([completeness.py](src/touchstone/extraction/completeness.py)).
Anything the core cannot represent marks the measure `INCOMPLETE`, which sits outside
`PROOF_LEVELS` — it can still be reported, diffed and blamed, but can never be certified
equal to anything, and the receipt names what could not be read. Constructs leave that
list only by being genuinely modeled (each is now folded into the core: HAVING/QUALIFY
and bounds as tagged filters, grouping constructs in grain, join side and ON-less joins
in the join graph, CTE bodies as a recursive content digest keyed on the body rather
than the name). A gate that never lifts is just a refusal to verify.

## Formerly semantix

The project was called semantix. What was written under that name keeps meaning
what it meant:

- **Stores.** When `touchstone.db` is missing and `semantix.db` sits beside it, every
  command refuses rather than create a new store. A store holds the signing key, and a
  new one would mint a new key that no pinned public key matches. With nothing holding
  it open (stop any `semantix mcp` server first), rename `semantix.db` to
  `touchstone.db`, together with `semantix.db-wal` and `semantix.db-shm` if they are
  there: the store runs in WAL mode, and recent writes live in the `-wal` file until
  they are checkpointed. Or pass `--db semantix.db`. A `touchstone.db` beside a
  stranded, non-empty `semantix.db-wal` is refused until the `-wal` is renamed to match.
- **Policy id.** Scans and receipts recorded under `semantix-proof/2` are under
  `touchstone-proof/2`. The grammar did not change with the name, and neither do
  fingerprint ids: rescanning an existing store records no restatement.
- **SQL annotations.** `-- semantix: relation=` directives and `semantix: metric=` tags
  are still read.
- **Published tables.** `touchstone publish` replaces the rows published under the
  `semantix` provider in `agents.root`. Agent queries that filter
  `provider = 'semantix'` must now filter `provider = 'touchstone'`; the republished
  `skill/verify-metric` row says so. While a `semantix.verdicts` table exists, publish
  writes the same projection into it, so gates that still read it keep failing on new
  findings. `--refresh-policy` brings both tables under the policy. Point the gates at
  `touchstone.verdicts`, then drop `semantix.verdicts`.
- **What you wrote that names it.**
  - The command is `touchstone`. Update CI, cron jobs and MCP client configs
    (`{"command": "touchstone", "args": ["mcp", …]}`). In an existing virtualenv, run
    `pip uninstall semantix` before reinstalling, so no stale `semantix` command lingers.
  - The dbt package is `integrations/dbt/touchstone`: tests are `touchstone.<test>`,
    and the vars are `touchstone_database`, `touchstone_schema` and `touchstone_table`.
  - Environment variables are `TOUCHSTONE_*` (`TOUCHSTONE_DATABRICKS_*`,
    `TOUCHSTONE_QUALIFY_SQLGLOT`).

## Layout (maps 1:1 to the spec's five layers)

```
src/touchstone/
  adapters/         L1  dbt manifest+compiled SQL, raw-SQL dirs, view-DDL dumps,
                        metrics YAML, Agents Schema, Apache Ossie documents,
                        catalog + declared keys/relationships                  (§2)
  extraction/       L2  SQLGlot canonicalization ladder, fingerprint builder,
                        schema registry with physical-lineage terminals        (§3)
  store.py          L3  bitemporal, append-only fingerprint store + signing keys,
                        relationship edges/observations/checks                 (§4)
  services/         L4  concept resolution, reconciliation + clause-level blame,
                        scan-over-scan diff, DuckDB execution slice, receipt
                        freshness, relationship mining + verification          (§5)
  warehouse.py      L1  parameterized warehouse access (DuckDB driver ships)   (§2)
  report/, cli.py   L5  HTML concept map; scan/diff/history/asof/attest/
                        verify-receipt/pubkey/mcp/bench/mine-relationships/
                        verify-relationships/relationships CLI                 (§6)
  mcp.py            L5  attestation MCP server (stdio, stdlib-only)            (§6)
  signing.py            Ed25519 receipt signing / offline verification
  bench.py              open equivalence benchmark over the shipped corpus
  engine.py             scan orchestrator
```

## Design invariants (from the spec, enforced in code)

**Append-only store, derived valid time.** Every write is an INSERT; fingerprints are
content-addressed and immutable. A definition's valid interval is *never stored* — it is
computed at read time from the observation log, so history can't be retroactively edited.

**Asymmetric equivalence.** Canonical-form equality (at proof-grade canonicalization) is
proof of sameness; canonical difference is only a hypothesis. Phase 0 verdicts therefore
carry `method: static-canonical-diff` and name exactly which components diverge
(expression / filters / grain / join graph / physical lineage). The Phase 1 execution
harness upgrades hypotheses to measured numeric divergence.

**Terminal triage.** `--min-severity N` and `--top N` bound what the terminal
prints on a large estate (a first scan can emit one SHADOW_METRIC per
undeclared measure); every verdict is still stored, in the report, and in
`--json`.

**Degradation ladder, never hard failure.** `supported` is the only proof level:
inside the proof grammar, from stated rules. Below it come `optimized` and `qualified`,
which are the general optimizer's readings (hypotheses, never proof), then `parsed`
and `failed`. `incomplete` is the completeness gate's verdict on SQL we parsed but
cannot fully model. Artifacts that can't be fully canonicalized still get recorded; equality
below proof grade can't certify (`UNRESOLVED`, not a false `CERTIFIED`). The scan
prints two coverage numbers: how much SQL parsed and qualified, and how many measures
reached proof grade (each unreadable artifact counts as one unverifiable measure). The
second is the one to judge readiness by. The §9 target is >85 %; the demo estate is at
80 % under `touchstone-proof/2`, because one of its five models joins two tables. Coverage used to be the first number alone, which let an estate whose only metric
used TABLESAMPLE report 100 % while nothing in it could be certified.

In practice `qualified` almost never fires: `qualify()` fails on the same
unresolvable-column condition as `optimize()`, so coverage behaves as a binary
between "bound to the catalog" and "not". Coverage on a real estate therefore depends
entirely on whether relation names in the SQL match the names in `--catalog` and in the
manifest — see `--min-coverage`, which fails a scan that read the estate but understood
little of it.

**Names live outside the semantic core.** Two definitions are compared by name-free
canonical content, which is what makes aliasing detectable and renames non-events.

## Phase 0 deviations from the spec

- **SQLite instead of Postgres** for the store engine. The schema is portable SQL (TEXT
  ids, ISO-8601 timestamps, JSON-as-TEXT) and every query is standard; the spec's v0
  Postgres target is a connection-string change, not a redesign. Chosen so Phase 0 runs
  with zero infrastructure.
- **Clustering signals**: normalized-name equality plus identical-core merge. Lineage-
  overlap and embedding similarity (§5) are Phase 1; `services/resolution.py` is where
  they slot in. Human merge/split overrides are likewise stubbed for Phase 1.
- **Severity** is scored from verdict type, declared presence, and variant count. Usage
  telemetry (query counts, dashboards) arrives with the warehouse query-history adapter.

## Phase 1 backlog (spec §7) — updated

**Landed early** (Phase-0-honest cuts of Phase 1/2 items): execution against locally
reproducible data (`scan --execute`; the full harness with warehouse creds, sampling
windows, and cost controls is still Phase 1) · the CI bot's core (`touchstone diff`
+ `--fail-on-new`; the GitHub App wrapper that posts the comment remains) · the
attestation MCP server (was Phase 2; receipt schema unchanged) · signed receipts ·
receipt write-back into Agents Schema (`touchstone publish`; DuckDB target now,
Snowflake connector remains) · an Agents Schema claims adapter (second-platform
declared source: OSI + LookML) · an Apache Ossie document adapter (`scan --osi`;
per-dialect variants as sibling claims, cross-dialect drift as contradictions).

**Still open:** monitor persistence (first/last seen, new findings only) and a
pre-execution `check_sql` for agents · join slices and dbt / metrics-YAML dimensions in
the conformance rules · live Databricks connector (system.query.history and
information_schema over a real workspace — file-based intake for both ships) ·
Ossie export (`publish` emitting Ossie documents stamped with the
receipt hash + `verified_as_of`) · applying the declared join graphs (Ossie
relationships, metric-view joins) so cross-dataset metrics translate ·
telemetry-driven severity ·
concept-resolution overrides (lineage-overlap clustering first; embedding similarity
only as candidate generation — consensus signals must never decide a verdict, that is
the failure mode this engine exists to correct) · IN-list operand normalization (the
benchmark's one confirmed gap) · change-attribution service · live warehouse
connectors for Agents Schema read/write.

## License

Apache License 2.0. See [LICENSE](LICENSE).
