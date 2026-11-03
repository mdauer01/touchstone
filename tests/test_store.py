"""Bitemporal store: append-only writes, derived valid time, as-of queries,
restatement detection, receipts."""

import pytest

import touchstone.store as store_mod
from touchstone.models import Fingerprint, Provenance, SemanticCore
from touchstone.store import Store, normalize_ts


@pytest.fixture
def clock(monkeypatch):
    """Deterministic, strictly increasing scan timestamps."""
    times = iter(
        [f"2026-0{m}-01T00:00:00Z" for m in range(1, 10)]
    )
    current = {"t": "2026-01-01T00:00:00Z"}

    def fake_utcnow():
        return current["t"]

    def advance():
        current["t"] = next(times)
        return current["t"]

    monkeypatch.setattr(store_mod, "utcnow", fake_utcnow)
    return advance


def make_fp(expr: str, name: str = "net_revenue", site: str = "model.acme.growth") -> Fingerprint:
    core = SemanticCore(measure_expr=expr, join_nodes=("analytics.x",))
    return Fingerprint(
        fingerprint_id=Fingerprint.make_id(core, name),
        concept_candidate=name,
        core=core,
        relation="analytics.growth_net_revenue",
        dialect="duckdb",
        canon_level="supported",
        canonical_sql="SELECT ...",
        provenance=Provenance(system="dbt", object_id=site),
    )


def test_history_derives_valid_time_from_observations(tmp_path, clock):
    store = Store(tmp_path / "t.db")

    t1 = clock()
    scan1, _ = store.begin_scan("s1", "duckdb")
    fp_v1 = make_fp('SUM(t0."amount")')
    store.record_fingerprint(fp_v1, scan1, t1)

    t2 = clock()
    scan2, _ = store.begin_scan("s2", "duckdb")
    fp_v2 = make_fp('SUM(t0."amount" - t0."refund_amount")')
    store.record_fingerprint(fp_v2, scan2, t2)

    history = store.history("net_revenue")
    assert len(history) == 2
    first, second = sorted(history, key=lambda iv: iv.valid_from)
    assert first.valid_from == normalize_ts(t1) and first.valid_to == normalize_ts(t2)
    assert second.valid_from == normalize_ts(t2) and second.valid_to is None  # live

    # as-of returns the definition that was live at the time
    assert store.as_of("net_revenue", t1)[0].ref_id == fp_v1.fingerprint_id
    assert store.as_of("net_revenue", t2)[0].ref_id == fp_v2.fingerprint_id

    # a comparison spanning the boundary is a restatement
    assert store.restatement_check("net_revenue", t1, t2)
    assert not store.restatement_check("net_revenue", "2020-01-01T00:00:00Z", t1)


def test_reobservation_of_same_definition_extends_no_interval(tmp_path, clock):
    store = Store(tmp_path / "t.db")
    fp = make_fp('SUM(t0."amount")')
    for _ in range(3):
        t = clock()
        scan, _ = store.begin_scan(None, "duckdb")
        store.record_fingerprint(fp, scan, t)
    history = store.history("net_revenue")
    assert len(history) == 1
    assert history[0].valid_to is None


def test_latest_scan_tie_breaks_by_insertion_order(tmp_path, clock):
    store = Store(tmp_path / "t.db")
    clock()
    scan1, _ = store.begin_scan("first", "duckdb")
    scan2, _ = store.begin_scan("second", "duckdb")  # same second
    store.finish_scan(scan1, {})
    store.finish_scan(scan2, {})
    assert store.latest_scan()["scan_id"] == scan2


def test_an_interrupted_scan_is_not_the_latest_scan(tmp_path, clock):
    """A scan that died partway wrote no stats and describes nothing. It was
    still the newest row, so every "current health" read — concepts, attest,
    publish, the MCP tools — answered from it and reported a CLEAN ESTATE
    while the store held a recorded CONTRADICTION."""
    store = Store(tmp_path / "t.db")
    clock()
    done, _ = store.begin_scan("complete", "duckdb")
    store.finish_scan(done, {})
    store.begin_scan("interrupted", "duckdb")  # process dies here
    assert store.latest_scan()["scan_id"] == done


def test_latest_scan_is_none_before_any_scan_completes(tmp_path, clock):
    store = Store(tmp_path / "t.db")
    clock()
    store.begin_scan("interrupted", "duckdb")
    assert store.latest_scan() is None


def test_fingerprint_rows_are_immutable_content_addressed(tmp_path, clock):
    store = Store(tmp_path / "t.db")
    t = clock()
    scan, _ = store.begin_scan(None, "duckdb")
    fp = make_fp('SUM(t0."amount")')
    store.record_fingerprint(fp, scan, t)
    store.record_fingerprint(fp, scan, t)  # INSERT OR IGNORE — no dupes, no updates
    rows = store._conn.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0]
    assert rows == 1
