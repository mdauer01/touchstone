# Second frozen test: non-additive metrics (2026-11-09)

The second frozen test of `touchstone check`, and the first of metrics that are not
sums. The detector was touchstone 0.1.1 as released (criteria v6, gpt-6-luna), with
nothing changed for the test. The test's data is not published; this page reports
the result.

## What was tested

- **Definitions**: two Unity Catalog metric views over the TPC-H sample data. One over
  orders, for customers with a non-negative account balance (a filter through a join):
  active customers (a distinct count), average order value (an average), orders per
  customer (a count over a distinct count). One over line items: a value-weighted return
  rate, a late-delivery rate (a filtered count over a count), a discount weighted by
  gross value. Both state their relationship facts and what nation and region mean.
- **Statements**: Databricks Genie answered 55 conversations (63 messages) twice: once
  in a space with the raw tables and both metric views, once in a space with the raw
  tables and a one-line glossary per metric. The questions covered slices, dropped
  eligibility, changed numerators and denominators, summed distinct counts, averaged
  rates, approximate counts, undeclared joins and separately named variants.
- **History**: `system.query.history` for the window, every finished statement (393:
  Genie's queries, the metadata queries Genie ran on its own, the setup's DDL), read
  as a user's export would be.
- **Protocol**, as for the first test: the detector, the questions and the expected
  state of every case were committed before any verdict existed. The expectations were
  written from the SQL and the definitions by the policy's rules; a second model,
  blind to them, agreed on 201 of 203; running the statements on local data raised no
  conflict. The detector judged each case once, the findings were sealed by hash before
  they were opened, and they were scored once.

## Result

205 cases: 203 comparisons (123 distinct statements, each compared with the measures it
could use) and 2 uses not assessed.

| Measure | Result | 95% lower bound |
|---|---|---|
| Drift caught (recall) | 13 of 18 (72%) | 50% |
| Review-needed answers right (precision) | 13 of 17 (76%) | 54% |
| False alarms, over cases without drift | 4 of 181 (2.2%) | |
| Could not assess or not assessed | 30 of 205 (15%) | |

**The release bar of 0.1.0 (no missed drift, at least 80% precision) is not met.**

## What the misses and false alarms were

- **No drift was passed as clean.** The 5 missed drifts were answered could not assess,
  so all 18 reached review. The model's own answer was "departs" on 18 of 18, each with
  a correct explanation and a verified clause. Every miss came from the code's name
  check, which requires the metric's name inside the output's name: `avg_order_value`
  (four cases) and `weighted_avg_discount` do not contain `average_order_value` or
  `average_discount`. The same check turned 5 cases without drift into could not
  assess.
- **Another code guard** (a statement that names the metric cannot be set aside)
  turned 7 cases into could not assess where the model correctly answered that no
  output presents the metric: a measure computed in a subquery whose final output is a
  share, an average of monthly values, or a product of measures.
- **False alarms (4):** two read `IS NOT NULL` checks on a rate's dates as changing its
  denominator, where the policy calls them a slice; one called `try_divide` a departure
  for its behaviour at a zero denominator; one read `customer_avg_order_value` as
  average order value, a case marked ambiguous beforehand. For the weighted discount the
  model also read `l_discount IS NOT NULL` as changing the denominator in four cases:
  for a ratio this is a real question the policy's NULL rule does not settle.
- **Intake:** the statement splitter cuts `CREATE VIEW ... WITH METRICS` at semicolons
  inside its YAML, so metric-view DDL in a history appears as uses not assessed (2 here).
- **Genie:** with the metric views available, it used them for 48 of 63 statements and
  named most requested variants for what they are; 6 of its statements present a
  governed metric computed differently. With the raw tables, 12 do.

## What this means

One more test: one schema, one model, one run. It shows the model's reading holding up
on non-additive metrics, and two code checks and one policy rule that do not: the
name check is stricter than the policy it enforces, and the NULL rule does not settle
filters on a ratio's inputs. Any fix is a detector change, and has to be measured on a
test the detector has not seen; this test is now development data.
