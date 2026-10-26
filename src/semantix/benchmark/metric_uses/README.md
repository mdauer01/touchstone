# Metric-use drift dataset (synthetic, v1)

Labeled uses of governed metrics, for evaluating an ADVISORY drift classifier
(`semantix assess benchmark`). This is the business-definition dataset. It is
separate from the engine-correctness counterexamples
(`tests/data/engine_counterexamples.json`): those evaluate how semantix reads SQL,
and none of them is a drift label.

## How labels were written

Every label comes from **the business definition and policy below and the SQL text**,
never from semantix's output. A second, blind pass labeled every item again
(`blind_labels.yaml`). It was made by a separate agent that was given only this policy,
the definitions and the SQL: no labels, no rationales, no semantix output. It agreed on
56 of 60 items. Each of the four disagreements is recorded in `adjudication.yaml` with
the decision and why. In all four, the written policy decided for the blind label.

## The business definitions

Metric view `main.finance.revenue_mv` over `main.finance.orders`. Every measure of the
view has the required filter `status = 'completed'`.

| metric | definition |
|---|---|
| `net_revenue` | `SUM(amount) - SUM(refund_amount)`: completed orders' amounts, less their refunds |
| `gross_revenue` | `SUM(amount)`: completed orders' amounts |

Declared dimensions: `country`, `channel`, `order_date`, `month` (=
`DATE_TRUNC('MONTH', order_date)`).

Columns: `orders(order_id, customer_id, amount DOUBLE, refund_amount DOUBLE,
discount DOUBLE, status, country, channel, order_date DATE)`,
`customers(customer_id, segment, region)`, and `order_items(order_id, sku, qty)`.
`refund_amount` is nullable. Key uniqueness and referential integrity are **not**
stated.

## The policy (what is a permitted variation)

- **Filters.** Filtering on a declared dimension is permitted, including time windows
  on `order_date` or `month`. A filter on any other column changes the population.
- **Grouping.** Grouping by declared dimensions exactly as declared is permitted.
  Ranking, ordering or limiting the groups is also permitted, as long as each group's
  value is unchanged. Grouping by anything else changes the grain, and so does
  returning the value once per row.
- **Required filter.** The required filter must hold. Dropping, weakening, broadening
  or replacing it changes the population.
- **Formula.** The formula is the declared one: same aggregation, same columns, same
  arithmetic. A cast that cannot change a DOUBLE's value is the same formula.
  Cumulative, ratio or scaled versions are a different formula.
- **Source.** The source is `main.finance.orders`, read directly or through
  select-project-filter steps. Another relation, or a join that can change which rows
  are counted (fan-out, dropped rows), changes the source.
- **Identity.** Presenting one metric's definition under another metric's name is an
  identity mismatch, even when the math is a correct definition of some metric.
- **Insufficient evidence.** Use this label when the text alone cannot decide the
  question:
  - an unknown function;
  - session state that is not shown;
  - a result that depends on nullability, key uniqueness or referential integrity the
    definitions do not state.

## Labels

`permitted_variation`, `formula_drift`, `population_drift`, `grain_drift`,
`source_drift`, `identity_mismatch`, `insufficient_evidence`.

The advisory classes the classifier is scored on are derived from these labels:

- `permitted_variation` becomes `likely_permitted`;
- `insufficient_evidence` stays `insufficient_evidence`;
- every other label becomes `suspected_drift`.

## Splits

Splits are made **by family**, so a classifier tuned on one shape of query is tested on
shapes it has not seen. Thresholds are tuned on `dev` only.

| split | families |
|---|---|
| `dev` | slices, staging_ctes, window_ranks, temp_views |
| `test` | time_windows, dimension_joins, top_n, unknown_udfs, renamed_metrics |
