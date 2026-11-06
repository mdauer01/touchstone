# Fresh test for criteria v5 (frozen before the test SQL existed)

v4's fresh test (`../v4_fresh`) caught every changed definition and prohibited
variation but raised as many false alarms as real alerts: the model said the source
rows changed when a statement read the source alone or joined the declared tables with
INNER JOIN. Criteria v5 (`assess/criteria/v5.yaml`) states the metric view's join
semantics (left outer, many-to-one, applied only when needed:
[joins in metric views](https://docs.databricks.com/aws/en/uc-semantics/metric-views/joins)),
takes facts and joins an owner rule states as stated, separates a name qualified by a
subset (`eu_revenue`) from a name that states another quantity (an average, a share),
and decides a cited prohibited variation before the core facts.

## What was frozen, in the commit that added this file

- **Criteria v5** and the code that decides outcomes (`criteria.decide`).
- **The metric and the owner's rules**: the same `revenue_mv` snapshot and the same
  rules A1-A4 and P1-P2 as v4's test (`pilot/pilot.yml`; only the window is new).
- **The questions** (`questions.json`: 24 conversations, 27 questions, none asked of
  Genie before) and three official TPC-H queries not used before (`official.json`:
  Q8, Q9, Q15).
- **The answer-key builder** (`../v4_fresh/answer_key.py`): the local data now has every
  column of the catalog, and P2 also reads a single `o_custkey`. v4's key rebuilds byte
  for byte with it.

## Then, in order

1. `collect.py` asks Genie once and writes `genie_answers.jsonl` and
   `pilot/history.jsonl` from Genie's replies (no system-table export, so no warehouse
   query beyond Genie's own). Nothing is selected or edited.
2. `touchstone scan` and `touchstone monitor --export-comparisons` turn the history into
   `pilot/uses.jsonl`.
3. `answer_key.yaml` lists each statement's own extra filters, groupings and joins, read
   from the SQL alone, and `answer_key.py` builds `answer_key.jsonl` by execution, as in
   v4's test. Both are committed before any model judges the statements.
4. The frozen configuration judges every statement once with criteria v5, and once
   with v4 as the baseline on the same statements (gpt-6-luna, chat completions). The
   verdicts stay outside the repository; `SEALED.md` records their hashes.
5. `score.py` scores each once.

```bash
python src/touchstone/benchmark/v4_fresh/answer_key.py src/touchstone/benchmark/v5_fresh
python src/touchstone/benchmark/v4_fresh/score.py out/v5_fresh/verdicts.v5.SEALED.jsonl src/touchstone/benchmark/v5_fresh
```

Primary measures, as in v4's test: alert precision, and missed alerts (changed
definitions and prohibited variations).

## Result (scored once, 2026-11-06)

Both runs on the same 30 statements; the answer key alerts on 8 (5 changed
definitions, 3 prohibited variations), is informational on 7 investigations, and
raises no alert on 15.

| Measure | v5 | v4 (baseline) |
|---|---|---|
| Alert precision | 8 of 14 | 8 of 22 |
| Missed alerts | 0 of 8 | 0 of 8 |
| Investigations alerted (key: informational) | 2 of 7 | 7 of 7 |
| Permitted uses alerted (key: no alert) | 2 of 8 | 5 of 8 |
| Separately named measures alerted | 2 of 3 | 2 of 3 |
| Unresolved | 0 | 0 |
| Outcome agreement | 24 of 30 | 13 of 30 |

v5 removes most of v4's false alarms with no missed alert, but 6 remain, so it
stays opt-in:

- **4 still answer `source_rows: changed`.** Three of them cite the very rule that
  settles it: A2 for an INNER JOIN along the declared keys (order year; FOB in the
  MACHINERY segment) and A4 for the part join it names (revenue by brand). The fourth
  joins supplier to group by supplier, a join no rule names. The FOB statement also
  joins part without using it, which the key does not count as a change (the
  reference repeats added joins).
- **2 derived measures were read as the metric:** `median_revenue_per_order_1995` and
  `avg_revenue_per_customer`, each citing A3 (qualified names present the metric).
  v5's naming answers say a name that states another quantity is a separate measure;
  the model followed the owner's rule instead.
