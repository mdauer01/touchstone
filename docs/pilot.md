# Running a monitoring pilot

A pilot answers one question for one estate: **of the metric computations the warehouse
executed in a window, which used an approved definition correctly, which changed it,
and which could not be judged — and why.**

A query-history export alone cannot answer that. It says what ran. It does not say which
definitions were authoritative, what they were when each query ran, or which session
defaults a short table name resolved under. The pilot package supplies those explicitly,
and `semantix monitor --pilot` refuses a package that leaves one out.

## Package layout

```
pilot/
  pilot.yml            the manifest (below)
  history.csv          executed statements (.csv, .jsonl or .sql)
  catalog.yml          column catalog for the tables the views read
  views/
    revenue_mv.yaml    one snapshot per approved metric view (the YAML body)
```

```yaml
history: history.csv
window: {from: 2026-10-01T00:00:00Z, to: 2026-10-15T00:00:00Z}
execution_context:                        # optional
  catalog: main
  schema: finance
  asserted_for_all_executions: true       # only if it is true of EVERY execution
authority:
  system: uc_metrics
  metric_views:
    - name: main.finance.revenue_mv       # fully qualified — refused otherwise
      snapshot: views/revenue_mv.yaml
      sha256: 3f1c…                        # of the snapshot file — refused on mismatch
      applies: {from: 2026-09-01T00:00:00Z, to: null}
compare_against: as_of_execution          # or: current
snapshots_taken_at: 2026-10-16T09:00:00Z  # optional; shown in `current` mode
catalog: catalog.yml
asset_map: {"dashboard:01ef…": net_revenue}   # optional identity hints
```

Unknown keys are refused. `compare_against` is required, because the two modes make
different claims:

- **`as_of_execution`**: every view needs `applies.from`. An execution whose
  `start_time` falls outside its definition's window, or that has no start time, is
  unverifiable ("no definition evidence for execution time").
- **`current`**: no windows are needed, and every finding says "compared against the
  definition snapshot taken …; not verified as of execution time".

## Steps

1. **Export history.** Run [scripts/export_databricks_history.sql](../scripts/export_databricks_history.sql)
   for the window, download the result as CSV, and save it as `history.csv`. Confirm the
   column names first with `DESCRIBE TABLE system.query.history`. Columns semantix does
   not read are listed in the report, not silently used. Timestamps are compared as UTC.
2. **Snapshot the approved views.** For each view, take the YAML body of its definition
   (for example, from `SHOW CREATE TABLE main.finance.revenue_mv`, the text between the
   `$$` markers) and save it under `views/`. Record its hash with
   `shasum -a 256 views/revenue_mv.yaml`, or leave `sha256` empty once and the loader
   prints it.
3. **Catalog.** Write `catalog.yml` for the tables the views read, or generate it with
   `semantix catalog --warehouse …`.
4. **Scan** the snapshots together with the estate's models, so the authority the
   monitor cites is the one the scan verified:
   ```bash
   semantix scan --dbt my_project --uc-metrics pilot/views --catalog pilot/catalog.yml \
     --dialect databricks --db pilot.db
   ```
   The monitor refuses if the latest scan did not read each snapshot measure
   byte-for-byte.
5. **Monitor:**
   ```bash
   semantix monitor --pilot pilot/ --dbt my_project --db pilot.db --json findings.json
   ```

## Session context

The published Databricks history schema has no per-query default catalog or schema.

- A query that names its tables in full resolves on its own.
- A query using short names resolves only from its own row's `catalog`/`schema`
  columns, or from an `execution_context` the package **asserts** for every execution.
- Otherwise it is **unverifiable**, never "conforming with a warning".

Assert a context only when it holds for the whole export, for example when every
dashboard in the window runs against one catalog and schema. The report states whether
a context was asserted.

## Reading the report

Every measure use lands in exactly one bucket: conforming, contested, violation,
candidate, unregistered, unverifiable, or below the recurrence threshold. A use's
bucket is its most severe finding, judged as the metric the query claims to compute
(its alias, an explicit `-- semantix: metric=` tag, or the asset map); a calculation
that matches a *different* approved metric is evidence on that finding, not a second
one. Findings are merged only when their verdicts are identical, so each carries only
its own executions. Each bucket is shown as a share of all measure uses. Governed references (`MEASURE()` over an
approved view) are counted alongside, never instead. `checked = conforming +
violations` is the share the rules could decide. Unverifiable uses are broken down
by reason, which shows where the next bottleneck is:

| Most unverifiable uses are… | The next step is… |
| --- | --- |
| execution context | per-row catalog/schema in the export, or an honest assertion |
| unsupported shape | widening the rule set (joins, CTE-wrapped slices) |
| unsupported dimension | supporting more dimension expressions |
| no definition evidence | definition windows that cover the export |
| below proof grade | catalog coverage, or constructs the core does not model |

## What a conforming verdict does and does not say

It says the query matched an authoritative definition exactly, or matched one of the
shapes listed in rule set `monitor-conformance/1` ([conformance.py](../src/semantix/services/conformance.py)).
It says nothing beyond that rule set. Excluded from the claim:

- joins and joined dimensions
- window measures
- LIMIT, HAVING, QUALIFY and DISTINCT
- TABLESAMPLE and time travel
- CTEs and subqueries around a variation
- unsupported dimension expressions
- Databricks runtime behaviour of `MEASURE()` beyond its declared definition

`semantix oracle` executes every conforming verdict it can generate against the
definition plus the identified variation. That challenges the rules on its datasets but
does not prove them. `MEASURE()` runtime behaviour is checked by
`tests/test_databricks_integration.py` against a real workspace, which is opt-in via
`SEMANTIX_DATABRICKS_*` and has not yet been run.
