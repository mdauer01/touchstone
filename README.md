# touchstone

Advisory metric drift detection. touchstone checks the SQL that AI agents and BI tools
actually run against your approved metric definitions, and flags results presented as a
governed metric but computed differently, with the evidence.

Give it your semantic layer's definitions (Databricks metric views today) and a query
history. For each distinct statement that uses a governed metric it answers one question:
is the number presented as this metric computed the way the definition computes it? The
answer is one of three review states:

- **Review needed**: presented as the metric, computed differently. The report shows the
  definition's clause beside the statement's (or "missing from the statement"), a
  one-sentence explanation, and where the statement ran: dashboards, jobs, Genie, the editor.
- **Could not assess**: the answer depends on facts the definition does not state (for
  example an INNER JOIN to a table the definition does not declare), or the SQL cannot be
  read. The report says what is missing.
- **No issue flagged**: computed as defined. Extra filters and groupings are legitimate
  slices; a separately named quantity (an average, a share) is not judged as the metric.

The assessment is advisory: a model's reading that orders review, never a verdict that a
number is right. **No issue flagged** does not certify a statement correct. Each quoted
clause is checked against the statement or the definition; a clause found there shows the
text exists, not that the model interpreted it correctly. Status: 0.1.2, an early release;
see [How well it works](#how-well-it-works).

- License: [Apache-2.0](LICENSE)

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev,openai]"
export OPENAI_API_KEY=...     # or another provider, below

.venv/bin/touchstone check \
  --pilot examples/minimal \
  --provider openai --model <model> \
  --out out/check

open out/check/report.html
```

`--provider fake` runs the same pipeline without a key; its answers are placeholders and
measure nothing. [examples/minimal](examples/minimal) is the smallest complete package:
one metric view and three statements (a use through the view, the metric recomputed
without its required filter, and a separately named average).

**A real report:** [src/touchstone/benchmark/v6_fresh/report.html](src/touchstone/benchmark/v6_fresh/report.html)
(download and open it) is the fresh test below: 26 Genie statements against the TPC-H
`revenue` metric view on the Databricks sample data, with the model's sealed answers in
[findings.jsonl](src/touchstone/benchmark/v6_fresh/findings.jsonl).

### What you bring

A package directory whose `pilot.yml` names the definitions and the history. The whole
of [examples/minimal/pilot.yml](examples/minimal/pilot.yml):

```yaml
history: history.jsonl
window: {from: 2026-11-01T00:00:00Z, to: 2026-12-01T00:00:00Z}
compare_against: as_of_execution
authority:
  system: uc_metrics
  metric_views:
    - name: main.sales.revenue_mv         # fully qualified, as deployed
      snapshot: views/revenue_mv.yaml     # the definition text
      sha256: 02dfa6747b4eafea46df8547cddb0e7fe346e644158432777a0b2bd89b858455
      applies: {from: 2026-11-01T00:00:00Z, to: null}
```

- **The definitions**: each metric view's YAML as deployed, its sha256 (so a changed file
  is refused, not silently compared) and when it applied. What a definition states is
  all the check knows. Write relationship facts ("each line item matches exactly one
  order") and what a dimension means ("nation is the customer's") in the view's own
  comments; nothing is assumed. Join forms other than many-to-one
  (`cardinality: one_to_many`) are not assessed in this release.
- **The query history**: an export of `system.query.history`
  ([scripts/export_databricks_history.sql](scripts/export_databricks_history.sql)), as
  CSV or JSON lines; `statement_text` and `start_time` matter most.
- Optionally **a catalog** of column types (`touchstone catalog` writes one).

Which definition a statement is compared with is decided in code, never by falling back
to the first one listed. `MEASURE(revenue)` on a view is that view's `revenue`; a
statement that reads a definition's source is compared with the definitions over it;
a name two views declare, with nothing to choose between them, is not compared. With
`compare_against: as_of_execution` a definition is used only for runs inside its
`applies` window, so a run with no start time, or one before the definition applied, is
not compared either. Each such use is listed in the report as **not assessed**, with
the reason, as is a statement that cannot be parsed but mentions a governed view, source
or metric: an empty review queue means no drift was found, not that nothing was read.

### Models

The assessment runs where you choose: `--provider openai` (OpenAI or any compatible
server: LiteLLM, vLLM, Groq, Ollama via `--base-url`), `anthropic`, or inside your data
platform: `databricks` (`ai_query`) or `snowflake` (Cortex `AI_COMPLETE`), so statements
and definitions never leave it. Credentials come from the environment, never from
arguments. Every distinct statement is one call; `--cache` reuses answers across runs.

### Output and exit codes

`report.html` (one self-contained page), `findings.jsonl` (one line per use: the review
state with its evidence and whether each quoted clause was found verbatim, or why it was
not assessed) and `comparisons.jsonl` (the records as assessed). Exit 0 when the
assessment completed, whatever it found, including uses it could not compare; 1 when
some model requests failed (the report lists what was not assessed); 2 when the check
could not run or the report could not be written.

### How well it works

Measured with an automatic harness ([src/touchstone/benchmark/harness.py](src/touchstone/benchmark/harness.py)):
expected states written from each statement and the definition, challenged by running
the statements on local data, scored for recall of review-needed cases (an abstention on
one counts as a miss), precision of review-needed answers, and abstention.

- On the **dev set** (57 cases from two earlier tests, already seen, used to tune):
  13 of 13 review-needed cases caught, no false alarm, 9 could not assess.
- On **one frozen test of 26 queries against one metric** (new Genie statements, judged
  once, sealed, then scored:
  [src/touchstone/benchmark/v6_fresh](src/touchstone/benchmark/v6_fresh/README.md)),
  it identified all seven expected drifts, raised no false alerts, and abstained on four
  cases (the four expected joins the definition says nothing about). That met the 0.1.0
  release bar (no missed drift, at least 80% of review-needed answers right). It is one
  small test with one model and one metric, not a production rate, and its expected
  states were written by the developer, challenged by execution and agreed by a second
  model, not reviewed by an independent person. The sealed findings are published
  beside the test; rescore them with
  `python src/touchstone/benchmark/harness.py score src/touchstone/benchmark/v6_fresh/findings.jsonl src/touchstone/benchmark/v6_fresh`.
- On a **second frozen test, of non-additive metrics** (205 cases: six measures over
  TPC-H, including distinct counts, ratios and weighted averages, from two Genie spaces
  and the real query history; [docs/results-complex-metrics.md](docs/results-complex-metrics.md)),
  it caught 13 of 18 expected drifts and passed none as clean (the other 5 were could
  not assess); 13 of 17 review-needed answers were right, with 4 false alarms in 181
  cases without drift. That does not meet the 0.1.0 bar. All 5 misses came from the
  code's name check, which did not accept abbreviated names such as `avg_order_value`.
  After the fixes in 0.1.2, on this now-known benchmark (not a fresh test): 18 of 18
  drifts caught, 18 of 23 review-needed answers right, 4 false alarms in 129 cases
  without drift, all of one kind (a NULL check on a weighted average's input).

Historical comparison, not like for like: on the dev set the earlier five-question
classifier (criteria v4) raised 42 alerts to catch the same 13. It received owner rules
instead of the definition's stated facts, under a different policy, so the difference
reflects the inputs and the policy as well as the classifier.

### Lower-level commands

`touchstone assess run --comparisons out/check/comparisons.jsonl` re-assesses the records
`check` wrote, under another model or criteria version. `touchstone monitor` is legacy:
the older deterministic SQL rules, which need a scan; its `--export-comparisons` chooses
definitions by those rules, not by `check`'s selection above, so it is not part of the
supported path.

## Experimental (parked)

The estate scan and reconciliation, the bitemporal store, signed receipts and the MCP
server, relationship mining, the equivalence engine and its execution oracle, publish,
propose and the CI diff are kept and tested but off the main path. Their commands live
under `touchstone experimental <command>`, and their documentation is in
[docs/experimental.md](docs/experimental.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
