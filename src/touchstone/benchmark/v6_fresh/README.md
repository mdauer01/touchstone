# Fresh test for `touchstone check` (criteria v6), frozen before the test SQL existed

The release gate for 0.1.0. Everything that decides the result was committed before
Genie answered any of these questions:

- **The detector**: `touchstone check` with criteria v6 (one assessment with evidence,
  three review states), as of the commit that added this file.
- **The definition**: the TPC-H `revenue` metric view on the Databricks sample data,
  with the facts it states in its own comments: along the declared join keys each line
  item matches exactly one order, each order one customer, each customer one nation,
  each nation one region; nation and region mean the customer's. No owner approvals or
  prohibitions.
- **The questions** (`questions.json`): 26 in 24 conversations, none asked of Genie
  before, covering drift requests, ordinary investigations, declared slices, joins the
  definition says nothing about, a dimension presented with another meaning, and
  separately named derived measures.
- **The harness** (`../harness.py`) and **the release bar**: no missed drift (every
  expected review_needed answered review_needed; could_not_assess on one is a miss) and
  at least 80% of review_needed answers right, counted per distinct case. The abstention
  rate is reported too. One frozen test is evidence, not a production rate.

## Then, in order

1. `collect.py` asks Genie once; `genie_answers.jsonl` and `pilot/history.jsonl` keep
   every answer as returned.
2. `expected.yaml` (the expected state of each case, with its reason) and
   `ingredients.yaml` (each case's own filters, groupings and joins), written from the
   SQL and the definition alone, before any v6 verdict exists.
3. `python ../harness.py challenge .` runs the statements on local data to challenge the
   expectations; every conflict is re-examined and recorded. A second model, blind to
   any verdict, reviews the expectations (model review, not independent ground truth).
4. `touchstone check` judges every case once with gpt-6-luna; the findings stay outside
   the repository and `SEALED.md` records their hash.
5. `python ../harness.py score <findings> .` scores them once.
