# Metric-use drift dataset (synthetic, v1)

Labeled uses of governed metrics, for evaluating an ADVISORY drift classifier
(`touchstone assess benchmark`). This is the business-definition dataset. It is
separate from the engine-correctness counterexamples
(`tests/data/engine_counterexamples.json`): those evaluate how touchstone reads SQL,
and none of them is a drift label.

## How labels were written

Every label comes from **the business definition and policy below and the SQL text**,
never from touchstone's output.

**The labels are model-authored and model-reviewed.** A model wrote them. A separate
model then labeled every item blind (`blind_labels.yaml`), given only this policy, the
definitions and the SQL: no labels, rationales or touchstone output. It agreed on 56 of
60 items. The four disagreements are recorded in `adjudication.yaml`, with the decision
and why; the written policy decided all four for the blind label.

Agreement between two model passes is **not** independent validation. No human has
reviewed these labels yet. The four adjudications and the grouping policy (`time-05`,
`udf-03`, and every `GROUP BY` of a date truncation) need it most. To run a blind human
review:

```bash
touchstone assess review-sheet --out sheet.csv     # no labels, no rationales; priorities flagged
touchstone assess compare-labels sheet.csv         # agreement and every disagreement; changes nothing
```

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

These facts are also stored in `definitions.yaml` as `stated_facts`, beside the catalog
and the policy. Every record carries them, and so does the human review guide (with the
metric view's name), so the model and the human reviewer judge from the same evidence.

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
- **Not a use.** The statement computes something that is not the named metric and does
  not present it as that metric (an order count, a discount total over the same tables).
  A statement that presents its result under the metric's name is a use, whatever it
  computes.
- **Insufficient evidence.** Use this label when the text alone cannot decide the
  question:
  - an unknown function;
  - session state that is not shown;
  - a result that depends on nullability, key uniqueness or referential integrity the
    definitions do not state.

## Labels

`permitted_variation`, `formula_drift`, `population_drift`, `grain_drift`,
`source_drift`, `identity_mismatch`, `insufficient_evidence`, `not_a_use`.

The advisory classes the classifier is scored on are derived from these labels:

- `permitted_variation` becomes `likely_permitted`;
- `insufficient_evidence` stays `insufficient_evidence`, and `not_a_use` stays `not_a_use`;
- every other label becomes `suspected_drift`.

## Splits

Splits are made **by family**, so a classifier tuned on one shape of query is tested on
shapes it has not seen. Thresholds are tuned on `dev` only.

| split | families |
|---|---|
| `dev` | slices, staging_ctes, window_ranks, temp_views |
| `test` | time_windows, dimension_joins, top_n, unknown_udfs, renamed_metrics, non_uses |
