# touchstone-dbt

Gate `dbt build` on semantic findings — without a new alert channel.
`touchstone publish` projects each scan's verdicts into a `touchstone.verdicts`
table in your warehouse; this package turns those rows into ordinary dbt
tests, so suspected divergences block builds, show up in dbt Cloud / Elementary /
orchestrator alerting you already run, and stay red until they are addressed.

> **Positive verdicts are suspended.** Repeated reviews found false
> certifications, including SQL the parser reads differently from the engines
> (`docs/proof-policy.md` in the main repo). Until the re-enable criteria hold,
> nothing is certified: `certified` always fails with that reason, and
> `no_contradictions` gates on *suspected* divergences, which rest on an SQL
> interpretation that is not verified. Tables published before the suspension
> still hold old `CERTIFIED` rows; run `touchstone publish --refresh-policy
> --warehouse <target>` once to bring them under the policy.

## Install

```yaml
# packages.yml — pin a tag, like any dependency
packages:
  - git: "<your touchstone repo url>"
    subdirectory: integrations/dbt/touchstone
    revision: main            # or a tag once one exists — `git tag` is empty today
```

Run the pipeline in this order: `touchstone scan` → `touchstone publish
--warehouse <target>` → `dbt test`.

## Use

```yaml
models:
  - name: fct_orders
    data_tests:
      - touchstone.no_contradictions:          # any suspected divergence touching this relation
          severity: error
      - touchstone.certified:                  # always fails while positive verdicts are suspended
          concept: net_revenue
          severity: warn
      - touchstone.fresh_scan:                 # gating on stale verdicts is gating on nothing
          max_hours: 24
          severity: warn
```

If the projection lives outside the target database, point the package at it:

```yaml
vars:
  touchstone_database: analytics
  touchstone_schema: touchstone
```

## Semantics worth knowing

- **State, not events.** A test stays red while the finding stands — the
  triage model of a failing uniqueness test, not a paged alert.
- **`certified` fails closed.** While positive verdicts are suspended it fails
  for every concept, whatever the projection holds, with the suspension as its
  reason. An existing attachment fails loudly instead of passing.
- **`no_contradictions` is a triage gate on a suspicion.** It fires on
  `SUSPECTED_DIVERGENCE` (and on `CONTRADICTION` rows a pre-suspension table
  may still hold). A row is a reason to look at the SQL, not a verified
  finding.
- **`fresh_scan` keeps the gate honest.** Verdicts describe a moment; this
  test fails inside the same rail when that moment is too old to gate on.
- Monitor (query-stream) findings join the projection once monitor
  persistence lands; the tests will not need to change.

Not on dbt? The same table gates through warehouse alerts, Dagster asset
checks, Soda/Great Expectations, or plain cron — see `docs/action-rails.md`
in the main repo.
