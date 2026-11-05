# Running a monitoring pilot

A pilot answers one question for one estate: **of the metric computations the warehouse
executed in a window, which used an approved definition correctly, which changed it,
and which could not be judged — and why.**

A query-history export alone cannot answer that. It says what ran. It does not say which
definitions were authoritative, what they were when each query ran, or which session
defaults a short table name resolved under. The pilot package supplies those explicitly,
and `touchstone monitor --pilot` refuses a package that leaves one out.

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
      approved_variations:                # optional — kinds of variation the owner approves
        - "IS NOT NULL guards on the measure's own columns"
      approved_time_groupings:            # optional — see "Time groupings" below
        net_revenue: {order_date: [WEEK]} # a measure (or "*"): {date dimension: [units]}
compare_against: as_of_execution          # or: current
snapshots_taken_at: 2026-10-16T09:00:00Z  # optional; shown in `current` mode
catalog: catalog.yml
asset_map: {"dashboard:01ef…": net_revenue}   # optional identity hints
```

Unknown keys are refused, and so is a window whose `from` is not before its `to`.
`compare_against` is required, because the two modes make different claims:

- **`as_of_execution`**: every view needs `applies.from`. Every match is drawn from the
  definitions whose window covers the execution; an expired definition is never
  matched, and versions that never apply at the same time are not "contested". An
  execution no window covers, one with no start time, or one whose start time cannot
  be read is unverifiable, and the report says which. Windows compare at nanosecond
  precision (`applies.to` is exclusive). An approved definition that applies but could
  not be read makes a claim on it unverifiable ("could not be read"), never "missing".
- **`current`**: no windows are needed. Findings are judged against the definitions
  still applicable (no `applies.to`), and every finding says "compared against the
  definition snapshot taken …; not verified as of execution time".

The `window` is applied: executions outside it are not judged, and are counted in the
report (`executions_outside_window`), so an export that does not match its package
shows itself.

## Approved variations

`approved_variations` lists, in the owner's own words, kinds of difference from the
definition that the owner accepts for this metric: for example "IS NOT NULL guards on the
measure's own columns", "inner joins along the declared joins", or "output names that
qualify the metric's name (eu_revenue, revenue_1996)". The advisory judge
(`touchstone assess run`) receives them with the definition and treats each as
permitted. They are the owner's decision, not touchstone's: the default stays strict
(a statement must equal the definition for every possible data). At most 20 texts of
300 characters. On Databricks Genie's SQL, three such lines removed four of five false
alarms ([assess.md](assess.md), "A live test").

## Time groupings

A declared date dimension authorizes grouping by that dimension **as declared**, not by
every transformation of it. In the snapshot above, `order_date` and `month` (=
`DATE_TRUNC('MONTH', order_date)`) are declared, so grouping by either is a permitted
variation. Grouping by `DATE_TRUNC('WEEK', order_date)` or `DATE_TRUNC('QUARTER', …)`
is outside the governed model: the use is unverifiable (`grouping_undeclared`) and the
finding says how to approve it.

When a time grain is governed for a metric, approve it explicitly, per metric:

```yaml
approved_time_groupings:
  net_revenue: {order_date: [WEEK, QUARTER]}   # for net_revenue only
  "*": {order_date: [DAY]}                     # for every measure of this view
```

- An approval names a measure of the view (or `*`), a plain declared column dimension,
  and units from YEAR, QUARTER, MONTH, WEEK, DAY, HOUR, MINUTE, SECOND, MILLISECOND and
  MICROSECOND (or a spelling the dialect's DATE_TRUNC accepts for one, such as `MM`).
  Anything else is refused at load, and so is a measure or dimension listed twice — in
  any case, and as an exact duplicate key (every YAML file touchstone reads — pilot,
  catalog, snapshots, the benchmark dataset and criteria — refuses a key written twice
  in one mapping, which YAML would otherwise silently drop; an explicit key overriding
  a `<<:` merge is not a duplicate).
- A unit is read **as written, the way the engine reads it** (case-insensitive, never
  trimmed). On Databricks, `DATE_TRUNC` accepts `YEAR`/`YYYY`/`YY`, `QUARTER`,
  `MONTH`/`MM`/`MON`, `WEEK`, `DAY`/`DD`, `HOUR`, `MINUTE`, `SECOND`, `MILLISECOND` and
  `MICROSECOND`, and `TRUNC(d, unit)` only YEAR to WEEK. Any other spelling (`WEEKS`,
  `MONTHS`, `QTR`, `W`, `' month '`, `TRUNC(d, 'DD')`) returns NULL — one grand-total
  group, not the grain — and an unquoted unit names a column, so the statement is
  unverifiable, never read as the grain, not even when the rest equals the definition.
  DuckDB accepts its own spellings (`weeks`, `mon`, `yearweek`; not `MM`, `YYYY` or
  `DD` — and a one-letter `'w'` the parser rewrites, so such a statement is counted
  unreadable); Snowflake its documented date parts, quoted (`'mm'`, `'wk'`, also in
  `TRUNC(d, 'month')`); BigQuery its granularities as unquoted keywords
  (`DATE_TRUNC(d, MONTH)`; its `WEEK` starts on Sunday). A documented unit that is no
  grain this rule set models (BigQuery `ISOWEEK`, `WEEK(MONDAY)`; DuckDB `decade`), or
  a parameter (`DATE_TRUNC(:grain, d)`), is refused as exactly that. Another dialect
  gets only the full unit names, quoted. This holds wherever the truncation is — of a
  column or of an expression (`DATE_TRUNC('Y', CURRENT_DATE())`) — and on the
  definition side too: a metric view whose own text uses a unit Databricks rejects has
  that expression marked unsupported, and a SQL model written so is below proof grade.
- A truncation of a dimension that is itself a truncation (`DATE_TRUNC('MONTH',
  order_week)`) is never the declared grain and cannot be approved: a week that starts
  in September is credited to September.
- On a query over the metric view, the approval is checked per measure, so one query
  can be inside the model for one measure and outside it for another. A grain approved
  on any plain declared dimension of the same column counts.
- In a **filter**, a truncation of a declared date column is a time window on it, for
  example `DATE_TRUNC('WEEK', order_date) = DATE '2026-10-05'`. It is a permitted slice
  and needs no approval.
- Approvals are recorded in every exported comparison record
  (`definition.variation_policy`), so a reviewer sees the policy the use was judged
  under.

## Steps

1. **Export history.** Run [scripts/export_databricks_history.sql](../scripts/export_databricks_history.sql)
   for the window, download the result as CSV, and save it as `history.csv`. Confirm the
   column names first with `DESCRIBE TABLE system.query.history`. Columns touchstone does
   not read are listed in the report, not silently used. Timestamps are compared as UTC.
2. **Snapshot the approved views.** For each view, take the YAML body of its definition
   (for example, from `SHOW CREATE TABLE main.finance.revenue_mv`, the text between the
   `$$` markers) and save it under `views/`. Record its hash with
   `shasum -a 256 views/revenue_mv.yaml`, or leave `sha256` empty once and the loader
   prints it.
3. **Catalog.** Write `catalog.yml` for the tables the views read, or generate it with
   `touchstone catalog --warehouse …`.
4. **Scan** the snapshots together with the estate's models, so the authority the
   monitor cites is the one the scan verified:
   ```bash
   touchstone scan --dbt my_project --uc-metrics pilot/views --catalog pilot/catalog.yml \
     --dialect databricks --db pilot.db
   ```
   The monitor refuses if the latest scan did not read each snapshot measure
   byte-for-byte.
5. **Monitor:**
   ```bash
   touchstone monitor --pilot pilot/ --dbt my_project --db pilot.db --json findings.json
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

**Session state is reconstructed only from evidence.** What a short name reads can be
changed by statements the session ran earlier ([session.py](../src/touchstone/services/session.py)).
The rules:

- **What each statement can change.**
  - `USE`, `SET`, and an unparseable or unrecognized statement can change every
    *short* name.
  - `CREATE`, `DROP`, `ALTER` and `CACHE` can change the names they touch: a temp view
    `orders` shadows the short reference `orders`.
  - `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `SHOW`, `DESCRIBE`, `EXPLAIN` and `COMMENT`
    change nothing a name resolves to.
  - Fully qualified names do not depend on `USE` or `SET`.
- **When context is reconstructed.** Only for a `USE` earlier in the *same execution*,
  where the order is certain: `USE SCHEMA staging; SELECT … FROM orders` reads
  `<catalog>.staging.orders`.
- **When identity is uncertain.** Any other event that may precede a statement makes
  its relation identity depend on the session, and the statement is unverifiable. That
  covers an event in another execution of the same session (`session_id`), and one in
  an execution whose session the export does not record and that is not provably later
  by `start_time`. The same applies to a bare `.sql` log, where order is known but
  sessions are not.
- **Counts.** The report shows `session_events`, `statements_after_session_change` and
  `statements_with_reconstructed_context`.

Export every statement type with `session_id`, as
[export_databricks_history.sql](../scripts/export_databricks_history.sql) does. A
SELECT-only export hides exactly the statements that change a session. An asserted
`execution_context` is your statement that nothing outside the export changed these
names either.

**Identity.** A query is judged as the metric it claims. A `-- touchstone: metric=<name>`
comment names only the statement it is written on; a tag on the same line after a
statement's semicolon belongs to that statement. Query tags may be `metric=<name>`,
`metric: <name>` or JSON (`{"metric": "<name>"}`). A query tag or an `asset_map` entry
belongs to the execution and names a metric only when the execution holds one query.
Otherwise the projection's own alias is the claim, including aliases that look
generated (`sum_revenue`) when the analyst wrote them.

## Reading the report

**Positive verdicts are suspended** ([proof-policy.md](proof-policy.md)): what the rules
would call conforming or contested is reported as an *experimental match* (one reason a
use is unverifiable), and what they would call a violation is a *suspected
difference*. Nothing in the report is a conclusion.

Every measure use lands in exactly one bucket: suspected difference, candidate,
unregistered, unverifiable (including experimental matches), or below the recurrence
threshold. A use's
bucket is its most severe finding, judged as the metric the query claims to compute
(its alias, an explicit `-- touchstone: metric=` tag, or the asset map); a calculation
that matches a *different* approved metric is evidence on that finding, not a second
one. Judgment runs in a fixed order: definition evidence (for the referenced and the
claimed metric), then the supported-SQL boundary (shape, filters, groupings), then the
claimed metric and the projection's formula. If an earlier step fails, the use is
unverifiable, and anything that would have been a violation — a renamed or transformed
measure, a dropped filter — is kept as `suspected` context, never promoted to a verdict. Findings are merged only when their
verdicts are identical, so each carries only its own executions. Each bucket is shown as
a share of all measure uses. Governed references (`MEASURE()` over an
approved view) are counted alongside, never instead. Unverifiable uses are broken down
by reason, which shows where the next bottleneck is:

| Most unverifiable uses are… | The next step is… |
| --- | --- |
| execution context | per-row catalog/schema in the export, or an honest assertion |
| unsupported shape | widening the proof grammar or the rule set (joins, LIMIT, windows — see [proof-policy.md](proof-policy.md)) |
| unsupported dimension | supporting more dimension expressions |
| no definition evidence | definition windows that cover the export |
| below proof grade | catalog coverage, or constructs the core does not model |

## What an experimental match does and does not say

It is not a conclusion while positive verdicts are suspended. It says that, on the tree
sqlglot parsed, the query matched an authoritative definition exactly, or matched one of
the shapes listed in rule set `monitor-conformance/1` ([conformance.py](../src/touchstone/services/conformance.py)).
Both observations and definitions are read through the proof grammar of
[`touchstone-proof/2`](proof-policy.md). A select-project-filter CTE or derived table is
inlined by a stated rule and judged as the single query it is. SQL outside the grammar
is unverifiable, and its optimizer reading appears only as `suspected` context.
It says nothing beyond that rule set. Excluded from the claim:

- joins and joined dimensions
- window measures
- LIMIT, HAVING, QUALIFY and DISTINCT
- TABLESAMPLE and time travel
- CTEs that aggregate, and subqueries in expressions
- unsupported dimension expressions
- Databricks runtime behaviour of `MEASURE()` beyond its declared definition

`touchstone oracle` executes every match the rules produce for its generated cases against
the definition plus the identified variation. That challenges the rules on its datasets but
does not prove them. `MEASURE()` runtime behaviour is checked by
`tests/test_databricks_integration.py` against a real workspace, which is opt-in via
`TOUCHSTONE_DATABRICKS_*` and has not yet been run.
