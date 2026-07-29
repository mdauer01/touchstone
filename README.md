# semantix

**A semantic verification engine.** semantix extracts the de facto semantics of a data
estate from compiled SQL, reconciles them against the declared ontology, and certifies —
or contradicts — every metric definition with evidence.

Ranking the most *trusted* definition of a metric is not the same as verifying that the
definition is *correct*. The compiled SQL is the de facto ontology of the business;
semantix reads it and diffs it against what the organization claims its metrics mean.

- Concept: [docs/concept-one-pager.md](docs/concept-one-pager.md)
- Architecture: [docs/architecture-spec.md](docs/architecture-spec.md)

This repository implements **Phase 0** of the architecture spec (§7): adapters (dbt,
raw SQL directories, view-DDL dumps) → extraction & canonicalization → bitemporal
fingerprint store → concept clustering → static reconciliation with **clause-level
blame** → HTML report — plus the Phase 1/2 surfaces that were cheap to pull forward:
derived valid-time history, as-of queries, restatement detection, a **DuckDB execution
slice** that attaches measured dollar divergence to contradictions, **Ed25519-signed
semantic receipts** (`attest` / `verify-receipt`), a scan-over-scan **`diff`** for CI
(comment only newly introduced verdicts), an **attestation MCP server** so agents check
the receipt before quoting a number, and an **open equivalence benchmark** (`bench`).

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

### The bitemporal store in action

```bash
# change a model's SQL, rescan into the same db, then:
.venv/bin/semantix history net_revenue --db out/semantix.db   # valid-time intervals per site
.venv/bin/semantix asof net_revenue --at 2026-06-01T00:00:00Z --db out/semantix.db
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
`--agents-schema`) and it re-hashes them against the scan's recorded snapshot before
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
      1:N, parent key analytics.fct_orders.order_id — 2 orphan value(s) violate referential integrity
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
nobody joins it. In the demo above, the payments↔orders join *six dashboards rely on* is
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
# spec §7 Phase 2), plus a skill/verify-metric row teaching agents the check.
.venv/bin/semantix publish --db out/semantix.db --warehouse out/agents.duckdb
```

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

### Prove the prover — across dialects

```bash
.venv/bin/semantix bench
# equivalence corpus: 26 pairs · dialects: duckdb, databricks, snowflake, bigquery · proof-grade coverage 100.0%
#     duckdb      9/9 detected · near-misses 15/15 · coverage 100.0% · false certs 0
#     databricks  9/9 detected · near-misses 15/15 · coverage 100.0% · false certs 0
#     snowflake   9/9 detected · near-misses 15/15 · coverage 100.0% · false certs 0
#     bigquery    9/9 detected · near-misses 15/15 · coverage 100.0% · false certs 0
#   equivalence detection: 36/36 (100%)
#   known gaps: 2 confirmed
#   near-misses distinguished: 60/60
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
                        metrics YAML, catalog + declared keys/relationships    (§2)
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
prints canonicalization coverage — the §9 target is >85 %; the demo estate hits 100 %.

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
declared source: OSI + LookML).

**Still open:** warehouse query-history adapter · telemetry-driven severity ·
concept-resolution overrides (lineage-overlap clustering first; embedding similarity
only as candidate generation — consensus signals must never decide a verdict, that is
the failure mode this engine exists to correct) · IN-list operand normalization (the
benchmark's one confirmed gap) · change-attribution service · live warehouse
connectors for Agents Schema read/write.
