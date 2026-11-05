# Labeling the v4 fresh test (blind)

Open `review_sheet.csv` in a spreadsheet. Each row is one distinct statement (the same
SQL run in several places is one row; `where_it_ran` lists every place). Fill in four
columns per row, from the SQL and the definition below only. Do not open the model's
verdicts (`out/v4_fresh/verdicts.SEALED.jsonl`) before you finish.

## The governed metric: `revenue` (workspace.touchstone_tpch.revenue_mv)

- **Source:** `samples.tpch.lineitem`, with declared LEFT joins to orders → customer →
  nation → region.
- **Required filter:** `l_returnflag <> 'R'` (returned line items are not revenue).
- **Measure:** `revenue = SUM(l_extendedprice * (1 - l_discount))`; also
  `gross_sales = SUM(l_extendedprice)`.
- **Declared dimensions:** ship_date (`l_shipdate`), ship_month, ship_mode,
  order_priority, market_segment, nation and region (the customer's).

## The owner's rules

- **A1 (approved):** IS NOT NULL conditions on the measure's own input columns.
- **A2 (approved):** INNER JOIN instead of the declared LEFT joins, along the declared keys.
- **A3 (approved):** output names that qualify the metric's name (eu_revenue, revenue_1996).
- **A4 (approved):** revenue by part brand or part type, joining part on l_partkey.
- **P1 (prohibited):** revenue by supplier nation or supplier region.
- **P2 (prohibited):** revenue for one named customer.

## The four columns

| column | values |
|---|---|
| `what_changed` | one or more of `nothing`, `calculation`, `eligibility` (counts rows the definition excludes), `source_rows` (a join that repeats or drops rows, de-duplication, another table), `declared_slice`, `other_slice_or_grouping`, `separate_measure` (not presented as revenue), `not_a_use`, `cannot_tell` — separate several with `;` |
| `allowed` | `declared`, `approved`, `prohibited`, `unspecified`, `not_applicable`, `cannot_tell` |
| `rule_id` | the owner rule that applies, e.g. `A4` or `P1` (blank if none) |
| `alert` | `alert` (someone should review this), `informational`, `none`, `cannot_tell` |

`notes` is optional. When you are done, save the CSV and tell me; scoring is
`python src/touchstone/benchmark/v4_fresh/score.py score <your sheet> out/v4_fresh/verdicts.SEALED.jsonl`.
