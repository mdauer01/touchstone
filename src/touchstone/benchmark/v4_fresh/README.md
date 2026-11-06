# Fresh test for criteria v4 (frozen before the test SQL existed)

A pilot package (`pilot/`) for the TPC-H `revenue` metric view on the Databricks sample
data, with the owner's rules: four approved variations (A1-A4) and two prohibited ones
(P1-P2). The evaluation configuration - code, criteria v4, and these rules - was frozen
in the commit that added this file, before any test statement was written.

The test statements come afterwards: Databricks Genie's SQL for new business questions,
official TPC-H queries (human-written decades ago), and the same SQL in different
consumption contexts. They were judged once and the verdicts sealed (`SEALED.md`).

## The answer key, by execution

No person labels anything. `answer_key.py` answers three questions per statement:

- **Presented as the metric?** From the output name: `revenue`, or `revenue` qualified by
  a subset (rule A3: `weekly_revenue`, `promo_revenue`), presents the metric; a name that
  states another quantity (`avg_daily_revenue`, `revenue_before_discount`) is a
  separately named measure; a name without `revenue` does not present it.
- **What changed?** The statement and a reference query run in DuckDB on local
  TPC-H-shaped data, clean and then dirty (NULLs, unknown flag values, duplicate rows:
  nothing the definition or the owner's rules exclude). The reference is built by code:
  the governed measure over the governed rows, built from the view snapshot, plus
  exactly the statement's own extra filters and groupings (`answer_key.yaml`). An
  ingredient on the required filter's column is refused, so a departure cannot be
  copied into the reference. A different result on clean data is a changed definition;
  only on dirty data, a result that depends on facts nobody stated.
- **Allowed?** Each extra filter or grouping is classed by the columns it reads:
  declared dimensions, an owner rule (A1, A4, P1, P2), or other attributes.

Outcome, in the order of the target behavior: not presented as the metric, no alert; a
prohibited variation, alert; a changed definition, alert; depends on unstated facts,
unresolved; a variation on other attributes, informational; otherwise no alert.

**A pass is not a proof.** The build fails unless, for every statement compared, the
data can show a dropped required filter (the reference with and without it differ).
The data was changed once for this: TPC-H's generator ties returned items to early
dates, which the definition does not state, so return flags are independent of dates.
The key does not test fan-out from joins the statement adds (part, supplier): the
reference repeats them.

```bash
python src/touchstone/benchmark/v4_fresh/answer_key.py
python src/touchstone/benchmark/v4_fresh/score.py out/v4_fresh/verdicts.SEALED.jsonl
```

Primary measures: alert precision, and missed alerts (changed definitions and
prohibited variations). Also reported: alerts on statements the key does not alert on,
and how much stays unresolved.

## Result (scored once, 2026-11-05)

| Measure | Result |
|---|---|
| Alert precision | 10 of the model's 20 alerts are alerts in the key |
| Missed alerts | 0 of 10 (7 changed definitions, 3 prohibited variations) |
| Investigations alerted (key: informational) | 5 of 5 |
| Permitted uses alerted (key: no alert) | 3 of 4 |
| Separate measures alerted | 2 of 8 (contested, below) |
| Unresolved | 0 (key: 0) |

v4 as frozen does not meet its goal: it alerts on every investigation. It stays opt-in.

- **8 of the 10 false alarms come from one fact.** The model answered `source_rows:
  changed` for statements whose results equal the reference on clean and dirty data:
  four read `lineitem` alone, four join along the declared keys with INNER JOIN (rule
  A2, which the model cited in three of them). Databricks metric views join only the
  tables a query needs and treat every join as many-to-one
  ([joins in metric views](https://docs.databricks.com/aws/en/uc-semantics/metric-views/joins)),
  so neither changes the rows. `decide()` checks a changed core fact before owner rules,
  so the A2 citations could not count.
- **2 are contested naming:** `avg_daily_revenue` and `revenue_before_discount`. The
  model read rule A3 (qualified names present the metric) as covering them; the key
  reads a name that states another quantity as a separately named measure.
- **The 3 prohibited variations were alerted for the wrong reason:** the model cited P1
  or P2 correctly each time, but `source_rows: changed` made them definition changes.
