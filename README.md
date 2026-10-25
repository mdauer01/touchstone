# semantix

semantix finds where a metric's declared definition and the SQL that computes it
disagree, and explains each difference clause by clause. It certifies that two
definitions are the same only within a narrow scope that it states and tests.

It reads computed definitions from dbt projects, plain SQL directories and view-DDL
dumps, and declared definitions from metrics YAML, the dbt Semantic Layer, Apache
Ossie documents, Unity Catalog metric views and Agents Schema tables.

- Concept: [docs/concept-one-pager.md](docs/concept-one-pager.md)
- Architecture: [docs/architecture-spec.md](docs/architecture-spec.md)

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                      # full suite

.venv/bin/semantix scan \
  --dbt examples/acme/dbt_project \
  --metrics examples/acme/metrics.yml \
  --catalog examples/acme/catalog.yml \
  --execute examples/acme/seed.sql \
  --db out/semantix.db \
  --report out/acme-report.html \
  --label "acme demo estate"

open out/acme-report.html
```

(`--execute` is optional; estates without dbt scan via `--sql-dir <dir>` — one SELECT
per `.sql` file, `-- semantix: relation=…` header to override naming — and/or
`--views dump.sql` for a file of `CREATE VIEW` statements.)

The demo estate is seven compiled dbt models plus a three-metric declared ontology,
engineered so one scan produces every verdict type:

| Concept | Verdict | Why |
| --- | --- | --- |
| `net_revenue` | **CONTRADICTION** (sev 94) | Finance's model matches the declared definition; the engine's own blame sentence: *"growth_net_revenue diverges: missing filter status = 'completed'; adds filter status IN ('completed', 'pending'); expression omits refund_amount; reads analytics.stg_orders instead of analytics.fct_orders. Measured on seed data: totals 114,000 vs baseline 84,000 (+35.7%)."* |
| `gmv` | **CERTIFIED + ALIASING** | `total_gmv` and `gross_merchandise_value` are canonically identical to the declared metric — CTE vs. inline, flipped predicate order, `NOT =` vs. `<>` and `SELECT *` all normalize away. |
| `customer_ltv` | **ORPHAN_CLAIM** | Declared, never computed anywhere. |
| `refund_rate` | **SHADOW_METRIC** | Computed in ops, absent from the ontology. |

## Supported scope

**What it reads.** SQL in four dialects: DuckDB, Databricks, Snowflake and BigQuery.
Computed definitions come from dbt manifests, directories of `.sql` files, or
`CREATE VIEW` dumps; declared definitions from the formats listed above. The only
shipped warehouse driver is DuckDB, so other warehouses are read through exported
files.

**What a CERTIFIED verdict means.** The definitions' canonical cores are equal at
proof grade; every construct they use is one the core models; relation identity came
from names the SQL wrote or from `--default-database` / `--default-schema`; and the
parser is a qualified sqlglot version. Full statement and limits:
[What a certification guarantees](#what-a-certification-guarantees-stated-narrowly).

**What it refuses rather than guesses.** A construct the core does not model makes a
definition unverifiable: for example `TABLESAMPLE`, window-measure semantics,
joined-table references in metric views, bind parameters, or an unqualified
reference whose database depends on the session. It is reported with the reason and
never certified.

**What it does not cover.** Semantics the SQL text does not carry (session time
zones, collations, user-defined functions), engine runtime behaviour beyond the
DuckDB execution oracle, calculations done client-side that never reach the
warehouse, and definitions the scan did not read.

**How the claims are tested.**

| Check | What it does |
| --- | --- |
| `pytest` | The suite, including one regression file per external review round |
| `semantix bench` | 32 canonicalization pairs in four dialects; near-misses must never hash-equal |
| `semantix oracle` | Executes every pair the engine would certify equal against adversarial DuckDB data, compared exactly; then executes every query the monitor's rules call conforming against its definition plus the identified variation |

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
.venv/bin/semantix history net_revenue --db out/semantix.db   # valid-time intervals per site
.venv/bin/semantix asof net_revenue --at <timestamp after your first scan> --db out/semantix.db
.venv/bin/semantix attest net_revenue --db out/semantix.db    # semantic receipt (JSON)
```

`history` flags definition changes as restatement boundaries; `asof` answers "what did
this metric *mean* on this date"; `attest` issues the §6 receipt — status, immutable
definition versions, physical lineage, known divergences — **Ed25519-signed over its
canonical payload**, so it verifies offline:

```bash
.venv/bin/semantix attest net_revenue --db out/semantix.db > receipt.json
.venv/bin/semantix verify-receipt receipt.json          # tamper-evidence
.venv/bin/semantix pubkey --db out/semantix.db          # publish; verifiers pin it
.venv/bin/semantix verify-receipt receipt.json --public-key <that key>
```

**Verification decays honestly.** A verdict describes the estate *at scan time*, so
every receipt carries `verified_as_of` plus a `freshness` block. Pass the estate's
sources to `attest` (`--dbt` / `--sql-dir` / `--views` / `--metrics` /
`--agents-schema` / `--osi` / `--uc-metrics`) and it re-hashes them against the scan's recorded snapshot before
answering:

```bash
.venv/bin/semantix attest gmv --db out/semantix.db \
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
"fresh as of T" is itself tamper-evident. A stale CERTIFIED receipt is a rescan
demand, not a truth claim.

Trust model, honestly: the embedded key makes receipts *tamper-evident*; *identity*
comes from pinning a published key. Phase 0 keeps the private key in the store's
SQLite; production custody is a KMS behind the same two functions.

### Agents check the receipt (MCP)

```bash
.venv/bin/semantix mcp --db out/semantix.db \
  --dbt examples/acme/dbt_project --metrics examples/acme/metrics.yml
```

Exposes `attest` / `asof` / `history` / `concepts` / `relationships` / `verify_receipt` as MCP tools —
the §6 answer-path check: an agent asked "what was net revenue in March" first learns
the definition is CONTRADICTED and by how much, then answers with the receipt id.
With estate sources configured (optional, same source flags as `attest`), every
attest re-checks freshness against the live estate — agents get told when the
verdicts have drifted out from under them.

### Relationships: mined from practice, certified by the warehouse

Joins are the ontology one level down, and almost nobody declares them — FKs
go undeclared or unenforced, so the only honest record of how tables connect is the
SQL the BI tool actually ran. semantix mines that history, then refuses to trust it:

```bash
.venv/bin/semantix mine-relationships \
  --history-file examples/acme/query_history.sql \
  --catalog examples/acme/catalog.yml --db out/semantix.db
# or pull history from the warehouse itself:
#   --warehouse wh.duckdb --history-query "SELECT query_text FROM ..."

.venv/bin/semantix verify-relationships \
  --seed examples/acme/seed.sql --dbt examples/acme/dbt_project --db out/semantix.db
```

```
[CERTIFIED ] declared+used  analytics.fct_orders.customer_id = raw.customers.customer_id · 3 queries
      N:1, parent key raw.customers.customer_id
[FAILED    ] shadow         analytics.fct_orders.order_id = raw.payments.order_id · 2 queries
      1:N, parent key analytics.fct_orders.order_id — 2 orphan row(s) violate referential integrity
[FAILED    ] shadow         analytics.fct_orders.channel = raw.payments.method · 1 query
      N:M — no unique side (N:M) — join semantics undefined
[CERTIFIED ] orphan         raw.channels.channel_code = raw.orders.channel · never used
      1:N, parent key raw.channels.channel_code
```

**Used is not valid.** Query history is the richest source of join knowledge in the
estate and also a faithful record of every *popular wrong join* — frequency-weighting
it would rebuild the popularity-ranked context layer this product exists to audit. So
mining produces candidates, declarations produce claims, and only warehouse checks
(key uniqueness, orphan counts, cardinality class) certify. Usage decides what gets
verified **first**, never what is true — `--top N` spends the check budget where the
dashboards are.

Every edge therefore carries two orthogonal axes: **status** (`certified` / `failed` /
`unverified` — what the warehouse proved) and **usage** (`declared+used` / `shadow` /
`orphan` — declaration vs. practice). A certified shadow edge is a documentation gap; a
failed heavily-used edge is an active bug; a certified orphan means the ERD is right and
nobody joins it. In the demo above, the payments↔orders join *the shipped query history uses* is
broken: a payment for a cancelled order and one for a deleted order both survive into
`fct_orders` joins. Mining is alias- and direction-free — flipped operands and implicit
`FROM a, b WHERE a.x = b.y` joins collapse into one edge — and unparseable history lines
are skipped, not fatal.

Agents get the graph through the same MCP server (`relationships(table)`), which hands
back certified paths first and labels everything else as hypothesis rather than fact.

**Warehouses are parameterized.** `--warehouse` accepts a URL; v0 ships the DuckDB
driver (zero-infra, fully tested, `--seed` builds an ephemeral one from a seed script
plus your estate). A Snowflake/BigQuery/Databricks driver is `query(sql) -> rows`,
`close()`, and a dialect attribute — [warehouse.py](src/semantix/warehouse.py) names the
seam, and unsupported schemes say so instead of silently degrading.

### CI: only newly introduced problems

```bash
.venv/bin/semantix scan ... --db ci.db        # on main, then again on the PR branch
.venv/bin/semantix diff --db ci.db --markdown pr-comment.md --fail-on-new contradiction
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
.venv/bin/semantix monitor --db out/semantix.db --pilot pilot/ --dbt my_project

# Or against the scan's declared definitions, from a history file or the warehouse
.venv/bin/semantix monitor --db out/semantix.db --history-file history.jsonl \
  --dbt my_project --catalog catalog.yml [--authority uc_metrics,dbt_metrics] \
  [--min-runs 2] [--fail-on-alt]
```

Most of what it sees is a governed metric used correctly: net revenue filtered to the US,
grouped by channel. Some of it is a definition quietly changed. It tells them apart with
**bounded rules** (`monitor-conformance/1`, [conformance.py](src/semantix/services/conformance.py)):

| Finding | Meaning |
| --- | --- |
| CONFORMING | Exact canonical equality with an authoritative definition, **or** one of the listed shapes: the definition's own measure over its base table, every required filter kept as a top-level AND condition, extra conditions only on *supported declared dimensions*, grouping only by them — or a bare `MEASURE(m)` / `AGG(m)` over an approved metric view, filtered and grouped by its dimensions, in a statement built entirely from an allow-listed set of constructs |
| VIOLATION | A true statement about the SQL as written: the measure changed or transformed, a required filter absent / weakened through OR / broadened / replaced, a slice on a column the model does not declare as a dimension, a different source, or a governed measure presented under another metric's name |
| CANDIDATE | The same measure over the same columns, unnamed, that would be a violation — a hypothesis, ranked lower |
| UNVERIFIABLE | Everything outside the rules, with the reason: joins, CTEs or joins around a governed calculation, HAVING/LIMIT/DISTINCT, unsupported dimension expressions, a short table name with no evidenced session context, no definition evidenced for the execution time, or a query below proof grade |
| UNREGISTERED | A recurring measure no authoritative definition covers |

Referencing an approved metric is tracked **separately** from preserving it:
`MEASURE(net_revenue) * 2 AS net_revenue` is a governed reference *and* a violation.
A query is judged as the metric it **claims** to compute: two analysts running the same
`SUM(amount)` as `gross_revenue` and as `net_revenue` get separate findings, each with only
their own executions, and the second one's match to gross revenue's definition is
evidence on its violation (`its calculation: …`), never a conforming finding that could
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
  conforming                                              4/12 (33%)  exact 0 · raw-table slice 2 · metric view 2
  definition violations                                   4/12 (33%)
  candidates (unnamed near misses)                        2/12 (17%)
  unregistered                                            0/12 (0%)
  unverifiable                                            2/12 (17%)  execution context 2
  below recurrence threshold (≥2 runs; not judged)        0/12 (0%)
  checked = conforming + violations                       8/12 (67%)
```

A conforming verdict asserts nothing beyond its rule set, which states what it does not
cover; execution testing challenges the rules (see the execution oracle below) but does
not establish them. Client-side math (spreadsheet pivots, non-pushdown calculated
fields) never reaches the warehouse and is out of scope. v1 is read-only: findings are
reported, not recorded.

### Agents Schema: claims in, receipts out

[Agents Schema](https://www.opendatainfrastructure.com/agents-schema) publishes
declared semantics as `AGENTS.*` tables inside the warehouse so AI agents can
discover business context with plain SQL. By its own spec it carries no
verification, no history, and no conflict resolution — a distribution rail for
claims, which makes it both a semantix input and a semantix output:

```bash
# In: OSI metrics + LookML measures become claims alongside the metrics YAML.
.venv/bin/semantix scan --dbt examples/acme/dbt_project \
  --metrics examples/acme/metrics.yml \
  --agents-schema examples/acme/agents_schema.json \
  --catalog examples/acme/catalog.yml --db out/semantix.db

# Out: signed receipts written back into AGENTS.ROOT (write-back certification,
# spec §7 Phase 2), plus a skill/verify-metric row teaching agents the check —
# and a structured semantix.verdicts projection beside it.
.venv/bin/semantix publish --db out/semantix.db --warehouse out/agents.duckdb
```

### Act on findings — inside rails you already triage

semantix grows no alert channel of its own. `publish` projects verdicts into
`semantix.verdicts` (one row per verdict per affected relation), and the
estate's existing immune system gates on it: the shipped **dbt package**
([integrations/dbt/semantix](integrations/dbt/semantix)) turns findings into
ordinary tests (`semantix.no_contradictions`, `semantix.certified`,
`semantix.fresh_scan`) with dbt-native severity that stay red until the
finding is resolved — state, not a one-shot alert. Non-dbt estates gate on
the same table through Snowflake/Databricks SQL alerts, Dagster asset
checks, Soda/Great Expectations checks, or the CLI's exit codes in any
scheduler — copy-paste templates in
[docs/action-rails.md](docs/action-rails.md). The doctrinal line every rail
shares: semantix routes, proposes, and gates; it never auto-decides which
side of a contradiction is the truth.

The *propose* half is `semantix propose --out proposals/`: for every
CONTRADICTION it emits the definition the code actually computes as a
declaration in the ontology's own format (`--format metrics|ossie|uc-metrics`)
— Option A, the ontology drifted — and describes the code change that would
match the declaration instead — Option B, the code drifted — in a
`RESOLUTIONS.md` written to be a PR body. SHADOW_METRICs get their missing
declaration drafted from the fingerprint: accept it or delete the model.
Nothing is applied; the human picks in the review rail, and a declaration
emitted from a fingerprint scans back in as CERTIFIED (tested round-trip).
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
tells the agent what a metric means; semantix tells it whether that meaning is
true — in the same tables, over the same SQL.**

### Apache Ossie: the interchange format, verified

[Apache Ossie](https://github.com/apache/ossie) (incubating; formerly Open
Semantic Interchange) is the vendor-neutral spec for exchanging semantic
models, and dbt Core v1.12+ both reads `osi/` documents and emits
`target/osi_document.json`. Interchange is exactly where definitions drift, so
Ossie documents are a first-class claims source:

```bash
.venv/bin/semantix scan --dbt my_project \
  --osi my_project/osi \        # or my_project/target/osi_document.json
  --catalog catalog.yml --db out/semantix.db
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
.venv/bin/semantix catalog --warehouse duckdb://wh.db --out catalog.yml
.venv/bin/semantix scan --sql-dir models/ \
  --uc-metrics metric_views/ --catalog catalog.yml --db out/semantix.db
```

Each `measures[]` entry becomes a claim over the view's `source`, carrying
the view-level `filter`; expressions are parsed as Databricks SQL and
rendered for the scan dialect. Semantics that would change the metric's
meaning are refused with the reason, not approximated: `window:` measures
(trailing/cumulative/semiadditive), expressions reaching through join
aliases, `:parameter` references, and measures naming a declared field whose
expression is not the identity column. Joins (with cardinality),
parameters, and materialization schedules are disclosed as unverified.
`semantix catalog` generates the physical catalog from any warehouse's
ANSI `information_schema.columns` — regenerate it instead of hand-
maintaining YAML that drifts.

### Prove the prover — across dialects

```bash
.venv/bin/semantix bench
# equivalence corpus: 32 pairs · dialects: duckdb, databricks, snowflake, bigquery · proof-grade coverage 100.0%
#     duckdb      10/10 detected · near-misses 20/20 · coverage 100.0% · false certs 0
#     databricks  10/10 detected · near-misses 20/20 · coverage 100.0% · false certs 0
#     snowflake   10/10 detected · near-misses 20/20 · coverage 100.0% · false certs 0
#     bigquery    10/10 detected · near-misses 20/20 · coverage 100.0% · false certs 0
#   equivalence detection: 40/40 (100%)
#   known gaps: 2 confirmed
#   near-misses distinguished: 80/80
#   false certifications: 0 ✓
```

The equivalence corpus ships in the repo (`src/semantix/benchmark/equivalence.yaml`):
same-math rewrite pairs the canonicalizer must prove identical, `known_gap` rewrites it
deliberately misses toward the safe side (difference-hypothesis), and near-miss pairs
that must never hash-equal — one false certification in **any dialect** fails the run,
because falsely certifying two different definitions is the failure a verifier cannot
survive. The dialect matrix (`--dialects`, default above) is what turns "system-
agnostic" from a declared property into a certified one — dialect quirks are exactly
where canonicalizers break, so the corpus must hold everywhere a real estate speaks.
Honesty rules: a near-miss pair that fails to reach proof grade under a dialect is
reported as `UNPROVEN`, never as distinguished (a broken prover must not look like a
passing one), and unknown dialect names are rejected rather than silently run as
generic SQL.

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
.venv/bin/semantix oracle            # ~340 queries, ~900 executed pairs, <1s
.venv/bin/semantix oracle --full     # ~15,600 queries, ~92,000 executed pairs, ~40s
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
surface too, and a self-test requires the datasets to tell apart pairs known to
differ, so a quiet oracle has demonstrably been able to speak. Run against `ec4baff`
it reports 45 false certifications covering every class above. On the current code
it reports none: 0 disagreements across 897 executed pairs by default and 91,769 in
the full sweep, under exact comparison. Its scope is DuckDB execution of DuckDB-dialect SQL; the other
dialects in the matrix are canonicalized by `bench`, not executed.

The same command then challenges the monitor's conformance rules. It generates
authority × slice × grouping variants plus near misses (required filters dropped,
weakened through OR, broadened or replaced; transformed and changed measures; slices on
undeclared columns; HAVING, LIMIT, joins). Every query the rules call conforming is
executed against the expected query — the authority's measure under its required filters
AND the variation the rules identified — on every dataset, exactly; no near miss may
conform, and no legitimate variant may be called a violation. On its first run it found
a real defect: contradictory slices fold a WHERE to `FALSE`, and the rules reported the
required filter as missing — now unverifiable. Current results: 416 conforming verdicts
over 3,328 executions by default (7,506 over 60,048 with `--full`), 0 disagreements, 0
near misses conformed. Agreement on these datasets challenges the rules; it does not
prove them, and whether a slice is *permitted* is policy, tested by the acceptance cases.
`MEASURE()` cannot run in DuckDB; `tests/test_databricks_integration.py` checks it on a
real workspace when `SEMANTIX_DATABRICKS_*` is set (opt-in, not yet run).

### What a certification guarantees, stated narrowly

A `CERTIFIED` verdict means: the definitions' canonical cores are equal at proof
grade; every construct they use is one the core models (anything else is a gap and
refuses); relation identity came from names the SQL wrote or from execution context
the operator supplied (`--default-database` / `--default-schema`), never from a
shared short catalog entry; and the parser was a qualified sqlglot version
(`semantix/toolchain.py` — an unqualified version withholds certification, and a new
one qualifies only through `scripts/qualify_sqlglot.sh`, which runs the suite, the
benchmark and the full oracle). It does not cover semantics the SQL text does not
carry (session time zones, collations, user-defined functions), engine-specific
runtime behaviour beyond the DuckDB oracle, or definitions the scan did not reach. A
receipt answers two separate questions. It is a valid record of the instant it
describes, and it may be relied on for current use only when it is a current receipt
whose freshness check re-hashed every scanned source and found them unchanged. An
`--as-of` receipt is never valid for current use; `verify-receipt --require-current`
and the MCP tool's `current_use` field enforce that.

### The completeness gate

That review exposed a design flaw deeper than any single bug: **the `SemanticCore` is a
whitelist**, so any construct it does not read defaults to *absent*, and two definitions
differing only in an unread construct hash identically. A blind spot became a
certification.

The default is now inverted ([completeness.py](src/semantix/extraction/completeness.py)).
Anything the core cannot represent marks the measure `INCOMPLETE`, which sits outside
`PROOF_LEVELS` — it can still be reported, diffed and blamed, but can never be certified
equal to anything, and the receipt names what could not be read. Constructs leave that
list only by being genuinely modeled (each is now folded into the core: HAVING/QUALIFY
and bounds as tagged filters, grouping constructs in grain, join side and ON-less joins
in the join graph, CTE bodies as a recursive content digest keyed on the body rather
than the name). A gate that never lifts is just a refusal to verify.

## Layout (maps 1:1 to the spec's five layers)

```
src/semantix/
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

**Degradation ladder, never hard failure.** optimized → qualified → parsed → failed,
plus `incomplete` — the completeness gate's verdict on SQL we parsed but cannot
fully model. Artifacts that can't be fully canonicalized still get recorded; equality
below proof grade can't certify (`UNRESOLVED`, not a false `CERTIFIED`). The scan
prints two coverage numbers: how much SQL parsed and qualified, and how many measures
reached proof grade (each unreadable artifact counts as one unverifiable measure). The
second is the one to judge readiness by — the §9 target is >85 %; the demo estate hits
100 %. Coverage used to be the first number alone, which let an estate whose only metric
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
windows, and cost controls is still Phase 1) · the CI bot's core (`semantix diff`
+ `--fail-on-new`; the GitHub App wrapper that posts the comment remains) · the
attestation MCP server (was Phase 2; receipt schema unchanged) · signed receipts ·
write-back certification into Agents Schema (`semantix publish`; DuckDB target now,
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
