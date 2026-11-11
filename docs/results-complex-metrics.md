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

Every case, expected state against the detector's (the row totals are the expected
states; "not assessed" is a use the intake could not compare, with no model call):

| Expected \ detector | review needed | could not assess | no issue flagged | not assessed |
|---|---|---|---|---|
| review needed (18) | 13 | 5 | 0 | 0 |
| could not assess (6): undeclared joins | 0 | 6 | 0 | 0 |
| no issue flagged (181) | 4 | 17 | 158 | 2 |

- **The counts reconcile:** 205 cases = 203 comparisons (one model call each) + 2 uses
  not assessed. The 2 are intake failures, fragments of the setup's DDL (below), whose
  expected state is "no issue: not a use". Over the 203 comparisons alone, false alarms
  are 4 of 179.
- **Ambiguous cases:** 14 were marked ambiguous before any verdict (by either the author
  or the blind reviewer). Without them: recall 12 of 16, precision 12 of 15, false
  alarms 3 of 170.
- **The misses are missed alerts.** No drift was marked clean, but the 5 missed drifts
  sit among 30 could-not-assess answers, which a reviewer would have to read to find
  them.

## What the misses and false alarms were

- **No drift was passed as clean.** The 5 missed drifts were answered could not assess.
  On the 18 drifts the model's own answer was "departs" each time, with a correct
  explanation and a verified clause; it also answered "departs" where there was no drift
  (the false alarms below). Every miss came from the code's name check, which requires the metric's name inside the output's name: `avg_order_value`
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

## After the 0.1.2 patch: performance on the now-known benchmark

The fixes in 0.1.2 came from reading this test's results, so these scores are on a
benchmark the fixes were made against. They are not a fresh test, and they do not support
a new claim of generalization.

- **The records.** From the same 393 statements the patched intake builds 153 records
  and no uses not assessed. The DDL is one statement again, and an abbreviated output is
  compared with the metric it names instead of with every measure on its source (50
  "not presented" comparisons fewer). All 18 drifts and the 6 undeclared joins are still
  cases; each expected state is carried over unchanged from the same statement and
  measure.
- **The model calls.** With the patched code and v6, the 131 records whose prompt is
  byte-identical reuse the sealed answers and 22 new records were asked; with v7 all 153
  were asked. Answers are then decided by the patched code (the excerpt fix below was
  applied to the stored answers, with no further calls).

| Run on the known benchmark | Drift caught | Flags right | False alarms | Could not assess |
|---|---|---|---|---|
| 0.1.1, criteria v6 (the sealed result above) | 13/18 | 13/17 | 4/181 | 30/205 |
| 0.1.2 code, criteria v6 | 18/18 | 18/28 | 9/129 | 10/153 |
| 0.1.2 code, criteria v7 | 18/18 | 18/23 (78%) | 4/129 | 7/153 |

On the earlier benchmarks, with the 0.1.2 code and v7: the first frozen test, 7 of 7
drifts and 7 of 7 flags right, no false alarm, 4 of 26 could not assess; the dev set, 13
of 13 and 13 of 13, no false alarm, 9 of 57. Both as with v6.

- **What fixing the name check exposed.** With v6 the model's other readings reach the
  review state: NULL checks on a rate's dates, `try_divide`, two separately named
  averages. v7's two rules remove five of those false alarms.
- **What is left.** The 4 false alarms, and one flag on a case that depends on an
  undeclared join, are one reading: `l_discount IS NOT NULL` on the weighted average
  discount, read as removing those lines' gross value from the denominator. v7 calls a
  filter on the common input a slice, and the model does not follow it there. On this
  benchmark precision (78%) stays below the 0.1.0 bar's 80%.
- **Found while validating.** The excerpt check required a multi-part quote's parts to
  occur in order and, across lines, next to each other; it rejected two correct answers
  (one here under v7, one on the first test). It now checks that every part occurs.

## What this means

One more test: one schema, one model, one run. The frozen result is the first table:
the bar was not met. The patch fixes the code checks that discarded correct readings,
and on the known benchmark every drift now reaches review as review needed, at the cost
of four false alarms of one kind. A claim about statements the detector has not seen
needs a new test; the next useful one is an outside team's own export.
