# Action rails: gating on findings without a new alert channel

semantix deliberately does not grow its own alerting. Detection that lands in
a fresh inbox becomes one more signal nobody triages; detection that lands in
a rail the team already gates on becomes action. `semantix publish` therefore
projects every scan's verdicts into a structured warehouse table:

    semantix.verdicts (scan_id, scanned_at, published_at,
                       concept, verdict, severity, summary, relation)

One row per verdict per affected relation (relation is NULL when a concept
touches none). The table is a wholesale-replaced projection of the
append-only store — read it, never write it. Everything below is the same
one-line query dressed for a different rail.

The doctrinal line all rails share: semantix routes, proposes, and gates —
it never auto-decides which side of a contradiction is the truth. That
decision belongs to the owning human; the bitemporal store then records the
resolution as a definition change.

## Propose: resolutions in the code-review rail

```bash
semantix propose --db semantix.db --out proposals/ [--format metrics|ossie|uc-metrics]
gh pr create --body-file proposals/RESOLUTIONS.md   # the memo is the PR body
```

For each CONTRADICTION: **Option A** — a declaration file emitted from what
the code computes today, in the ontology's format (the ontology drifted);
**Option B** — the component diff describing the code change that would
match the declaration instead (the code drifted). For each SHADOW_METRIC: the
missing declaration, drafted from the fingerprint — accept it or delete the
model. Candidates the formats cannot express (join graphs, post-aggregate
filters, filtered metrics in Ossie 0.1.x) are refused with the reason in the
memo. Nothing is applied and no PR is opened by the tool: a proposal that
applied itself would be the verifier deciding meaning.

## dbt (the richest rail)

Use the shipped package — `integrations/dbt/semantix` — for generic tests
(`no_contradictions`, `certified`, `fresh_scan`) with dbt-native severity.
Failing tests block `dbt build` and flow into dbt Cloud, Elementary, and
anything else that already consumes test results.

## Warehouse-native alerts

Snowflake:

```sql
CREATE ALERT semantix_contradictions
  WAREHOUSE = alerting_wh
  SCHEDULE = '60 MINUTE'
  IF (EXISTS (SELECT 1 FROM semantix.verdicts WHERE verdict = 'CONTRADICTION'))
  THEN CALL SYSTEM$SEND_EMAIL(...);  -- or a notification integration
```

Databricks: a SQL Alert on the same SELECT, routed to the workspace's
existing email/Slack/webhook destinations. BigQuery: a scheduled query or a
Dataform assertion over the table.

## Orchestrators

Dagster — verdicts are relation-keyed, so they map onto asset checks:

```python
@asset_check(asset=fct_orders)
def no_semantic_contradictions(context) -> AssetCheckResult:
    rows = warehouse.query(
        "SELECT concept, summary FROM semantix.verdicts "
        "WHERE verdict = 'CONTRADICTION' "
        "AND lower(relation) = 'analytics.fct_orders'")
    return AssetCheckResult(passed=not rows,
                            metadata={"findings": [r[1] for r in rows]})
```

Airflow / Prefect / cron: run the CLI as a task — the exit code is the
integration.

```bash
semantix scan --dbt project/ --catalog catalog.yml --fail-on contradiction
semantix diff --fail-on-new contradiction     # PR pipelines: only NEW findings fail
```

## Data-quality frameworks

Soda:

```yaml
checks for semantix.verdicts:
  - failed rows:
      name: No semantic contradictions
      fail query: |
        SELECT concept, severity, summary
        FROM semantix.verdicts WHERE verdict = 'CONTRADICTION'
```

Great Expectations: `unexpected_rows_expectation` with the same query.

## Runtime (agents)

Already in-rail with no configuration: agents attesting through the MCP
server are refused stale or contested certifications, and signed receipts in
`agents.root` decay honestly (`verified_as_of` + freshness). Publish also on
a schedule so the projection the rails read stays current — and let the
`fresh_scan` test (or an alert on `scanned_at`) fail loudly when it does not.
