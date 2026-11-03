"""Open equivalence benchmark: prove the prover (§9's coverage risk, made public).

Runs the shipped corpus of SQL pairs through the exact canonicalize →
fingerprint path a scan uses — across a dialect matrix, because "system-
agnostic" is itself a claim that needs certifying, and dialect quirks are
where canonicalizers break. Reports, per dialect and in aggregate:

* equivalence detection rate — same-math pairs proven identical;
* known gaps — rewrites we deliberately do not normalize yet, which must
  degrade to a difference-hypothesis (missing toward safety);
* false certifications — near-miss pairs that hash-equal. This count must be
  zero: falsely certifying two different definitions as identical is the one
  failure a verification engine cannot survive, so `semantix bench` exits
  non-zero on any.

The corpus lives in the repo (benchmark/equivalence.yaml inside the package)
so every number quoted about the canonicalizer is reproducible by anyone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from semantix.adapters.loadutil import load_yaml
from semantix.extraction.canonicalize import canonicalize
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.proof import POLICY_ID, is_policy_gap
from semantix.extraction.registry import SchemaRegistry
from semantix.models import PROOF_LEVELS, Provenance

DEFAULT_CORPUS = Path(__file__).parent / "benchmark" / "equivalence.yaml"


def load_corpus(path: str | Path | None = None) -> dict[str, Any]:
    return load_yaml(Path(path or DEFAULT_CORPUS))


def _flatten_catalog(nested: dict) -> dict[str, dict[str, str]]:
    flat: dict[str, dict[str, str]] = {}
    for schema, tables in (nested or {}).items():
        for table, columns in (tables or {}).items():
            flat[f"{schema}.{table}"] = {str(c): str(t) for c, t in (columns or {}).items()}
    return flat


def _core_hash(sql: str, registry: SchemaRegistry, dialect: str,
               site: str) -> tuple[str | None, str, str | None]:
    """(core hash or None, canon level, grammar refusal or None) for one side
    of a pair. The refusal is set when the only thing between the query and
    proof grade is the proof grammar (semantix-proof/2) — a stated scope
    limit, reported apart from blind spots.

    The level reported is the FINGERPRINT's, not the artifact's: the
    completeness gate downgrades individual measures to INCOMPLETE when the
    core cannot represent something the query uses, and the benchmark must
    see that downgrade or it will score a blind spot as a success.
    """
    canon = canonicalize(sql, registry, dialect)
    fps = extract_fingerprints(canon, f"bench.{site}",
                               Provenance("bench", site), dialect, registry)
    if not fps:
        return None, canon.level.value, None
    fp = fps[0]
    refused = (sorted(fp.gaps)[0] if fp.gaps and all(is_policy_gap(g) for g in fp.gaps)
               else None)
    return fp.core.hash(), fp.canon_level, refused


# The default matrix certifies the cross-platform claim instead of declaring
# it: the same corpus must hold on every dialect a real estate might speak.
DEFAULT_DIALECTS = ["duckdb", "databricks", "snowflake", "bigquery"]


def _run_dialect(corpus: dict[str, Any], dialect: str) -> dict[str, Any]:
    """One full corpus pass under one dialect."""
    registry = SchemaRegistry(_flatten_catalog(corpus.get("catalog")))

    rows: list[dict[str, Any]] = []
    levels: list[str] = []
    for case in corpus["cases"]:
        # A case may scope itself to specific dialects (e.g. syntax that
        # cannot be expressed elsewhere); default is the whole matrix.
        if case.get("dialects") and dialect not in case["dialects"]:
            continue
        ha, level_a, refused_a = _core_hash(case["a"], registry, dialect, f"{case['name']}_a")
        hb, level_b, refused_b = _core_hash(case["b"], registry, dialect, f"{case['name']}_b")
        levels += [level_a, level_b]
        proof = (level_a in PROOF_LEVELS and level_b in PROOF_LEVELS
                 and ha is not None and hb is not None)
        equal = proof and ha == hb
        # Every side short of proof grade is short ONLY because of the
        # grammar: refused by design, with the reason. Any other shortfall is
        # a blind spot and stays unproven.
        short = [r for r, lv in ((refused_a, level_a), (refused_b, level_b))
                 if lv not in PROOF_LEVELS]
        refusal = short[0] if short and all(short) and not proof else None

        expect = case["expect"]
        known_gap = bool(case.get("known_gap"))
        if expect == "equivalent" and known_gap:
            outcome = "gap_closed" if equal else "gap_confirmed"
        elif refusal is not None:
            outcome = "refused"
        elif expect == "equivalent":
            outcome = "detected" if equal else "missed"
        elif not proof:
            # A near-miss pair that never reached proof grade proved NOTHING —
            # counting it as "distinguished" would report a verification
            # success exactly when the prover is broken (e.g. a dialect whose
            # parsing wholesale fails).
            outcome = "unproven"
        else:
            outcome = "false_certification" if equal else "distinguished"

        rows.append({"name": case["name"], "category": case["category"],
                     "expect": expect, "known_gap": known_gap,
                     "proof_grade": proof, "equal": equal, "outcome": outcome,
                     **({"refused": refusal} if outcome == "refused" else {}),
                     **({"note": case["note"]} if case.get("note") else {})})

    # Detection is reported over the SUPPORTED grammar; pairs it refuses are
    # the scope's cost, listed with their reasons — never folded into either.
    scored_equiv = [r for r in rows if r["expect"] == "equivalent" and not r["known_gap"]
                    and r["outcome"] != "refused"]
    detected = sum(1 for r in scored_equiv if r["outcome"] == "detected")
    different = [r for r in rows if r["expect"] == "different"]
    false_certs = [r for r in rows if r["outcome"] == "false_certification"]
    return {
        "cases": len(rows),
        "equivalence_detection": {
            "detected": detected,
            "of": len(scored_equiv),
            "rate": round(detected / len(scored_equiv), 4) if scored_equiv else None,
            "missed": [r["name"] for r in scored_equiv if r["outcome"] == "missed"],
            "refused": [r["name"] for r in rows
                        if r["expect"] == "equivalent" and r["outcome"] == "refused"],
        },
        "refused": {r["name"]: r["refused"] for r in rows if r["outcome"] == "refused"},
        "known_gaps": {
            "confirmed": [r["name"] for r in rows if r["outcome"] == "gap_confirmed"],
            "closed": [r["name"] for r in rows if r["outcome"] == "gap_closed"],
        },
        "near_misses": {
            "distinguished": sum(1 for r in different
                                 if r["outcome"] == "distinguished"),
            "of": len(different),
            "unproven": [r["name"] for r in different if r["outcome"] == "unproven"],
            "refused": [r["name"] for r in different if r["outcome"] == "refused"],
        },
        "false_certifications": [r["name"] for r in false_certs],
        # Raw counts so aggregation never reconstructs from rounded percentages.
        "level_counts": {
            "proof": sum(1 for lv in levels if lv in PROOF_LEVELS),
            "total": len(levels),
        },
        "proof_grade_coverage_pct": round(
            100.0 * sum(1 for lv in levels if lv in PROOF_LEVELS) / len(levels), 1)
            if levels else 0.0,
        "results": rows,
    }


def run_bench(corpus_path: str | Path | None = None,
              dialects: list[str] | None = None) -> dict[str, Any]:
    """Run the corpus across a dialect matrix.

    Top-level numbers aggregate over every (dialect, case) run — the gate is
    zero false certifications in EVERY dialect. `by_dialect` holds each
    dialect's full result for drill-down.
    """
    corpus = load_corpus(corpus_path)
    # dict.fromkeys: dedupe while preserving order, so result["dialects"]
    # always matches by_dialect's keys.
    matrix = list(dict.fromkeys(dialects or corpus.get("dialects") or DEFAULT_DIALECTS))
    by_dialect = {d: _run_dialect(corpus, d) for d in matrix}

    def total(path: Callable[[dict], int]) -> int:
        return sum(path(r) for r in by_dialect.values())

    detected = total(lambda r: r["equivalence_detection"]["detected"])
    scored = total(lambda r: r["equivalence_detection"]["of"])
    missed = [f"{name}@{d}" for d, r in by_dialect.items()
              for name in r["equivalence_detection"]["missed"]]
    false_certs = [f"{name}@{d}" for d, r in by_dialect.items()
                   for name in r["false_certifications"]]
    unproven = [f"{name}@{d}" for d, r in by_dialect.items()
                for name in r["near_misses"]["unproven"]]
    refused_equiv = [f"{name}@{d}" for d, r in by_dialect.items()
                     for name in r["equivalence_detection"]["refused"]]
    refused_near = [f"{name}@{d}" for d, r in by_dialect.items()
                    for name in r["near_misses"]["refused"]]
    # Grammar refusals are properties of the policy: one reason per case.
    refusals = {name: why for r in by_dialect.values() for name, why in r["refused"].items()}
    # Known gaps are properties of the canonicalizer, not of a dialect run —
    # dedupe by name so "1 confirmed gap" means one gap, not one per dialect.
    # A gap still confirmed in ANY dialect is not closed.
    gaps_confirmed = sorted({n for r in by_dialect.values()
                             for n in r["known_gaps"]["confirmed"]})
    gaps_closed = sorted({n for r in by_dialect.values()
                          for n in r["known_gaps"]["closed"]}
                         - set(gaps_confirmed))

    proof_runs = total(lambda r: r["level_counts"]["proof"])
    level_runs = total(lambda r: r["level_counts"]["total"])

    return {
        "corpus": str(corpus_path or DEFAULT_CORPUS),
        "dialects": matrix,
        "cases": len(corpus["cases"]),
        "by_dialect": by_dialect,
        "equivalence_detection": {
            "detected": detected,
            "of": scored,
            "rate": round(detected / scored, 4) if scored else None,
            "missed": missed,
            "refused": refused_equiv,
        },
        "proof_policy": POLICY_ID,
        "refused": dict(sorted(refusals.items())),
        "known_gaps": {"confirmed": gaps_confirmed, "closed": gaps_closed},
        "near_misses": {
            "distinguished": total(lambda r: r["near_misses"]["distinguished"]),
            "of": total(lambda r: r["near_misses"]["of"]),
            "unproven": unproven,
            "refused": refused_near,
        },
        "false_certifications": false_certs,
        "proof_grade_coverage_pct": round(100.0 * proof_runs / level_runs, 1)
            if level_runs else 0.0,
    }
