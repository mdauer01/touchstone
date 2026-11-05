# Fresh test for criteria v4 (frozen before the test SQL existed)

A pilot package (`pilot/`) for the TPC-H `revenue` metric view on the Databricks sample
data, with the owner's rules: four approved variations (A1-A4) and two prohibited ones
(P1-P2). The evaluation configuration - code, criteria v4, and these rules - was frozen
in the commit that added this file, before any test statement was written.

The test statements come afterwards: Databricks Genie's SQL for new business questions,
official TPC-H queries (human-written decades ago), and the same SQL in different
consumption contexts. They are labeled blind by a person on three separate questions -
what changed, whether it is allowed, whether it warrants an alert - so a disagreement
about governance shows as one, not as a classifier error.

Primary measures: alert precision, and missed definition changes. Also reported: alerts
on legitimate investigations, and how much stays unresolved.
