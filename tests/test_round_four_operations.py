"""Round four: what happens when the operator, the estate, or the clock is
imperfect.

2026-07-28. Correctness that only holds on a clean estate is not correctness.
These cover the failure paths an install actually meets: a wrong path, a
malformed file, two CI jobs at once, two scans in the same second, a wall of
verdicts nobody can read, and a three-part warehouse — the one that made the
whole engine inert on Snowflake, Databricks and BigQuery.
"""


import pytest

from semantix.cli import main
from semantix.extraction.canonicalize import canonicalize, canonicalize_sql
from semantix.extraction.registry import SchemaRegistry
from semantix.models import CanonLevel
from semantix.store import Store, normalize_ts, utcnow

COLS = {"order_id": "bigint", "region": "varchar", "amount": "decimal(18,2)"}


# ------------------------------------------------------------ schema binding

@pytest.mark.parametrize("dialect", ["duckdb", "snowflake", "databricks", "bigquery"])
def test_three_part_relations_bind(dialect):
    """`database.schema.table` is THREE levels, not a two-level dict with a
    dotted key. Splitting on the last dot produced a mapping sqlglot can never
    match, so on every warehouse where relations are three-part — which is all
    of them except the demo — proof-grade coverage was ~0% and nothing could
    ever be certified."""
    registry = SchemaRegistry({"raw_prod.landing.orders": COLS})
    canon = canonicalize_sql(
        "select region, sum(amount) as revenue from raw_prod.landing.orders group by 1",
        registry.sqlglot_schema(dialect), dialect)
    assert canon.level == CanonLevel.OPTIMIZED, canon.error


def test_a_mixed_arity_estate_binds_both_ways():
    """sqlglot refuses a mixed-depth mapping outright, and a real estate always
    mixes: dbt emits `database.schema.model` while a hand-written catalog is
    usually `schema.table`. Whichever arity appeared first won and everything
    at the other arity silently failed to resolve."""
    registry = SchemaRegistry({"raw.orders": COLS, "prod.landing.events": COLS})
    for sql in ("select region, sum(amount) as m from raw.orders group by 1",
                "select region, sum(amount) as m from prod.landing.events group by 1",
                "select region, sum(amount) as m from landing.events group by 1"):
        canon = canonicalize_sql(sql, registry.sqlglot_schema("snowflake"), "snowflake")
        assert canon.level == CanonLevel.OPTIMIZED, f"{sql}: {canon.error}"
    # The proof path binds the names the catalog states and refuses the
    # short one, whose table depends on the session's defaults.
    assert canonicalize("select region, sum(amount) as m from prod.landing.events "
                        "group by 1", registry, "snowflake").level == CanonLevel.SUPPORTED
    short = canonicalize("select region, sum(amount) as m from landing.events group by 1",
                         registry, "snowflake")
    assert short.level != CanonLevel.SUPPORTED
    assert any("depends on the session" in g for g in short.gaps), short.gaps


def test_registering_models_does_not_break_binding():
    """The cached schema is extended in place rather than rebuilt per model —
    rebuilding is O(models x relations x columns) and was the whole
    superlinearity. Extending must not corrupt what is already bound."""
    registry = SchemaRegistry({"prod.raw.orders": COLS})
    for i in range(5):
        canon = canonicalize_sql(
            f"select region, sum(amount) as m_{i} from prod.raw.orders group by 1",
            registry.sqlglot_schema("snowflake"), "snowflake")
        assert canon.level == CanonLevel.OPTIMIZED, canon.error
        registry.register_model(f"prod.analytics.m_{i}", canon.ast)
    canon = canonicalize_sql(
        "select region, sum(m_0) as total from prod.analytics.m_0 group by 1",
        registry.sqlglot_schema("snowflake"), "snowflake")
    assert canon.level == CanonLevel.OPTIMIZED, canon.error


# ------------------------------------------------------------------- the gate

def _estate(tmp_path, files: dict[str, str]):
    root = tmp_path / "sql" / "analytics"
    root.mkdir(parents=True)
    for name, sql in files.items():
        (root / name).write_text(sql)
    (tmp_path / "catalog.yml").write_text(
        "raw:\n  orders:\n    order_id: bigint\n    status: varchar\n"
        "    amount: 'decimal(18,2)'\n")
    return tmp_path / "sql", tmp_path / "catalog.yml"


def test_the_gate_fails_on_an_empty_estate(tmp_path, capsys):
    """A gate that passes on an unread estate is not a gate: a wrong --dbt path
    or a forgotten `dbt compile` turned CI green because nothing was verified
    and so nothing objected."""
    sql_dir, catalog = _estate(tmp_path, {})
    code = main(["scan", "--sql-dir", str(sql_dir), "--catalog", str(catalog),
                 "--db", str(tmp_path / "a.db"), "--fail-on", "any"])
    assert code == 1
    assert "no computed SQL" in capsys.readouterr().err


def test_the_gate_fails_when_nothing_yielded_a_measure(tmp_path, capsys):
    sql_dir, catalog = _estate(tmp_path, {"x.sql": "this is not sql at all ((("})
    code = main(["scan", "--sql-dir", str(sql_dir), "--catalog", str(catalog),
                 "--db", str(tmp_path / "b.db"), "--fail-on", "any"])
    assert code == 1
    err = capsys.readouterr().err
    assert "NONE yielded a measure" in err or "could not be read" in err


def test_parse_failures_are_named(tmp_path, capsys):
    """`unparseable` was computed and never exposed, so an operator saw only
    "yielded no measure" — which reads as "this model defines no metric"."""
    sql_dir, catalog = _estate(tmp_path, {"x.sql": "this is not sql at all ((("})
    main(["scan", "--sql-dir", str(sql_dir), "--catalog", str(catalog),
          "--db", str(tmp_path / "c.db")])
    assert "COULD NOT BE PARSED" in capsys.readouterr().out


def test_min_coverage_fails_a_scan_that_understood_little(tmp_path, capsys):
    sql_dir, catalog = _estate(tmp_path, {
        "a.sql": "select sum(amount) as revenue from nowhere.unknown_table"})
    code = main(["scan", "--sql-dir", str(sql_dir), "--catalog", str(catalog),
                 "--db", str(tmp_path / "d.db"), "--min-coverage", "90"])
    assert code == 1
    assert "below the required" in capsys.readouterr().err


# ------------------------------------------------------------------- triage

def test_triage_flags_bound_the_output_without_hiding_the_evidence(
        acme, tmp_path, capsys):
    """A first scan of a real estate emits one verdict per undeclared measure —
    764 from 800 models in one measured run. Filtering the terminal must never
    mean dropping them from the store, the report or the JSON."""
    main(["scan", "--dbt", str(acme / "dbt_project"),
          "--metrics", str(acme / "metrics.yml"),
          "--catalog", str(acme / "catalog.yml"),
          "--db", str(tmp_path / "t.db"), "--min-severity", "40",
          "--json", str(tmp_path / "out.json")])
    out = capsys.readouterr().out
    assert "SHADOW_METRIC" not in out.split("severity")[0] or "…" in out
    assert "are in the store, the report and --json" in out
    assert "SHADOW_METRIC" in (tmp_path / "out.json").read_text()


# ----------------------------------------------------------------- operator

def test_a_missing_manifest_is_an_error_not_a_traceback(tmp_path, capsys):
    code = main(["scan", "--dbt", str(tmp_path / "nope"),
                 "--db", str(tmp_path / "e.db")])
    assert code == 2
    assert "semantix:" in capsys.readouterr().err


def test_a_malformed_metrics_file_names_itself(acme, tmp_path, capsys):
    bad = tmp_path / "bad.yml"
    bad.write_text("metrics: [\n")
    code = main(["scan", "--dbt", str(acme / "dbt_project"), "--metrics", str(bad),
                 "--db", str(tmp_path / "f.db")])
    assert code == 2
    err = capsys.readouterr().err
    assert "bad.yml" in err and "not valid YAML" in err


def test_pubkey_does_not_mint_a_key_for_a_typo(tmp_path, capsys):
    """A typo'd --db silently CREATED a store and minted a fresh key, which the
    operator then published as the pin — after which every real receipt fails
    verification, and the failure looks like tampering rather than a typo."""
    missing = tmp_path / "typo.db"
    code = main(["pubkey", "--db", str(missing)])
    assert code == 2
    assert not missing.exists()
    assert "will not create one" in capsys.readouterr().err


# ------------------------------------------------------------------- clock

def test_timestamps_carry_sub_second_precision():
    """Whole seconds could not represent a change inside one second, so two
    scans in the same second produced a ZERO-LENGTH valid interval: a
    definition that genuinely existed was live at no instant."""
    assert normalize_ts("2026-03-01T00:00:00Z") == "2026-03-01T00:00:00.000Z"
    assert utcnow().endswith("Z") and len(utcnow()) == len("2026-03-01T00:00:00.000Z")
    # Text ordering — how valid time is actually compared — must still hold.
    assert normalize_ts("2026-03-01T00:00:00Z") < normalize_ts("2026-03-01T00:00:00.500Z")
    assert normalize_ts("2026-03-01T00:00:00.500Z") < normalize_ts("2026-03-01T00:00:01Z")


def test_a_second_connection_can_read_a_store(tmp_path):
    """WAL plus a busy timeout: a scan holds its write transaction for minutes,
    so two CI jobs meant one died with a bare "database is locked"."""
    db = tmp_path / "wal.db"
    first = Store(db)
    scan_id, _ = first.begin_scan("one", "duckdb")
    first.finish_scan(scan_id, {})
    second = Store(db)  # concurrent reader
    assert second.latest_scan()["scan_id"] == scan_id
