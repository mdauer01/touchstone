# Proof policy `semantix-proof/2`

A positive verdict (CERTIFIED, ALIASING, monitor `conforming_use`) says two
definitions compute the same thing. That statement must come from rules whose
soundness can be argued one at a time, not from a general query optimizer.

## Status: positive verdicts are suspended

Three independent reviews of this policy's first implementation reproduced, by
execution, about fourteen classes of false certification. It was the ninth
successive round to find new ones. Some were implementation errors, such as star
modifiers ignored, CTE scoping, and literal GROUP BY keys read as ordinals. Others were
errors in the **parse itself**:

- sqlglot gives `%` the precedence of `+`;
- it binds `IS` / `IS DISTINCT FROM` / `NOT … IN` differently from DuckDB and Spark.

`round_trips` compares sqlglot only with itself, so it cannot see either. A guard that
works on a misparsed tree may never recognize the dangerous construct, so refusing the
known classes does not establish that what remains is sound. Until the criteria below
hold:

- **Equalities are not conclusions.** CERTIFIED, ALIASING and monitor conforming /
  contested uses are reported as **experimental comparisons**. The engine's result
  travels under `evidence.experimental` / `experimental` for development; no surface
  presents it as a conclusion.
- **Definitive negatives become suspected.** CONTRADICTION and definition violations
  rest on the same interpretation, so they are **suspected divergences / differences**.
  Their reasons are unverified observations.
- **Reliance is decided by one policy** (`semantix.conclusions.reliance`). It gives
  three separate answers:
  1. signature authentic: this store issued exactly this payload;
  2. estate unchanged: a live re-check against the receipt's scan;
  3. semantic claim usable: under the policy in force.

  Only the third authorizes use, and neither of the first two can establish it. While
  positive verdicts are suspended it is always no: CLI, MCP, publish, the dbt
  `certified` gate (which fails closed) and `verify-receipt` (which exits 1, with or
  without `--require-current`, for current and as-of receipts alike).
- **Records are not rewritten.** Original signed receipts and stored rows stay as they
  are, and today's assessment is reported beside them. Stores written earlier are read
  under the policy (`Store.verdicts_for_scan`). Warehouse tables published earlier are
  brought under it with `semantix publish --refresh-policy`, which maps the verdicts,
  sets `usable_for_reliance = false`, and adds an `assessment/<concept>` row beside each
  unchanged receipt.

**Re-enable criteria.** One clean review or pilot is not enough. All of these must
hold:

- an explicitly defined supported grammar;
- a stated justification for every transformation;
- evidence that parsing matches **each target engine**.

A parser-disagreement detector against DuckDB is one source of evidence about DuckDB
only. It is not a qualification for any engine, and passing it cannot by itself
re-enable positive verdicts.

**The grammar is frozen.** `tests/test_grammar_frozen.py` pins every rule, node and
argument of the allow-lists as an upper bound. Refusing more is always allowed; adding
anything needs a new policy id and the criteria above.

## Why the optimizer left the certification path

Up to round fifteen, proof grade meant "sqlglot's optimizer produced a
canonical form". The second sweep after `6e831d0` reproduced 35 classes of
rewrite in which that form did not preserve the query's meaning:

- predicate pushdown below an ordered `LIMIT 1` (NULL became 900);
- merges on the null-supplying side of outer joins;
- join elimination that changed row counts;
- subquery unnesting under `NOT IN` and empty scalar lookups;
- boolean simplification that is wrong under NULLs;
- exact-decimal folding of DOUBLE arithmetic.

When an optimizer finishes successfully, that does not show its output means the same thing as the query. Each round of guards around
it closed the cases found and left the class open.

The optimizer is still used to *find* things: its readings feed candidate matching, diffs, blame and model
registration. Those readings are **hypotheses**. They never certify, alias or
conform, and nothing derived from one is stated as a definitive difference.

## The grammar (positively defined)

`extraction/proof.py` grants proof grade only to SQL inside this grammar. Anything
else is refused, and the refusal names the construct.

- **One `SELECT` over exactly one source.** The source is a table, or a chain of
  select-project-filter CTEs and derived tables over one table.
- **Expressions** may use:
  - columns of that table, literals and arithmetic (not `%`);
  - comparisons, `AND` / `OR` / `NOT`;
  - `IN` over literals, `BETWEEN`, `IS [NOT] NULL`, `LIKE`, `CASE`;
  - an allow-list of scalar functions (`proof._SCALAR_FUNCTIONS`), `CURRENT_DATE`
    and `CURRENT_TIMESTAMP`.
- **Aggregates** are `SUM`, `COUNT` (including `COUNT(*)` and `DISTINCT`),
  `AVG`, `MIN` and `MAX`, each with an optional `FILTER (WHERE …)`. `GROUP BY`
  and `HAVING` may be used over these.
- **Every column resolves** to a known column of the source. The catalog
  describes that relation under the exact name the SQL resolves to: a short
  catalog entry's columns are never borrowed for a longer name.
- **Refused:**
  - joins, set operations and recursion;
  - `LIMIT` / `OFFSET`, `DISTINCT`, `QUALIFY` and window functions;
  - subqueries in expressions, `ROLLUP` / `CUBE` / `GROUPING SETS`;
  - sampling, time travel, `LATERAL` / `UNNEST` / `PIVOT`, bind parameters;
  - unknown functions, and CTE or derived-table bodies that aggregate in any
    clause.
- **Refused since round seventeen**, each reproduced by execution:
  - `%`, which sqlglot binds like `+` and the engines like `*`;
  - an unparenthesized chain of comparison / IS / IN / BETWEEN / LIKE operators
    (`tier = 1 IS NOT TRUE`, `NOT a IN (…) IN (…)`), which the parser and the engines
    bind differently;
  - `*` with any modifier (EXCLUDE, EXCEPT, REPLACE, RENAME, ILIKE), and `SELECT *` in
    an aggregating query;
  - GROUP BY a parenthesized item, a tuple, a constant, or a name or ordinal that
    resolves to one (they render as ordinals);
  - a `DATE_TRUNC` / `TRUNC` / `DATE_PART` unit written unquoted (the engines read a
    column of that name) and function-form `like()` (the parser swaps its arguments),
    both found from the original tokens;
  - ORDER BY inside an inlined body (it fixes the order a float SUM or AVG adds rows
    in);
  - BETWEEN whose bounds are of different kinds (`qty BETWEEN 2.5 AND '2.5'`);
  - a WITH that is not the statement's own, and a CTE that reads a CTE defined after
    it;
  - a qualifier other than the exact name the source is visible under;
  - a table name that is one identifier containing a dot, or a string;
  - a parameterized floating-point type (`FLOAT(53)`);
  - any statement the parser-disagreement detector finds DuckDB reading differently.

## The rules

Each rule is a transformation stated together with its preconditions (`proof.RULES`).
A positive verdict records the rules its forms used.

| Rule | Transformation |
|---|---|
| `identifiers` | unquoted identifiers folded by the dialect's case rule |
| `inline` | a select-project-filter CTE / derived table (one source; no aggregate in any clause, no window, DISTINCT, GROUP BY, HAVING, LIMIT/OFFSET, set operation or subquery) substituted into its single consumer; its WHERE is conjoined |
| `star` | `SELECT *` expanded from the source's known columns |
| `unused_cte` | an unread CTE removed |
| `order` | the final query's `ORDER BY` without `LIMIT`/`OFFSET` removed, unless it holds the query's only aggregate (an inlined body's ORDER BY is refused) |
| `group_ordinal` | a GROUP BY ordinal replaced by the projection it names |
| `output_alias` | a GROUP BY / HAVING name that is a projection alias and **not** a source column replaced by that projection |
| `group_all` | GROUP BY ALL replaced by the non-aggregate, non-constant projections |
| `conjuncts` | WHERE read as the set of its top-level AND conjuncts |
| `between` | `x BETWEEN a AND b` (not SYMMETRIC) is `x >= a AND x <= b` |
| `commutative` | AND/OR chains flattened and ordered; the two operands of one `+`, `*`, `=`, `<>` ordered — never re-associated |
| `not_equality` | `NOT (a = b)` is `a <> b`, and the reverse |
| `table_alias` | the source's alias renamed `t0` |
| `parens` | parentheses re-derived from the parse tree: exactly those precedence needs, checked by re-parsing the canonical text |

What the rules deliberately do **not** do:

- no boolean simplification (it is unsound under NULLs);
- no constant folding (it is wrong for floating point);
- no re-association;
- no predicate movement across any boundary.

`SUM(a + (b + c))` and `SUM(a + b + c)` stay different, and so do `NOT (x = c AND x <> c)` and `TRUE`.

## Assumptions

These are recorded on every positive verdict (`proof.ASSUMPTIONS`):

- column types are those the catalog or registered model declares;
- expressions evaluate without raising errors, so AND/OR operand order changes no
  result;
- the final query's ORDER BY without a bound changes no value: it orders the result
  after every aggregate is computed;
- relation identity is the name the SQL wrote, completed only by the
  operator-supplied execution context;
- identifier case folding follows the dialect;
- `CURRENT_DATE` and `CURRENT_TIMESTAMP` are one value per execution.

## Versioning and requalification

- **Scans.** Every scan records `proof_policy`. Every CERTIFIED or ALIASING verdict
  carries `evidence.proof`, which holds `{policy, rules, assumptions}`.
- **Receipts.** Receipts carry `proof_policy` inside the signature.
- **Old scans.** A scan made under any other policy, including one that predates
  versioning, cannot carry its verdicts forward:
  - `check_freshness` reports it `stale` even when every file hash is unchanged
    ("rescan under it");
  - a receipt issued from it is `UNRESOLVED`. Each entry is mapped under the
    conclusions policy first and then downgraded (`[downgraded] Experimental
    comparison …`), with the engine's verdict only under `experimental` /
    `suspected`;
  - a receipt issued under another policy is never valid for current use.
- **Old receipts** remain valid signed records of what was said, under the policy
  that said it.

## The monitor

- **Inside the grammar.** CTEs and derived tables are inlined by `inline`, so the
  monitor's Shape A judges the proof form: one SELECT over one table.
- **Outside the grammar.** The optimizer's reading is judged only as context.
  - The result is always `unverifiable` (`shape_unsupported`), with the grammar's
    reason.
  - Would-be violations travel as `suspected` and are worded "under the
    optimizer's reading (a hypothesis)".
  - It is never conforming and never a violation. It is never "unregistered"
    either, since that would be a statement about its meaning.

## The parser-disagreement detector

`extraction/parse_agreement.py` asks DuckDB whether it reads a statement the way sqlglot
does:

1. It starts from the **original SQL**, before any optimization or transformation.
2. sqlglot's parse is rendered fully parenthesized (post-order), so the rendering states
   sqlglot's binding explicitly.
3. DuckDB serializes the original and that rendering (`json_serialize_sql`); query
   locations, operator-vs-call spelling (`%` / `MOD()`), constants' values and DuckDB's
   two spellings of one negated predicate are ignored.
4. The result is `disagrees`, `no_disagreement_found` or `not_checked`, with the dialect
   and the DuckDB and sqlglot versions.

What it is, and is not:

- `not_checked` covers another dialect, DuckDB not installed, input either parser
  rejects, and more than one statement. It is never agreement.
- It detects disagreement for DuckDB SQL only. Agreement says nothing about name binding,
  types, later transformations or another warehouse's semantics, and no DuckDB result
  qualifies Databricks or Snowflake.
- DuckDB's serialization can normalize syntax, so a disagreement it hides stays hidden. A
  rendering that renames a function or rewrites a literal can be flagged although both
  readings agree; it errs toward `disagrees`.
- A statement it finds DuckDB reading differently is **refused**. Every proof-grade form
  records its result, and experimental comparisons carry a summary
  (`proof.parse_agreement`). Passing it re-enables nothing.

It catches the confirmed `%` and IS cases, which is evidence about those cases only, not
about general correctness.

## How it is challenged

- **`semantix oracle`.**
  - Executes every pair the engine equates, compared exactly on adversarial DuckDB
    datasets.
  - Executes **each proof-grade query against its own trusted transformed SQL**
    (the proof form), so every rule is challenged query by query. The datasets
    cover NULLs, duplicates and ties, empty input, float cancellation and
    overflow, and integer extremes.
  - Includes CTE and derived-table inlining variants, with traps: swapped and
    renamed columns, precedence, NOT over a substituted comparison, filters on
    both sides, chains.
  - Generates combinations *inside* the grammar systematically
    (`oracle.generate_grammar_combinations`): 8 source wrappers × 16 measures ×
    10 filters × 7 groupings, 8,960 queries under `--full`.
    - Wrappers: direct, CTE `*`, renamed, swapped, computed, filtered, derived,
      chain.
    - Filters: with `BETWEEN`, `NOT`, OR and redundant parentheses.
    - Groupings: with ordinal, output alias, `ALL` and `HAVING`.

    Every one reaches proof grade. Each is executed against its proof form, and
    every pair the engine equates, across wrappers too, against each other.
  - Agreement challenges the rules; it does not prove them.
- **The detector**, on every generated query's original text; any proof-grade query it
  finds DuckDB reading differently fails the run. The generators also write round
  seventeen's former blind spots (`%`, IS chains, star modifiers, nested WITH, inner
  ORDER BY, constant GROUP BY keys, unquoted units).
- **`tests/data/engine_counterexamples.json`** (`tests/test_round_seventeen_fixes.py`).
  Replays round seventeen's recorded pairs and proof forms on their seeds. These are
  engine-correctness cases, never business-drift labels.
- **`tests/test_sweep_two_counterexamples.py`.**
  - Replays 271 recorded pairs from the second sweep's findings 1–35 (excluding
    the four over-refusals). Each pair executes differently on its seed, and none
    may be equal at proof grade.
  - Thirty of those findings are falsely equated by the former
    optimizer-as-proof path.
  - One (#3, an aggregate only in a derived table's ORDER BY) also broke the
    first version of this path; the `inline` and `order` preconditions exist
    because of it.

## Measured cost of the narrower scope

| Measure | Before (optimizer as proof) | `semantix-proof/2` (round 16) | after round 17 |
|---|---|---|---|
| bench equivalence detection | 40/40 | 40/40 | 39/39 (two BigQuery runs scoped out: that SQL is not BigQuery) |
| bench near-misses distinguished | 80/80 | 44/80 distinguished, 36 refused by name, 0 unproven | 43/79 distinguished, 36 refused by name, 0 unproven |
| acme demo proof-grade coverage | 100 % | 80 % (`ops_refund_rate` joins) | 80 % |
| oracle (default) proof-grade queries | — | 636 / 736 | 642 / 764 |
| oracle (full) proof-grade queries | — | 17,421 / 24,478 | 17,427 / 24,506 |
| parser disagreements found by the detector (full oracle) | — | — | 10 originals, all refused; 0 at proof grade |
| false equalities (bench, oracle, sweep and round-17 replays) | sweep 2 found 35 classes | 0 (the round-17 reviews then found ~14 classes) | 0 |

These figures challenge the rules on the generated and recorded cases. They do not
establish soundness, and positive verdicts stay suspended.

The bench lists every refused pair with its reason (`semantix bench`). These
refusals are the coverage backlog. A construct joins the grammar only with:

- a stated rule;
- its preconditions;
- oracle variants that execute original against transformed;
- regression cases.
