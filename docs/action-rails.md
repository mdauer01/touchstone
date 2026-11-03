# Action rails: gating on findings without a new alert channel

touchstone deliberately does not grow its own alerting. Detection that lands in
a fresh inbox becomes one more signal nobody triages; detection that lands in
a rail the team already gates on becomes action. `touchstone publish` therefore
projects every scan's verdicts into a structured warehouse table:

    touchstone.verdicts (scan_id, scanned_at, published_at,
                       concept, verdict, severity, summary, relation)

One row per verdict per affected relation (relation is NULL when a concept
touches none). The table is a wholesale-replaced projection of the
append-only store — read it, never write it. Everything below is the same
one-line query dressed for a different rail.

The doctrinal line all rails share: touchstone routes, proposes, and gates —
it never auto-decides which side of a contradiction is the truth. That
decision belongs to the owning human; the bitemporal store then records the
resolution as a definition change.

## Propose: resolutions in the code-review rail

```bash
touchstone propose --db touchstone.db --out proposals/ [--format metrics|ossie|uc-metrics]
gh pr create --body-file proposals/RESOLUTIONS.md   # the memo is the PR body
```

For each suspected divergence: **Option A** — a declaration file emitted from what
the code computes today, in the ontology's format (the ontology drifted);
**Option B** — the component diff describing the code change that would
match the declaration instead (the code drifted). For each SHADOW_METRIC: the
missing declaration, drafted from the fingerprint — accept it or delete the
model. Candidates the formats cannot express (join graphs, post-aggregate
filters, filtered metrics in Ossie 0.1.x) are refused with the reason in the
memo. Nothing is applied and no PR is opened by the tool: a proposal that
applied itself would be the verifier deciding meaning.

## dbt (the richest rail)

Use the shipped package — `integrations/dbt/touchstone` — for generic tests
(`no_contradictions`, `certified`, `fresh_scan`) with dbt-native severity.
`no_contradictions` fails on suspected divergences (a triage gate on a suspicion);
`certified` fails closed while positive verdicts are suspended
([proof-policy.md](proof-policy.md)). After upgrading, run
`touchstone publish --refresh-policy --warehouse …` so tables published earlier carry
the policy (`usable_for_reliance = false`) before any rail reads them.
Failing tests block `dbt build` and flow into dbt Cloud, Elementary, and
anything else that already consumes test results.

## Warehouse-native alerts

Snowflake:

```sql
CREATE ALERT touchstone_contradictions
  WAREHOUSE = alerting_wh
  SCHEDULE = '60 MINUTE'
  IF (EXISTS (SELECT 1 FROM touchstone.verdicts WHERE verdict IN ('SUSPECTED_DIVERGENCE', 'CONTRADICTION')))
  THEN CALL SYSTEM$SEND_EMAIL(...);  -- or a notification integration
ALTER ALERT touchstone_contradictions RESUME;  -- alerts are created SUSPENDED
```

Databricks: a SQL Alert on the same SELECT, routed to the workspace's
existing email/Slack/webhook destinations. BigQuery: a scheduled query or a
Dataform assertion over the table (`scanned_at` is a TIMESTAMP; compare it
against `CURRENT_TIMESTAMP()`, not a DATETIME).

Two DuckDB notes: name the warehouse file anything but `agents.duckdb` or
`touchstone.duckdb`, or qualify with the catalog (`"agents".agents.root`) —
DuckDB otherwise reports the bare `agents.root` as ambiguous. URL forms are
`duckdb://relative/path.duckdb` and `duckdb:///absolute/path.duckdb`.

## Orchestrators

Dagster — verdicts are relation-keyed, so they map onto asset checks:

```python
@asset_check(asset=fct_orders)
def no_semantic_contradictions(context) -> AssetCheckResult:
    # Relations are stored as the scan saw them — three-part on a dbt
    # project with a database, two-part otherwise — so match both forms.
    rows = warehouse.query(
        "SELECT concept, summary FROM touchstone.verdicts "
        "WHERE verdict IN ('SUSPECTED_DIVERGENCE', 'CONTRADICTION') "
        "AND (lower(relation) = 'prod.analytics.fct_orders' "
        "     OR lower(relation) = 'analytics.fct_orders')")
    return AssetCheckResult(passed=not rows,
                            metadata={"findings": [r[1] for r in rows]})
```

Airflow / Prefect / cron: run the CLI as a task — the exit code is the
integration.

```bash
touchstone scan --dbt project/ --catalog catalog.yml --fail-on contradiction
touchstone diff --fail-on-new contradiction     # PR pipelines: only NEW findings fail
```

## Data-quality frameworks

Soda:

```yaml
checks for touchstone.verdicts:
  - failed rows:
      name: No semantic contradictions
      fail query: |
        SELECT concept, severity, summary
        FROM touchstone.verdicts WHERE verdict IN ('SUSPECTED_DIVERGENCE', 'CONTRADICTION')
```

Great Expectations: `unexpected_rows_expectation` with the same query.

## Runtime (agents)

Already in-rail with no configuration: agents attesting through the MCP
server never get a usable claim while positive verdicts are suspended
(`semantic_claim_usable: false`, reported beside `signature_authentic` and
`estate_unchanged`), and signed receipts in `agents.root` carry an
`assessment/<concept>` row saying so. Publish also on
a schedule so the projection the rails read stays current — and let the
`fresh_scan` test (or an alert on `scanned_at`) fail loudly when it does not.
