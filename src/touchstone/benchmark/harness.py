"""Test harness for `touchstone check`: expected review states, challenged by
execution, and the score of a check's findings against them.

    python harness.py challenge TEST_DIR          # before any verdict is opened
    python harness.py score FINDINGS TEST_DIR     # findings.jsonl from `touchstone check`

A test directory holds `pilot/` (the package `check` reads), `expected.yaml` (the
expected state of every case, with its reason, written from the statement and the
definition) and `ingredients.yaml` (a case's own extra filters, groupings and joins, for
the reference query of ../v4_fresh/answer_key.py).

Expectations decide; execution only challenges them, on local TPC-H-shaped data, and
only where the calculation should be sensitive. A difference on a fixture shows a
difference on that fixture, not which review state is right. Challenges:

  review_needed     with ingredients: the statement and the reference differ on clean data
  no_issue_flagged  with ingredients: they agree on clean data and on dirty data that
                    respects the stated facts (NULLs, odd flags, duplicate line items)
  could_not_assess  `violates: [part|supplier]`: violating that unstated relationship fact
                    (a duplicated or unmatched lookup key) changes the statement's result
  every ingredient  the data can show a dropped required filter

and the fixtures must satisfy the definition's stated relationship facts. Nothing here
assumes a meaning for an undeclared join, and nothing runs on a warehouse.

Score: recall over the expected review_needed cases (a could_not_assess or failed answer
on one is a miss), precision of the review_needed answers, and the abstention rate, per
distinct case. With a SEALED.md in the test directory the findings must match a hash in
it. Release bar for 0.1.0 (fixed before the fresh test): recall 1.0, precision >= 0.8.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import duckdb
import yaml

sys.path.insert(0, str(Path(__file__).parent / "v4_fresh"))
import answer_key as ak  # noqa: E402 — the local data and reference queries

STATES = ("review_needed", "could_not_assess", "no_issue_flagged")
# A violation of each unstated relationship fact: duplicated lookup rows, and line items
# whose key matches no lookup row.
VIOLATE = {
    "part": ["INSERT INTO samples.tpch.part SELECT * FROM samples.tpch.part WHERE p_partkey % 5 = 0",
             "UPDATE samples.tpch.lineitem SET l_partkey = -l_partkey WHERE l_orderkey % 7 = 0"],
    "supplier": ["INSERT INTO samples.tpch.supplier SELECT * FROM samples.tpch.supplier "
                 "WHERE s_suppkey % 5 = 0",
                 "UPDATE samples.tpch.lineitem SET l_suppkey = -l_suppkey WHERE l_orderkey % 7 = 0"],
}
# The dev definition's stated facts: along the declared keys, exactly one match.
CHAIN = [("lineitem", "l_orderkey", "orders", "o_orderkey"),
         ("orders", "o_custkey", "customer", "c_custkey"),
         ("customer", "c_nationkey", "nation", "n_nationkey"),
         ("nation", "n_regionkey", "region", "r_regionkey")]


def _clean() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for sql in ak.CLEAN:
        con.execute(sql)
    ak.governed_rows(con)
    return con


def _facts_hold(con) -> list[str]:
    bad = []
    for child, fk, parent, pk in CHAIN:
        n = con.execute(f"""SELECT COUNT(*) FROM samples.tpch.{child} c
            LEFT JOIN (SELECT {pk}, COUNT(*) AS k FROM samples.tpch.{parent} GROUP BY 1) p
            ON c.{fk} = p.{pk} WHERE p.k IS DISTINCT FROM 1""").fetchone()[0]
        if n:
            bad.append(f"{n} {child} row(s) do not match exactly one {parent}")
    return bad


def _metric(statement: str) -> str | None:
    return ak.presented_as(statement)[1]


def _run(con, statement: str, spec: dict) -> list:
    keys = [g.rsplit(" AS ", 1)[1] for g in spec.get("group_by", [])]
    return ak.run_statement(con, statement, keys, _metric(statement))


def _reference(con, statement: str, spec: dict, rows: str = "governed") -> list:
    return ak._rows(con, ak.reference(spec, _metric(statement), rows))


def challenge(test: Path) -> dict:
    from touchstone.services.check import export_records

    expected = yaml.safe_load((test / "expected.yaml").read_text())
    ingredients = yaml.safe_load((test / "ingredients.yaml").read_text()) or {}
    _, _, records, _ = export_records(test / "pilot")
    statements = {r["record_id"]: r["execution"]["statement"] for r in records}
    conflicts, checked = [], Counter()
    if set(expected) != set(statements):
        conflicts.append("expected.yaml and the exported records cover different cases: "
                         f"{sorted(set(expected) ^ set(statements))}")
    for rid, e in expected.items():
        if e.get("expected") not in STATES or not e.get("reason"):
            conflicts.append(f"{rid}: needs an expected state and a reason")

    con = _clean()
    conflicts += [f"clean fixtures: {b}" for b in _facts_hold(con)]
    same = {}
    for rid, spec in ingredients.items():
        statement = statements[rid]
        ref = _reference(con, statement, spec)
        same[rid] = {"clean": _run(con, statement, spec) == ref}
        if _reference(con, statement, spec, "governed_all") == ref:
            conflicts.append(f"{rid}: the data cannot show a dropped required filter")
        checked["required filter testable"] += 1
    for sql in ak.DIRTY:
        con.execute(sql)
    conflicts += [f"dirty fixtures: {b}" for b in _facts_hold(con)]
    for rid, spec in ingredients.items():
        same[rid]["dirty"] = _run(con, statements[rid], spec) == _reference(con, statements[rid], spec)

    for rid, e in expected.items():
        state, spec = e.get("expected"), ingredients.get(rid)
        if state == "review_needed" and spec:
            checked["review_needed differs on clean data"] += 1
            if same[rid]["clean"]:
                conflicts.append(f"{rid}: expected review_needed, but the statement equals "
                                 f"the reference on clean data")
        if state == "no_issue_flagged" and spec:
            checked["no_issue_flagged agrees on clean and dirty data"] += 1
            if not (same[rid]["clean"] and same[rid]["dirty"]):
                conflicts.append(f"{rid}: expected no_issue_flagged, but the statement differs "
                                 f"from the reference ({same[rid]})")
        for lookup in e.get("violates") or []:
            checked[f"could_not_assess sensitive to {lookup}"] += 1
            base, broken = _clean(), _clean()
            for sql in VIOLATE[lookup]:
                broken.execute(sql)
            spec = spec or {}
            if _run(base, statements[rid], spec) == _run(broken, statements[rid], spec):
                conflicts.append(f"{rid}: expected could_not_assess for an unstated {lookup} "
                                 f"fact, but violating it does not change the result")
    return {"cases": len(expected), "expected": dict(Counter(e.get("expected")
                                                          for e in expected.values())),
            "checked": dict(checked), "conflicts": conflicts}


def score(findings: Path, test: Path) -> dict:
    expected = {k: v["expected"] for k, v in
                yaml.safe_load((test / "expected.yaml").read_text()).items()}
    sealed = test / "SEALED.md"
    if sealed.exists():
        hashes = set(re.findall(r"\b[0-9a-f]{64}\b", sealed.read_text()))
        if hashlib.sha256(findings.read_bytes()).hexdigest() not in hashes:
            raise SystemExit("the findings do not match a sealed hash")
    verdicts = {v["record_id"]: v for v in map(json.loads, open(findings))}
    if set(verdicts) != set(expected):
        raise SystemExit("the findings and expected.yaml cover different cases")
    model = {rid: v.get("label") or "not_assessed" for rid, v in verdicts.items()}
    review = [r for r, s in expected.items() if s == "review_needed"]
    flagged = [r for r, s in model.items() if s == "review_needed"]
    hits = [r for r in review if model[r] == "review_needed"]
    recall = len(hits) / len(review) if review else None
    precision = len(hits) / len(flagged) if flagged else None
    return {
        "cases": len(expected),
        "recall": recall, "precision": precision,
        "abstention": sum(m in ("could_not_assess", "not_assessed") for m in model.values())
        / len(model),
        "release_bar_met": recall == 1.0 and precision is not None and precision >= 0.8,
        "missed": {r: model[r] for r in review if model[r] != "review_needed"},
        "false_alarms": {r: expected[r] for r in flagged if expected[r] != "review_needed"},
        "expected_vs_model": dict(Counter(f"{expected[r]} / {model[r]}" for r in expected)),
        "disagreements": [{"record_id": r, "expected": expected[r], "model": model[r],
                           "evidence": verdicts[r].get("evidence"), "error": verdicts[r].get("error")}
                          for r in sorted(expected) if expected[r] != model[r]],
    }


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "challenge":
        result = challenge(Path(sys.argv[2]))
        print(json.dumps(result, indent=1))
        sys.exit(1 if result["conflicts"] else 0)
    if len(sys.argv) == 4 and sys.argv[1] == "score":
        print(json.dumps(score(Path(sys.argv[2]), Path(sys.argv[3])), indent=1))
        sys.exit(0)
    sys.exit(__doc__)
