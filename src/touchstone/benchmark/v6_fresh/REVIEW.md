# Checks on the expectations, before any verdict

Both ran after Genie answered and before `touchstone check` judged any case.

## Execution challenges (`python ../harness.py challenge .`)

26 cases: 7 review_needed, 15 no_issue_flagged, 4 could_not_assess.

- review_needed differs from the reference on clean data: 6 of 6 with ingredients
  (the seventh, the supplier's nation presented as `nation`, is a meaning departure
  that execution cannot show).
- no_issue_flagged agrees with the reference on clean and fact-respecting dirty data:
  12 of 12 with ingredients.
- could_not_assess changes when the unstated fact is violated: 3 of 3 part, 1 of 1
  supplier.
- The fixtures satisfy the stated facts (exactly one match along the declared keys),
  clean and dirty.

**One conflict, examined:** `cmp_f951696ca301d121` (quantity of exactly 1, 1996): the
data cannot show a dropped required filter, because in the local generator quantity and
return flag come from correlated hash values, so no returned line item has quantity 1.
The statement keeps the required filter and its no_issue_flagged expectation passed its
own challenge; the conflict is a limit of the fixture, recorded here, and the data is
not changed (it would change the earlier tests' keys).

## Blind model review

A second model read only the definition and the 26 statements with the proposed states
(no verdicts existed) and decided each state itself. This is model review, not
independent ground truth.

- **Agreed on 26 of 26 states.**
- Two reasons reworded on its notes, states unchanged: `cmp_a1d97ee5db7474c7` (the
  LOWER/TRIM filter also matches case and space variants: the required filter is
  replaced by its opposite) and `cmp_a839fa7eea9b3952` ("on request" is not visible in
  the SQL).
- Its closing tally (8 / 4 / 14) miscounts its own list, which has 7 review_needed and
  15 no_issue_flagged.
