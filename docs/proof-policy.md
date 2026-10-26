# Proof policy `semantix-proof/2`

A positive verdict (CERTIFIED, ALIASING, monitor `conforming_use`) says two
definitions compute the same thing. That statement must come from rules whose
soundness can be argued one at a time, not from a general query optimizer.

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
  - columns of that table, literals and arithmetic;
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

## The rules

Each rule is a transformation stated together with its preconditions (`proof.RULES`).
A positive verdict records the rules its forms used.

| Rule | Transformation |
|---|---|
| `identifiers` | unquoted identifiers folded by the dialect's case rule |
| `inline` | a select-project-filter CTE / derived table (one source; no aggregate in any clause, no window, DISTINCT, GROUP BY, HAVING, LIMIT/OFFSET, set operation or subquery) substituted into its single consumer; its WHERE is conjoined |
| `star` | `SELECT *` expanded from the source's known columns |
| `unused_cte` | an unread CTE removed |
| `order` | `ORDER BY` without `LIMIT`/`OFFSET` removed, unless it holds the query's only aggregate |
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
- ORDER BY without a bound changes no value, because the grammar has no
  order-sensitive function;
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
    ("rescan to recertify");
  - a receipt issued from it is `UNRESOLVED`, with the original verdicts marked
    `superseded`;
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
  - Agreement challenges the rules; it does not prove them.
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

| Measure | Before (optimizer as proof) | `semantix-proof/2` |
|---|---|---|
| bench equivalence detection | 40/40 | 40/40 |
| bench near-misses distinguished | 80/80 | 44/80 distinguished, 36 refused by name, 0 unproven |
| acme demo proof-grade coverage | 100 % | 80 % (`ops_refund_rate` joins) |
| oracle (default) proof-grade queries | — | 274 / 372 |
| oracle (full) proof-grade queries | — | 8,598 / 15,655 |
| false certifications (bench, oracle, sweep replay) | sweep 2 found 35 classes | 0 |

The bench lists every refused pair with its reason (`semantix bench`). These
refusals are the coverage backlog. A construct joins the grammar only with:

- a stated rule;
- its preconditions;
- oracle variants that execute original against transformed;
- regression cases.
