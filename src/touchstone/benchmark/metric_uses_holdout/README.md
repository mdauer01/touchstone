# Held-out metric uses (criteria v3)

40 metric uses written after the criteria v3 policy was refined against `metric_uses`
and `metric_uses_exec`, and never used to refine it. They use SQL patterns those sets do
not contain as written (aggregate FILTER, GROUPING SETS, QUALIFY, a sum of per-channel
sums, a DISTINCT over a window sum, a self semi-join, time travel, TABLESAMPLE, a time
zone shift before truncation, an unresolved template parameter, a cut-off statement, ...).

The definitions are the same as `metric_uses_exec` (`definitions.yaml`). Each label is
decided by logic under the v3 test: equal to the definition for every possible data the
definition allows (`permitted_variation`), different for some possible data (a drift),
or not readable (`insufficient_evidence`). 17 permitted, 19 drifts, 2 non-uses and 2
unreadable. One label was corrected after the first runs (`hold-40`, with its reason).

Provenance: written by a model, with no execution check, no blind pass and no human
review yet (`adjudication.yaml`). A judge's disagreement is evidence about the item as
much as about the judge.

```bash
touchstone assess benchmark --provider <backend> --model <model> --split all \
  --dataset src/touchstone/benchmark/metric_uses_holdout
```
