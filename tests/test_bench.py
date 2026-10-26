"""Open equivalence benchmark: the numbers the README quotes, reproducible."""

import yaml

from semantix.bench import DEFAULT_CORPUS, run_bench
from semantix.cli import main


# Near-miss cases the completeness gate answers with "unproven" rather than a
# proven distinction, because the core does not model the construct. This set
# is EMPTY: every construct the 2026-07-25 review found is now genuinely
# modeled. A name may only be added here alongside a deliberate, documented
# decision not to model something — never to make a regression pass.
KNOWN_UNMODELED: set[str] = set()

# Near-misses OUTSIDE the proof grammar (semantix-proof/2): refused by policy
# with a named reason, never certified — the measured cost of the narrower
# scope. Supporting one of these constructs moves it to "distinguished"; a
# case may only be added with the grammar change that refuses it.
GRAMMAR_REFUSED = {
    "cross-database-same-short-table": "lists only raw.orders",
    "cross-join-no-on-clause": "uses a join",
    "cte-body-invisible": "uses GROUP BY",
    "grouping-rollup-vs-ungrouped-total": "uses ROLLUP",
    "grouping-sets-vs-flat": "uses GROUPING SETS",
    "join-type-inner-vs-left": "uses a join",
    "limit-is-not-neutral": "uses LIMIT",
    "rollup-ordinal-order": "uses ROLLUP",
    "select-distinct-row-dedup": "uses DISTINCT",
}


def test_shipped_corpus_holds_the_gates():
    result = run_bench()
    det = result["equivalence_detection"]
    assert det["rate"] == 1.0
    assert det["missed"] == []
    assert result["false_certifications"] == []  # the survival condition
    assert result["known_gaps"]["confirmed"] == [
        "distinct-over-a-projected-key-is-neutral", "in-list-order",
    ]
    assert result["known_gaps"]["closed"] == []


def test_unproven_near_misses_are_only_the_documented_gaps():
    """The gate may refuse to compare, but only for constructs we have
    admitted we do not model. Anything else appearing here means a blind spot
    opened silently; anything LEAVING here must be modeled, not waved through."""
    result = run_bench()
    unproven = {name.split("@")[0] for name in result["near_misses"]["unproven"]}
    assert unproven <= KNOWN_UNMODELED, f"undocumented blind spot: {unproven - KNOWN_UNMODELED}"


def test_grammar_refusals_are_exactly_the_documented_scope():
    result = run_bench()
    assert result["proof_policy"] == "semantix-proof/2"
    assert set(result["refused"]) == set(GRAMMAR_REFUSED)
    for name, why in result["refused"].items():
        assert why.startswith(("outside the supported grammar (semantix-proof/2): ",
                               "relation identity not established (semantix-proof/2): "))
        assert GRAMMAR_REFUSED[name] in why, (name, why)
    # Refusals never count as detection or distinction.
    assert result["equivalence_detection"]["refused"] == []
    nm = result["near_misses"]
    assert nm["distinguished"] + len(nm["refused"]) + len(nm["unproven"]) == nm["of"]


def test_matrix_covers_the_cross_platform_claim():
    """The gates must hold per dialect, not just in aggregate — 'system-
    agnostic' is certified, not declared."""
    result = run_bench()
    assert result["dialects"] == ["duckdb", "databricks", "snowflake", "bigquery"]
    for dialect, r in result["by_dialect"].items():
        assert r["false_certifications"] == [], dialect
        assert r["equivalence_detection"]["rate"] == 1.0, dialect
        # Every near-miss is either proven different or explicitly unproven —
        # never silently counted as a success.
        nm = r["near_misses"]
        assert nm["distinguished"] + len(nm["unproven"]) + len(nm["refused"]) == nm["of"], \
            dialect


def test_dialect_restriction():
    result = run_bench(dialects=["duckdb"])
    assert result["dialects"] == ["duckdb"]
    assert list(result["by_dialect"]) == ["duckdb"]
    from semantix.bench import load_corpus
    expected = sum(1 for c in load_corpus()["cases"]
                   if c.get("expect") == "equivalent" and not c.get("known_gap"))
    assert result["equivalence_detection"]["of"] == expected


def test_broken_dialect_reports_unproven_not_distinguished():
    """A dialect whose parsing wholesale fails proves nothing — it must never
    be reported as successful near-miss distinction."""
    result = run_bench(dialects=["duckddb"])  # typo'd on purpose (API path)
    r = result["by_dialect"]["duckddb"]
    assert r["proof_grade_coverage_pct"] == 0.0
    assert r["near_misses"]["distinguished"] == 0
    assert len(r["near_misses"]["unproven"]) == r["near_misses"]["of"] > 0
    assert r["false_certifications"] == []  # degradation is never a false cert


def test_cli_rejects_unknown_and_empty_dialects(capsys):
    assert main(["bench", "--dialects", "duckddb"]) == 2
    assert "unknown dialect(s): duckddb" in capsys.readouterr().err
    assert main(["bench", "--dialects", " , "]) == 2
    assert "names no dialects" in capsys.readouterr().err


def test_cli_dedupes_and_strips_dialects(capsys):
    assert main(["bench", "--dialects", "duckdb, duckdb"]) == 0
    stdout = capsys.readouterr().out
    assert "dialects: duckdb ·" in stdout  # one entry, not two
    assert "18/18" not in stdout           # not double-counted


def test_false_certification_fails_the_cli_gate(tmp_path, capsys):
    corpus = yaml.safe_load(DEFAULT_CORPUS.read_text())
    # Poison the corpus: an expect-different pair that is actually identical.
    corpus["cases"].append({
        "name": "poisoned-pair", "category": "near-miss", "expect": "different",
        "a": "select sum(amount) as m from raw.orders",
        "b": "select sum(amount) as m from raw.orders",
    })
    poisoned = tmp_path / "poisoned.yaml"
    poisoned.write_text(yaml.safe_dump(corpus))

    assert main(["bench", "--corpus", str(poisoned)]) == 1
    err = capsys.readouterr().err
    assert "FALSE EQUALITIES: poisoned-pair" in err


def test_cli_bench_reports_and_writes_json(tmp_path, capsys):
    out = tmp_path / "bench.json"
    assert main(["bench", "--json", str(out)]) == 0
    stdout = capsys.readouterr().out
    assert "false equalities: 0" in stdout
    assert "known gaps: 2 confirmed" in stdout
    assert "dialects: duckdb, databricks, snowflake, bigquery" in stdout
    assert out.exists()


def test_cli_bench_dialects_flag(capsys):
    assert main(["bench", "--dialects", "duckdb,snowflake"]) == 0
    stdout = capsys.readouterr().out
    assert "dialects: duckdb, snowflake" in stdout
    assert "bigquery" not in stdout
