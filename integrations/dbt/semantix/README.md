# semantix-dbt

Gate `dbt build` on semantic findings — without a new alert channel.
`semantix publish` projects each scan's verdicts into a `semantix.verdicts`
table in your warehouse; this package turns those rows into ordinary dbt
tests, so contradictions block builds, show up in dbt Cloud / Elementary /
orchestrator alerting you already run, and stay red until resolved.

## Install

```yaml
# packages.yml — pin a tag, like any dependency
packages:
  - git: "<your semantix repo url>"
    subdirectory: integrations/dbt/semantix
    revision: v0.1.0
```

Run the pipeline in this order: `semantix scan` → `semantix publish
--warehouse <target>` → `dbt test`.

## Use

```yaml
models:
  - name: fct_orders
    data_tests:
      - semantix.no_contradictions:          # any contradiction touching this relation
          severity: error
      - semantix.certified:                  # this concept must be affirmatively certified
          concept: net_revenue
          severity: error
      - semantix.fresh_scan:                 # gating on stale verdicts is gating on nothing
          max_hours: 24
          severity: warn
```

If the projection lives outside the target database, point the package at it:

```yaml
vars:
  semantix_database: analytics
  semantix_schema: semantix
```

## Semantics worth knowing

- **State, not events.** A test stays red while the finding stands — the
  triage model of a failing uniqueness test, not a paged alert.
- **`certified` fails closed.** No verdict data for the concept is a
  failure, not a pass; so is a standing contradiction beside a certification.
- **`fresh_scan` keeps the gate honest.** Verdicts describe a moment; this
  test fails inside the same rail when that moment is too old to gate on.
- Monitor (query-stream) findings join the projection once monitor
  persistence lands; the tests will not need to change.

Not on dbt? The same table gates through warehouse alerts, Dagster asset
checks, Soda/Great Expectations, or plain cron — see `docs/action-rails.md`
in the main repo.
