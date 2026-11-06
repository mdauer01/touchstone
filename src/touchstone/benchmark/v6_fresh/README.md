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

## Result (scored once, 2026-11-06)

| Measure | Result |
|---|---|
| Drift caught (recall over expected review_needed) | 7 of 7 |
| Review-needed answers right (precision) | 7 of 7 |
| Could not assess | 4 of 26 (15%), exactly the 4 expected: joins to part or supplier, which the definition says nothing about |
| Agreement on all three states | 26 of 26 |

**The release bar is met** (no missed drift, at least 80% of review-needed answers
right). What this does and does not show:

- One frozen test: 26 cases, one metric (TPC-H revenue), one model (gpt-6-luna), one
  run. With 7 drift cases, 7 of 7 is consistent with a true recall well below 100%;
  it is evidence on this test, not a production rate.
- The expectations were written by the developer from the SQL, challenged by execution
  (one fixture limit, examined) and agreed by a blind second model, 26 of 26. They are
  not independent human labels.
- The sealed findings are published: [findings.jsonl](findings.jsonl) (the same bytes as
  the hash in [SEALED.md](SEALED.md); `python ../harness.py score findings.jsonl .`
  rescores them) and [report.html](report.html), the report the check code renders from
  those answers. Replaying the answers through the check code of 0.1.1 gives the same 26
  records, states and evidence, so the result holds for these 26 cases under 0.1.1. They
  do not exercise what 0.1.1 added (session context in the prompt, coverage gaps), and
  this test does not measure it.
- No issue flagged does not certify a statement correct, and a quoted clause found in
  the text shows it exists, not that the model interpreted it correctly.
- Genie wrote every statement; the questions were chosen to cover drift, ordinary
  investigations, declared slices, undeclared joins, a dimension with another meaning
  and separately named measures.
