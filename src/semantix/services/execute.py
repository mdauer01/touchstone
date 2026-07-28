"""Execution slice (§5 dynamic stage, the Phase-0-honest cut).

The doctrine says static difference is only a hypothesis; execution is what
upgrades it to fact. The full harness — warehouse credentials, sampling
windows, cost controls — is Phase 1. This slice covers the case that needs
none of that: when the estate's data is locally reproducible (a seed script,
a DuckDB file's worth of rows), materialize the models in dependency order,
run every competing definition of a contested concept, and attach the
measured divergence to the verdict. One number — "these two net_revenues
disagree by 30,000 (+35.7%)" — is what turns a semantic diff into a budget
conversation.

Degradation, as always, is graceful: duckdb missing, a model that fails to
materialize, or a definition that errors at runtime each degrade to a note in
the evidence, never a failed scan and never a fabricated number.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from semantix.models import Concept, Verdict, VerdictType
from semantix.services.relationships import qcol
from semantix.warehouse import qident


def annotate_with_execution(
    concepts: list[Concept],
    verdicts: list[Verdict],
    artifacts: list,  # ComputedArtifacts in topological order
    seed_path: str | Path,
    dialect: str,
) -> dict[str, Any]:
    """Measure every CONTRADICTION's competing definitions against seed data.
    Mutates the matching verdicts in place (evidence['measured'] + summary
    suffix) — call before the store persists them so measurement is part of
    the recorded verdict. Returns a scan-level execution summary."""
    summary: dict[str, Any] = {"engine": "duckdb", "seed": str(seed_path),
                               "measured": 0, "collisions": 0,
                               "declined": [], "errors": []}
    try:
        import duckdb
    except ImportError:
        summary["errors"].append("duckdb not installed — execution skipped")
        return summary

    con = duckdb.connect()
    try:
        con.execute(Path(seed_path).read_text())
    except Exception as e:  # noqa: BLE001 — seed is user input
        summary["errors"].append(f"seed failed: {e}")
        return summary

    # Materialize the estate as views, dependency order (adapter is topo-sorted).
    # Relations that fail are REMEMBERED: a seed script may already define an
    # object of the same name, so a failed materialization can leave a stale
    # relation behind, and measuring through it produces a number describing
    # neither definition — presented as fact.
    failed: set[str] = set()
    for artifact in artifacts:
        schema, _, _ = artifact.relation.rpartition(".")
        try:
            # Estate SQL is a FILE, not a trusted statement. Interpolating it
            # raw let a model body close the parenthesis and append its own
            # statements — silently redefining the relations a divergence is
            # then measured over. Re-render exactly one parsed statement.
            body = _single_statement(artifact.sql, dialect)
            if schema:
                con.execute(f"CREATE SCHEMA IF NOT EXISTS {qident(schema)}")
            con.execute(
                f"CREATE OR REPLACE VIEW {qident(artifact.relation)} AS ({body})"
            )
        except Exception as e:  # noqa: BLE001
            failed.add(artifact.relation)
            summary["errors"].append(f"materialize {artifact.relation}: {e}")
    summary["failed_relations"] = sorted(failed)

    by_id = {c.concept_id: c for c in concepts}
    for verdict in verdicts:
        # CONTRADICTION is measured to QUANTIFY a difference we already
        # suspect. CERTIFIED and ALIASING are measured for the opposite and
        # more important reason: they are the verdicts that can be wrong in
        # the unsurvivable direction, and execution is the only thing that can
        # falsify a canonical equality. Measuring only contradictions pointed
        # the one falsifying instrument at the one class of verdict that does
        # not need it.
        if verdict.verdict_type not in (VerdictType.CONTRADICTION,
                                        VerdictType.CERTIFIED,
                                        VerdictType.ALIASING):
            continue
        concept = by_id.get(verdict.concept_id)
        if concept is None:
            continue
        measured = _measure_concept(con, concept, verdict, failed, dialect)
        if measured is None:
            continue
        verdict.evidence["measured"] = measured
        if not measured.get("measured"):
            summary["declined"].append(f"{verdict.concept_label}: {measured['reason']}")
            continue

        # A falsified canonical equality outranks any divergence figure: it
        # means the engine's own proof of sameness was wrong.
        collisions = measured.get("collision_falsified", [])
        for collision in collisions:
            verdict.summary += (
                f" ⚠ {collision['vs']} hashes identically to "
                f"{collision.get('against', measured.get('baseline'))} but totals "
                f"{collision['actual']:,.0f} vs {collision['baseline']:,.0f} "
                f"— canonical equality unsound.")
            summary["collisions"] += 1
        if collisions and verdict.verdict_type in (VerdictType.CERTIFIED,
                                                   VerdictType.ALIASING):
            # The certification was the claim these definitions agree. The
            # data says they do not. Whatever the cause — an unsound
            # canonicalization or a construct we do not model — the one thing
            # that cannot stand is the certificate.
            verdict.verdict_type = VerdictType.CONTRADICTION
            verdict.severity = max(verdict.severity, 95)
            verdict.summary = (
                "Certification WITHDRAWN by execution: definitions the engine "
                "proved canonically identical compute different values on the "
                "same data. " + verdict.summary)

        worst = measured.get("max_divergence")
        if worst:
            pct = f" ({worst['pct']:+.1f}%)" if worst["pct"] is not None else ""
            verdict.summary += (
                f" Measured on seed data: {worst['vs']} totals "
                f"{worst['actual']:,.0f} vs baseline {worst['baseline']:,.0f}"
                f"{pct}.")
        summary["measured"] += 1
    return summary


def _single_statement(sql: str, dialect: str) -> str:
    """Re-render estate SQL as exactly one statement, or refuse.

    Anything else — a trailing `; DROP ...`, an unparseable body — is not a
    view definition and must not reach the warehouse.
    """
    statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    if len(statements) != 1:
        raise ValueError(
            f"expected exactly one statement, found {len(statements)}"
        )
    return statements[0].sql(dialect=dialect)


def _relations_read(sql: str, dialect: str) -> set[str] | None:
    """Physical relations a definition READS, lowercased and dotted.

    Returns None when the SQL cannot be parsed: a caller must not conclude
    anything from a name that merely appears somewhere in the text.
    """
    try:
        node = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 — arbitrary estate SQL
        return None
    if node is None:
        return None
    out: set[str] = set()
    for table in node.find_all(exp.Table):
        parts = [p for p in (table.catalog, table.db, table.name) if p]
        if not parts:
            continue
        # A CTE reference resolves in-query, not in the warehouse — but only
        # a CTE VISIBLE FROM THIS TABLE's scope. Collecting CTE names from the
        # whole tree let an unrelated subquery's CTE named `orders` mask a
        # genuine unqualified read of the physical `orders`, so the taint
        # guard concluded "reads nothing failed" and measured through a
        # relation that did not materialize.
        if len(parts) == 1 and parts[0].lower() in _visible_ctes(table):
            continue
        out.add(".".join(parts).lower())
    return out


def _visible_ctes(node: exp.Expression) -> set[str]:
    """CTE aliases in scope at `node`, walking outward from it."""
    names: set[str] = set()
    scope = node.parent_select
    while scope is not None:
        names |= {cte.alias_or_name.lower() for cte in (scope.ctes or [])}
        scope = scope.parent_select
    return names


def _same_relation(a: str, b: str) -> bool:
    """Do two relation names denote the same object?

    Compared component-wise with suffix tolerance (`db.analytics.orders`
    matches `analytics.orders`), never as substrings. A substring test made
    `raw.orders` match `raw.orders_backup` — and match any definition whose
    text merely CONTAINED the name, including inside a string literal or a
    comment, so an unrelated failure silently suppressed measurement.
    """
    pa = [p for p in a.lower().split(".") if p]
    pb = [p for p in b.lower().split(".") if p]
    # Suffix tolerance is for QUALIFICATION (`db.schema.t` vs `schema.t`), not
    # for a bare table name: matching `staging.orders` against an unqualified
    # `orders` made an unrelated failure suppress measurement and name a
    # relation the definition never touches. Both sides need at least a schema.
    n = min(len(pa), len(pb))
    if n < 2:
        return pa == pb
    return pa[-n:] == pb[-n:]


def _is_additive(measure_expr: str) -> bool:
    """Can this measure's output column be meaningfully SUMmed across rows?

    True for plain SUM and non-distinct COUNT. False for ratios, averages,
    COUNT(DISTINCT), medians and percentiles — summing those yields a number
    that means nothing, which must never be presented as measured evidence.
    Unparseable expressions are treated as non-additive: the safe direction is
    to decline to measure.
    """
    try:
        node = sqlglot.parse_one(measure_expr)
    except Exception:  # noqa: BLE001 — arbitrary estate SQL
        return False
    # Unwrap wrappers that preserve additivity, so an ordinary
    # `round(sum(x), 2)` or `coalesce(sum(x), 0)` is still measurable —
    # declining on those refused to measure most real revenue models.
    for _ in range(6):
        if isinstance(node, (exp.Alias, exp.Paren, exp.Cast, exp.TryCast, exp.Round)):
            node = node.this
        elif isinstance(node, exp.Coalesce):
            non_literal = [a for a in [node.this, *(node.expressions or [])]
                           if not isinstance(a, exp.Literal)]
            if len(non_literal) != 1:
                return False
            node = non_literal[0]
        else:
            break
    # An arithmetic combination of additive aggregates is itself additive:
    # SUM(gross) - SUM(refunds) is the commonest net-revenue shape there is,
    # and rejecting it meant those contradictions were never quantified while
    # the stated reason blamed non-additivity for an expression that is
    # additive. Scaling by a constant is additive too.
    if isinstance(node, (exp.Add, exp.Sub)):
        return _is_additive_node(node.this) and _is_additive_node(node.expression)
    if isinstance(node, (exp.Mul, exp.Div)):
        additive = [s for s in (node.this, node.expression) if not _is_literal(s)]
        return len(additive) == 1 and _is_additive_node(additive[0])
    return _is_additive_node(node)


def _is_literal(node: exp.Expression) -> bool:
    inner = node.this if isinstance(node, exp.Paren) else node
    return isinstance(inner, exp.Literal)


def _is_additive_node(node: exp.Expression) -> bool:
    if isinstance(node, exp.Paren):
        return _is_additive_node(node.this)
    if isinstance(node, (exp.Add, exp.Sub)):
        return _is_additive_node(node.this) and _is_additive_node(node.expression)
    if not isinstance(node, (exp.Sum, exp.Count)):
        return False
    # A FILTER (WHERE ...) clause restricts which rows the aggregate sees; the
    # result still sums across partitions, so it stays additive.
    # DISTINCT does not: it de-duplicates before aggregating, so partial
    # results do not recompose — true of SUM(DISTINCT x) exactly as much as
    # COUNT(DISTINCT x), which is the half that was checked.
    return node.find(exp.Distinct) is None


def _output_is_not_summable(sql: str, dialect: str) -> bool:
    """Would SUM() over this definition's rows produce a meaningless number?

    True for a set operation (the stored canonical_sql is one branch, so the
    total describes neither definition) and for any grouping that emits
    subtotal rows (summing counts the same money twice). Unparseable SQL is
    treated as not-summable: declining to measure is the safe direction.
    """
    try:
        node = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 — arbitrary estate SQL
        return True
    if node is None:
        return True
    if node.find(exp.Union, exp.Except, exp.Intersect) is not None:
        return True
    for group in node.find_all(exp.Group):
        if any(group.args.get(k) for k in ("rollup", "cube", "grouping_sets", "totals")):
            return True
    return False


def _diverges(a: float, b: float, rel: float = 1e-9) -> bool:
    """Do two totals of the SAME canonical definition genuinely disagree?

    Compared relatively, not absolutely: an absolute 1e-9 epsilon is tighter
    than ordinary float summation noise on currency (700.70 sums to
    700.7000000000001), so identical definitions were being reported as a
    falsified canonical equality — the loudest alarm the engine has.

    A flat 1e-9 was calibrated to float64 SUMMATION noise, but warehouse
    DECIMAL arithmetic is exact and float64 resolution near 5e9 is ~1e-6 —
    so on a $2bn book the check went blind six orders of magnitude above
    actual noise and a deterministic $1.50 difference read as sound. The
    tolerance is now the larger of the relative term and one float ULP at the
    magnitude being compared, which is the real noise floor.
    """
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) > max(scale * rel, math.ulp(scale) * 4)


def _total(con, canonical_sql: str, column: str) -> tuple[float | None, str | None]:
    """Grand total of the measure column over the definition's full output —
    grain-independent, so monthly and weekly variants stay comparable."""
    try:
        # The column name comes from an alias in estate SQL, i.e. from a file
        # we do not control — quote it the same way every other warehouse call
        # does rather than interpolating it raw.
        row = con.execute(
            f'SELECT SUM({qcol(column)}) FROM ({canonical_sql}) _semantix_q'
        ).fetchone()
        value = row[0] if row else None
        return (float(value) if value is not None else None), None
    except Exception as e:  # noqa: BLE001 — arbitrary estate SQL
        return None, str(e)


def _measure_concept(con, concept: Concept, verdict: Verdict,
                     failed: set[str] | None = None,
                     dialect: str = "duckdb") -> dict[str, Any] | None:
    """Run every member definition of a contested concept; report totals and
    the largest divergence from the baseline definition."""
    members: list[dict[str, Any]] = []
    for claim in concept.claims:
        if claim.expressible and claim.canonical_sql:
            members.append({"where": claim.model or claim.name, "kind": "declared",
                            "column": claim.name, "sql": claim.canonical_sql,
                            "core_hash": claim.core.hash(),
                            "measure_expr": claim.core.measure_expr})
    for fp in concept.fingerprints:
        members.append({"where": fp.relation, "kind": "computed",
                        "column": fp.concept_candidate, "sql": fp.canonical_sql,
                        "core_hash": fp.core.hash(),
                        "measure_expr": fp.core.measure_expr})
    if len(members) < 2:
        return None

    # Divergence is measured by summing each definition's output column, which
    # is only meaningful for an ADDITIVE measure. Summing a ratio, an average
    # or a COUNT(DISTINCT) produces a number with no interpretation — and then
    # reports it as "measured on seed data", i.e. fabricates evidence. Refuse
    # instead: an unmeasured contradiction is honest, a fabricated one is not.
    # A definition reading a relation that did not materialize cannot be
    # measured: whatever is under that name is not what this definition means.
    tainted: set[str] = set()
    opaque: list[str] = []
    for m in members:
        reads = _relations_read(m["sql"] or "", dialect)
        if reads is None:
            opaque.append(m["where"])
            continue
        tainted |= {f for f in (failed or ())
                    if any(_same_relation(f, r) for r in reads)}
    if tainted:
        return {
            "method": "duckdb-seed-execution",
            "measured": False,
            "reason": ("definitions read relations that failed to materialize, so any "
                       "number would describe neither of them: "
                       + ", ".join(sorted(tainted))),
        }
    if opaque and failed:
        # We cannot see what these read, and something in the estate is
        # broken — so we cannot rule out that they read it.
        return {
            "method": "duckdb-seed-execution",
            "measured": False,
            "reason": ("could not determine which relations these definitions read "
                       "while part of the estate failed to materialize: "
                       + ", ".join(sorted(set(opaque)))),
        }

    non_additive = sorted({m["measure_expr"] for m in members
                           if not _is_additive(m["measure_expr"])})
    if non_additive:
        return {
            "method": "duckdb-seed-execution",
            "measured": False,
            "reason": ("measure is not additive, so summing its output would not "
                       "be a meaningful comparison: " + "; ".join(non_additive)),
        }

    # `_is_additive` judges the measure EXPRESSION; `_total` sums the
    # definition's OUTPUT ROWS. Two shapes break that correspondence:
    #
    #  * a SET OPERATION — the fingerprint's canonical_sql is ONE BRANCH of the
    #    union, so a `us UNION ALL eu` model measured 100 instead of 1,900 and
    #    manufactured a -94.7% gap against definitions that agree exactly.
    #    UNION ALL across regions is the commonest dbt model shape there is.
    #  * ROLLUP/CUBE/GROUPING SETS — the output CONTAINS its own subtotals, so
    #    summing it double-counts and reports +100% between definitions that
    #    agree.
    unsummable = sorted({m["where"] for m in members
                         if _output_is_not_summable(m["sql"] or "", dialect)})
    if unsummable:
        return {
            "method": "duckdb-seed-execution",
            "measured": False,
            "reason": ("the definition's output rows cannot be summed into a "
                       "comparable total (set operation, or a grouping that emits "
                       "its own subtotals): " + ", ".join(unsummable)),
        }

    baseline = next((m for m in members if m["kind"] == "declared"), members[0])
    totals = []
    for m in members:
        value, error = _total(con, m["sql"], m["column"])
        totals.append({"where": m["where"], "kind": m["kind"], "total": value,
                       **({"error": error} if error else {})})

    # POSITIVE-EVIDENCE FLOOR. The relationship layer has one; execution had
    # none, so an empty seed produced `measured: true, collisions: 0` over
    # totals of [null, null] — and the CI assertion that certifications are
    # falsified against real data passed on zero rows, proving nothing. A
    # number we could not compute is not evidence that two definitions agree.
    measurable = [t for t in totals if t["total"] is not None]
    if len(measurable) < 2:
        return {
            "method": "duckdb-seed-execution",
            "measured": False,
            "reason": ("fewer than two definitions produced a number on this seed "
                       "data, so nothing could be compared"),
            "totals": totals,
        }

    # Falsification compares every SAME-HASH pair, not just each member against
    # the baseline. Two non-baseline members sharing a core — exactly the pair
    # an ALIASING certificate names — were never compared to each other, so a
    # falsified equality between them survived untouched. And a NULL baseline
    # used to disable the whole check, leaving the contradicting evidence
    # printed beside a surviving certificate.
    collisions = []
    by_hash: dict[str, list[dict]] = {}
    for t, m in zip(totals, members):
        if t["total"] is not None:
            by_hash.setdefault(m["core_hash"], []).append(t)
    for group in by_hash.values():
        anchor = group[0]
        for other in group[1:]:
            if _diverges(other["total"], anchor["total"]):
                collisions.append({
                    "vs": other["where"], "against": anchor["where"],
                    "baseline": anchor["total"], "actual": other["total"],
                    "note": "definitions hash identically but compute different "
                            "values — the canonical equality is unsound",
                })

    base_total = next((t["total"] for t, m in zip(totals, members)
                       if m is baseline), None)
    worst = None
    if base_total is not None:
        for t, m in zip(totals, members):
            if m is baseline or t["total"] is None:
                continue
            if m["core_hash"] == baseline["core_hash"]:
                continue  # same-hash pairs are falsification, not divergence
            abs_div = abs(t["total"] - base_total)
            if worst is None or abs_div > worst["abs"]:
                worst = {
                    "vs": t["where"],
                    "baseline": base_total,
                    "actual": t["total"],
                    "abs": abs_div,
                    "pct": (100.0 * (t["total"] - base_total) / base_total
                            if base_total else None),
                }

    return {
        "method": "duckdb-seed-execution",
        "measured": True,
        "baseline": baseline["where"],
        "totals": totals,
        "max_divergence": worst,
        **({"collision_falsified": collisions} if collisions else {}),
    }
