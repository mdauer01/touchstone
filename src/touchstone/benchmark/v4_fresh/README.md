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
