"""The rename from semantix: what was written under the former name keeps
meaning what it meant.

A store under its former default name is never replaced by a fresh one (a
new signing key), a scan or receipt recorded under the former policy id is
under the policy in force, annotations already written in an estate's SQL
still declare what they declared, and rows published under the former name
are brought under the policy instead of being left behind.
"""

from __future__ import annotations

import json

import pytest
import sqlglot

from touchstone.extraction.proof import POLICY_ID, policy_in_force
from touchstone.store import Store


def test_a_store_under_the_former_name_is_never_replaced_by_a_new_one(tmp_path):
    from touchstone.cli import main

    former = tmp_path / "semantix.db"
    Store(former).close()
    with pytest.raises(ValueError, match="semantix.db exists"):
        Store(tmp_path / "touchstone.db")
    assert not (tmp_path / "touchstone.db").exists()
    Store(former).close()                         # named explicitly, it opens
    Store(tmp_path / "other.db").close()          # only the default name is guarded
    assert main(["experimental", "concepts", "--db", str(tmp_path / "touchstone.db")]) == 2
    assert main(["experimental", "pubkey", "--db", str(tmp_path / "touchstone.db")]) == 2
    assert not (tmp_path / "touchstone.db").exists()


def test_every_command_that_reads_a_store_refuses_beside_the_former_one(tmp_path, capsys):
    pytest.importorskip("cryptography")
    from touchstone.cli import main
    from touchstone.engine import run_scan

    estate = tmp_path / "estate"
    estate.mkdir()
    (estate / "rev.sql").write_text("-- touchstone: relation=db.main.rev\n"
                                    "SELECT SUM(amount) AS revenue FROM db.main.orders\n")
    _, other = run_scan(sql_dir=str(estate), db_path=str(tmp_path / "other.db"))
    try:
        label = other.concepts_for_scan(other.latest_scan()["scan_id"])[0]["label"]
        (tmp_path / "receipt.json").write_text(json.dumps(other.issue_receipt(label).as_dict()))
    finally:
        other.close()
    here = tmp_path / "here"
    here.mkdir()
    Store(here / "semantix.db").close()
    default = str(here / "touchstone.db")
    capsys.readouterr()
    # Skipping the store dropped the issued-by-this-store check without a word.
    assert main(["experimental", "verify-receipt", str(tmp_path / "receipt.json"), "--db", default]) == 2
    assert "semantix.db exists" in capsys.readouterr().err
    wh = _former_warehouse(tmp_path / "wh.duckdb")
    assert wh is not None
    assert main(["experimental", "publish", "--refresh-policy", "--warehouse", str(tmp_path / "wh.duckdb"),
                 "--db", default]) == 2
    assert "semantix.db exists" in capsys.readouterr().err
    assert not (here / "touchstone.db").exists()


def test_the_refusal_names_the_wal_files_a_rename_must_carry(tmp_path):
    from touchstone.store import former_store

    Store(tmp_path / "semantix.db").close()
    (tmp_path / "semantix.db-wal").write_bytes(b"")
    message = former_store(tmp_path / "touchstone.db")
    assert "nothing holding it open" in message and "semantix.db-wal" in message


def test_the_former_policy_id_is_the_policy_in_force():
    assert POLICY_ID == "touchstone-proof/2"
    assert policy_in_force("semantix-proof/2") and policy_in_force(POLICY_ID)
    assert not policy_in_force(None) and not policy_in_force("semantix-proof/1")
    # A receipt is arbitrary JSON; verification never raises on it.
    assert not policy_in_force({"id": "x"}) and not policy_in_force(["semantix-proof/2"])
    from touchstone.signing import _assess_live

    _assess_live({"scan_id": "s", "proof_policy": {"id": "x"}}, "current", None,
                 {"scan_id": "s", "status": "fresh"})


def test_fingerprint_ids_are_the_ones_the_former_name_recorded(tmp_path):
    """A gap's text carries the policy id; the id it hashes is spelled as
    semantix 83a57db recorded it, so a rescan restates nothing."""
    from touchstone.engine import run_scan
    from touchstone.extraction.proof import is_grammar_gap, is_policy_gap

    estate = tmp_path / "estate" / "analytics"
    estate.mkdir(parents=True)
    (estate / "rev_join.sql").write_text(
        "SELECT SUM(o.amount) AS net_revenue FROM raw.orders o JOIN raw.customers c "
        "ON o.customer_id = c.customer_id WHERE o.status = 'completed'\n")
    (tmp_path / "catalog.yml").write_text(
        "raw:\n  orders:\n    amount: DOUBLE\n    customer_id: INTEGER\n"
        "    status: VARCHAR\n  customers:\n    customer_id: INTEGER\n")
    _, store = run_scan(sql_dir=str(tmp_path / "estate"),
                        catalog_path=str(tmp_path / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"))
    try:
        ids = [r[0] for r in store._conn.execute("SELECT fingerprint_id FROM fingerprints")]
    finally:
        store.close()
    # Recorded by semantix 83a57db for exactly this estate and catalog.
    assert ids == ["018e16357f89324fccca318661e734abecef527ee41345bdd5332f4f1b8ba54a"]
    former = "outside the supported grammar (semantix-proof/2): uses a join"
    assert is_grammar_gap(former) and is_policy_gap(former)
    assert is_policy_gap("relation identity not established (semantix-proof/2): x")


def test_a_scan_recorded_under_the_former_policy_id_is_not_stale_on_policy(tmp_path):
    from touchstone.engine import run_scan
    from touchstone.services.freshness import check_freshness

    estate = tmp_path / "estate"
    estate.mkdir()
    (estate / "rev.sql").write_text("-- touchstone: relation=db.main.rev\n"
                                    "SELECT SUM(amount) AS revenue FROM db.main.orders\n")
    _, store = run_scan(sql_dir=str(estate), db_path=str(tmp_path / "s.db"))
    try:
        store._conn.execute("UPDATE scans SET stats_json = json_set(stats_json, "
                            "'$.proof_policy', 'semantix-proof/2')")
        store._conn.commit()
        live = check_freshness(store, sql_dir=str(estate))
        assert "proof_policy" not in live and live["status"] == "fresh"
        for concept in store.concepts_for_scan(store.latest_scan()["scan_id"]):
            receipt = store.issue_receipt(concept["label"])
            assert not any("proof policy" in d for d in receipt.known_divergences)
    finally:
        store.close()


@pytest.mark.parametrize("prefix", ["touchstone", "semantix"])
def test_relation_directives_under_either_name_bind(tmp_path, prefix):
    from touchstone.adapters.sql_dir import load_sql_dir

    (tmp_path / "rev.sql").write_text(f"-- {prefix}: relation=db.main.rev_mart\n"
                                      "SELECT SUM(amount) AS revenue FROM db.main.orders\n")
    (tmp_path / "bad.sql").write_text(f"-- {prefix}: relation=\nSELECT 1 AS one\n")
    artifacts, skipped = load_sql_dir(tmp_path)
    assert [a.relation for a in artifacts] == ["db.main.rev_mart"]
    assert [s["path"].rsplit("/", 1)[1] for s in skipped] == ["bad.sql"]
    assert f"-- {prefix}: relation=" in skipped[0]["reason"]   # names what is there


def test_only_a_line_that_tries_to_declare_a_relation_is_a_directive(tmp_path):
    """Prose naming the product beside a column called `relation` is SQL to
    read; a malformed directive is reported by its own line."""
    from touchstone.adapters.sql_dir import load_sql_dir

    (tmp_path / "prose.sql").write_text(
        "-- Touchstone: partner revenue as reported to the board\n"
        "SELECT SUM(amount) AS partner_revenue FROM db.main.orders WHERE relation='partner'\n")
    (tmp_path / "below.sql").write_text(
        "SELECT SUM(amount) AS revenue FROM db.main.orders\nWHERE kind_relation='x'\n"
        "-- touchstone: relation=db.main.rev_mart\n")
    (tmp_path / "bad.sql").write_text(
        "SELECT SUM(amount) AS revenue FROM db.main.orders\nWHERE kind_relation='x'\n"
        "-- touchstone: relation=\n")
    (tmp_path / "block.sql").write_text("/* semantix: relation=db.main.x */\nSELECT 1 AS one\n")
    artifacts, skipped = load_sql_dir(tmp_path)
    assert sorted(a.relation for a in artifacts) == ["db.main.rev_mart", "public.prose"]
    reasons = {s["path"].rsplit("/", 1)[1]: s["reason"] for s in skipped}
    assert set(reasons) == {"bad.sql", "block.sql"}
    assert "'-- touchstone: relation='" in reasons["bad.sql"]
    assert "semantix: relation=db.main.x" in reasons["block.sql"]


def test_a_store_renamed_without_its_wal_is_refused(tmp_path):
    from touchstone.store import former_store

    Store(tmp_path / "touchstone.db").close()
    (tmp_path / "semantix.db-wal").write_bytes(b"frames")
    with pytest.raises(ValueError, match="never reached the main file"):
        Store(tmp_path / "touchstone.db")
    (tmp_path / "semantix.db-wal").write_bytes(b"")      # an empty WAL holds nothing
    assert former_store(tmp_path / "touchstone.db") is None


@pytest.mark.parametrize("prefix", ["touchstone", "semantix"])
def test_metric_tags_under_either_name_name_the_metric(prefix):
    from touchstone.services.monitor import _statement_tag

    stmt = sqlglot.parse_one(f"SELECT SUM(amount) AS x FROM t -- {prefix}: metric=net_revenue")
    assert _statement_tag(stmt) == ("tag", "net_revenue")


def _former_warehouse(path, verdicts="table"):
    """A warehouse as semantix 83a57db published it."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA semantix")
    if verdicts == "table":
        con.execute("CREATE TABLE semantix.verdicts (scan_id VARCHAR, scanned_at TIMESTAMPTZ, "
                    "published_at TIMESTAMPTZ, concept VARCHAR, verdict VARCHAR, "
                    "severity INTEGER, summary VARCHAR, relation VARCHAR, "
                    "policy_status VARCHAR, usable_for_reliance BOOLEAN, "
                    "published_verdict VARCHAR)")
        con.execute("INSERT INTO semantix.verdicts (scan_id, concept, verdict, severity, "
                    "summary) VALUES ('s', 'gmv', 'CERTIFIED', 5, 'identical')")
    else:   # an operator's bridge from the former name to the new projection
        con.execute("CREATE VIEW semantix.verdicts AS SELECT 1 AS x")
    con.execute("CREATE SCHEMA agents")
    con.execute("CREATE TABLE agents.root (provider VARCHAR, key VARCHAR, content VARCHAR)")
    con.execute("INSERT INTO agents.root VALUES ('semantix', 'receipt/gmv', '{\"a\": 1}'), "
                "('semantix', 'skill/verify-metric', 'old skill'), "
                "('someone_else', 'receipt/gmv', 'theirs')")
    con.close()
    return duckdb


def test_refresh_brings_what_was_published_under_the_former_name_under_the_policy(tmp_path):
    from touchstone.services.writeback import refresh_published_policy

    warehouse = tmp_path / "old.duckdb"
    duckdb = _former_warehouse(warehouse)
    out = refresh_published_policy(str(warehouse))
    assert out["verdict_tables"] == ["semantix.verdicts"] and out["verdict_rows"] == 1
    assert out["former_provider_rows"] == 2
    con = duckdb.connect(str(warehouse))
    verdict, usable = con.execute(
        "SELECT verdict, usable_for_reliance FROM semantix.verdicts").fetchone()
    rows = con.execute("SELECT provider, key, content FROM agents.root").fetchall()
    con.close()
    assert verdict != "CERTIFIED" and usable is False
    assert ("touchstone", "receipt/gmv", '{"a": 1}') in rows      # content untouched
    assert ("someone_else", "receipt/gmv", "theirs") in rows
    assert not [r for r in rows if r[0] == "semantix"]
    assert ("touchstone", "skill/verify-metric") in {(p, k) for p, k, _ in rows}
    assert "old skill" not in {c for _, _, c in rows}


def _published(tmp_path, verdicts="table"):
    from touchstone.engine import run_scan
    from touchstone.services.writeback import publish_receipts

    estate = tmp_path / "estate"
    estate.mkdir()
    (estate / "rev.sql").write_text("-- touchstone: relation=db.main.rev\n"
                                    "SELECT SUM(amount) AS revenue FROM db.main.orders\n")
    _, store = run_scan(sql_dir=str(estate), db_path=str(tmp_path / "s.db"))
    warehouse = tmp_path / "wh.duckdb"
    duckdb = _former_warehouse(warehouse, verdicts)
    try:
        summary = publish_receipts(store, str(warehouse))
    finally:
        store.close()
    return summary, duckdb.connect(str(warehouse))


def test_publish_replaces_former_rows_and_keeps_the_former_table_in_step(tmp_path):
    """A gate still reading semantix.verdicts keeps failing on NEW findings: it
    gets the same projection, not a frozen copy of the last old publish."""
    summary, con = _published(tmp_path)
    cols = "scan_id, concept, verdict, severity, relation, usable_for_reliance"
    try:
        former = con.execute(f"SELECT {cols} FROM semantix.verdicts ORDER BY ALL").fetchall()
        current = con.execute(f"SELECT {cols} FROM touchstone.verdicts ORDER BY ALL").fetchall()
        providers = {r[0] for r in con.execute("SELECT provider FROM agents.root").fetchall()}
        policy = json.loads(con.execute(
            "SELECT content FROM agents.root WHERE provider = 'touchstone' "
            "AND key = 'policy/conclusions'").fetchone()[0])
    finally:
        con.close()
    assert former == current and summary["former_verdict_rows"] == len(current) > 0
    assert all(row[2] != "CERTIFIED" and row[-1] is False for row in former)
    assert providers == {"touchstone", "someone_else"}
    assert policy["usable_for_reliance"] is False


def test_a_view_under_the_former_name_is_the_operators_not_ours(tmp_path):
    from touchstone.services.writeback import refresh_published_policy

    summary, con = _published(tmp_path, verdicts="view")
    try:
        assert con.execute("SELECT * FROM semantix.verdicts").fetchall() == [(1,)]
    finally:
        con.close()
    assert summary["former_verdict_rows"] is None
    out = refresh_published_policy(str(tmp_path / "wh.duckdb"))
    assert out["verdict_tables"] == ["touchstone.verdicts"]
