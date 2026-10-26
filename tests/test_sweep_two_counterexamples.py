"""Every confirmed counterexample of the second sweep, kept as a regression.

Sweep 2 (after 6e831d0) confirmed 35 extraction and optimizer findings in
which two queries executing differently were certified equal. The pairs here
were recorded from the finders' own scripts: each executed differently on its
seed. Under semantix-proof/2 none of them may be equal at proof grade — most
are now outside the grammar and refused by name; the rest are distinguished.
One of them (#3, an aggregate only in a derived table's ORDER BY) also broke
the first version of the proof path, and is the reason the `inline` and
`order` rules carry their aggregate preconditions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from semantix.extraction.canonicalize import canonicalize
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import PROOF_LEVELS, Provenance

duckdb = pytest.importorskip("duckdb")

DATA = json.loads((Path(__file__).parent / "data" / "sweep2_counterexamples.json")
                  .read_text(encoding="utf-8"))


def _rows(con, sql):
    return sorted(map(repr, con.execute(sql).fetchall()))


def _fps(sql: str, catalog: dict):
    registry = SchemaRegistry(catalog)
    registry.set_estate_depth(list(catalog))
    canon = canonicalize(sql, registry, "duckdb")
    return extract_fingerprints(canon, "x", Provenance("q", "x"), "duckdb", registry)


def _proof(fp) -> bool:
    return fp.canon_level in PROOF_LEVELS and not fp.gaps


@pytest.mark.parametrize("case", DATA["cases"],
                         ids=[f"f{c['finding']:02d}-{i}" for i, c in enumerate(DATA["cases"])])
def test_counterexample_is_never_equal_at_proof_grade(case):
    con = duckdb.connect()
    con.execute(DATA["seeds"][case["seed"]])
    assert _rows(con, case["a"]) != _rows(con, case["b"]), "not a counterexample"
    catalog = DATA["catalogs"][case["catalog"]]
    fa, fb = _fps(case["a"], catalog), _fps(case["b"], catalog)
    for x in fa:
        for y in fb:
            if x.concept_candidate != y.concept_candidate:
                continue
            assert not (_proof(x) and _proof(y) and x.core.hash() == y.core.hash()), \
                (case["a"], case["b"])


def test_the_fixture_covers_every_extraction_and_optimizer_finding():
    covered = {c["finding"] for c in DATA["cases"]}
    # 18-21 were over-refusals (equivalent pairs): coverage backlog, not
    # counterexamples.
    assert covered == set(range(1, 36)) - {18, 19, 20, 21}
