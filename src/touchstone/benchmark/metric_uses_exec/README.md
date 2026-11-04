# Execution-checked metric uses (synthetic, 114 items)

Written for the 2026-11-04 study of which assessor should judge drift (touchstone's
rules, an LLM judge, or an LLM-written counterpart checked by execution). Same
definitions and policy as `../metric_uses` (`definitions.yaml` is a copy).

| family | items | written to |
|---|---|---|
| `exec_llmhard` | 30 | fool an LLM judge: NULL semantics, precedence, misleading comments and aliases |
| `exec_parsehard` | 30 | defeat a SQL canonicalizer: QUALIFY, ROLLUP, lateral aliases, set operations |
| `exec_real` | 30 | look like real logs: Looker/Tableau-generated SQL, analyst mistakes |
| `long_longbi` | 12 | long BI-generated statements (79-181 lines), drift buried in CTEs |
| `long_longnb` | 12 | long notebook/dbt-style analyses with many other columns |

## How the labels were checked

Every item carries `counterpart_sql`: the governed definition with exactly the declared
filters and groupings the statement applies. Both were executed in DuckDB on clean
datasets (everything the definitions state holds) and dirty ones (duplicates, orphans,
NULLs the definitions do not rule out). A difference on clean data means the statement
does not compute the permitted counterpart. A difference only on dirty data means the
result depends on unstated facts, so the label is `insufficient_evidence`. Long items
compare only `key_columns` and `metric_column`, projected out of the statement by name.

The authored labels agreed with execution on 88 of 90 short items and 23 of 24 long
ones, after the datasets were strengthened. Before that, 6 known drifts executed
identically for lack of rows that exercise them, so **a pass is not a proof**.
`adjudication.yaml` records the three decisions. No human has reviewed these labels:
`touchstone assess review-sheet --dataset src/touchstone/benchmark/metric_uses_exec`.
