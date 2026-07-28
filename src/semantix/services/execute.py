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
            if schema:
                con.execute(f"CREATE SCHEMA IF NOT EXISTS {qident(schema)}")
            con.execute(
                f"CREATE OR REPLACE VIEW {qident(artifact.relation)} AS ({artifact.sql})"
            )
        except Exception as e:  # noqa: BLE001
            failed.add(artifact.relation)
            summary["errors"].append(f"materialize {artifact.relation}: {e}")
    summary["failed_relations"] = sorted(failed)

    by_id = {c.concept_id: c for c in concepts}
    for verdict in verdicts:
        if verdict.verdict_type != VerdictType.CONTRADICTION:
            continue
        concept = by_id.get(verdict.concept_id)
        if concept is None:
            continue
        measured = _measure_concept(con, concept, verdict, failed)
        if measured is None:
            continue
        verdict.evidence["measured"] = measured
        if not measured.get("measured"):
            summary["declined"].append(f"{verdict.concept_label}: {measured['reason']}")
            continue

        # A falsified canonical equality outranks any divergence figure: it
        # means the engine's own proof of sameness was wrong.
        for collision in measured.get("collision_falsified", []):
            verdict.summary += (
                f" ⚠ {collision['vs']} hashes identically to "
                f"{measured['baseline']} but totals {collision['actual']:,.0f} "
                f"vs {collision['baseline']:,.0f} — canonical equality unsound.")
            summary["collisions"] += 1

        worst = measured.get("max_divergence")
        if worst:
            pct = f" ({worst['pct']:+.1f}%)" if worst["pct"] is not None else ""
            verdict.summary += (
                f" Measured on seed data: {worst['vs']} totals "
                f"{worst['actual']:,.0f} vs baseline {worst['baseline']:,.0f}"
                f"{pct}.")
        summary["measured"] += 1
    return summary


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
    if isinstance(node, exp.Alias):
        node = node.this
    if not isinstance(node, (exp.Sum, exp.Count)):
        return False
    # DISTINCT de-duplicates before aggregating, so partial results do not
    # recompose — true of SUM(DISTINCT x) exactly as much as COUNT(DISTINCT x),
    # which is the half that was checked.
    return node.find(exp.Distinct) is None


def _diverges(a: float, b: float, rel: float = 1e-9) -> bool:
    """Do two totals of the SAME canonical definition genuinely disagree?

    Compared relatively, not absolutely: an absolute 1e-9 epsilon is tighter
    than ordinary float summation noise on currency (700.70 sums to
    700.7000000000001), so identical definitions were being reported as a
    falsified canonical equality — the loudest alarm the engine has.
    """
    return abs(a - b) > max(abs(a), abs(b), 1.0) * rel


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
                     failed: set[str] | None = None) -> dict[str, Any] | None:
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
    tainted = sorted({
        relation
        for relation in (failed or ())
        for m in members
        if relation in (m["sql"] or "")
    })
    if tainted:
        return {
            "method": "duckdb-seed-execution",
            "measured": False,
            "reason": ("definitions read relations that failed to materialize, so any "
                       "number would describe neither of them: " + ", ".join(tainted)),
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

    baseline = next((m for m in members if m["kind"] == "declared"), members[0])
    totals = []
    for m in members:
        value, error = _total(con, m["sql"], m["column"])
        totals.append({"where": m["where"], "kind": m["kind"], "total": value,
                       **({"error": error} if error else {})})

    base_total = next((t["total"] for t, m in zip(totals, members)
                       if m is baseline), None)
    worst = None
    collisions = []
    if base_total is not None:
        for t, m in zip(totals, members):
            if m is baseline or t["total"] is None:
                continue
            same_hash = m["core_hash"] == baseline["core_hash"]
            abs_div = abs(t["total"] - base_total)
            if same_hash:
                # Members we called canonically IDENTICAL must produce the same
                # number. If they do not, the equality was a hash collision and
                # a certification somewhere is wrong — this is the one case
                # execution can falsify rather than merely quantify, so it is
                # measured rather than skipped.
                if _diverges(t["total"], base_total):
                    collisions.append({
                        "vs": t["where"], "baseline": base_total, "actual": t["total"],
                        "note": "definitions hash identically but compute different "
                                "values — the canonical equality is unsound",
                    })
                continue
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
