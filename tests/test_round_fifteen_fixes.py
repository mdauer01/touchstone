"""Round fifteen: review of ca0b74a.

1. DISTINCT ON keys kept an unresolved projection alias. ORDER BY terms were
   resolved in round fourteen, but `DISTINCT ON (k)` still recorded "k":
   over `SUM(refund) AS k` vs `SUM(refund) % 2 AS k` the two revenue
   fingerprints were optimized, gap-free and identical while one query
   returned 900 and 100 and the other only 900 (both groups share the key).
   False ALIASING in a scan, exact conforming in the monitor.
2. Equivalent-name matching could choose an EXPIRED definition. With
   net_revenue (current), an old total_revenue equal to it (ended Oct 1) and
   a new total_revenue = SUM(amount) (from Oct 1), an Oct 2
   `MEASURE(net_revenue) AS total_revenue` was "contested … conforms to the
   old view", and the raw-table form was unverifiable although the new
   definition covered the run. Time-disjoint versions also made the metric
   "contested". Every match candidate, and contestedness, is now drawn from
   the definitions that apply to the execution.

Under the conclusions policy (touchstone.conclusions) matches are reported as
experimental and violations as suspected differences; the tests pin the
engine's judgment through tests.policy and the reported kind beside it.
"""

from __future__ import annotations

import hashlib
import json

import pytest
import yaml

from touchstone.adapters.catalog import load_catalog
from touchstone.adapters.sql_dir import load_sql_dir
from touchstone.engine import run_scan
from touchstone.models import PROOF_LEVELS, VerdictType
from touchstone.services.history import read_history
from touchstone.services.monitor import build_registry, monitor_history
from touchstone.services.pilot import load_pilot

from .policy import engine_codes, engine_funnel, engine_kind, engine_kinds
from .test_monitor_conformance import CATALOG, GOVERNED, MODEL, VIEW, codes
from .test_round_fourteen_fixes import SEED, _estate, _fp
from .test_round_ten_fixes import headline_consistent

duckdb = pytest.importorskip("duckdb")

KEYED = ("SELECT DISTINCT ON (k) SUM(amount) AS revenue, SUM(refund) AS k "
         "FROM raw.orders GROUP BY country ORDER BY revenue DESC")
KEYED_MOD = KEYED.replace("SUM(refund) AS k", "SUM(refund) % 2 AS k")


# ------------------------------------------------- 1. DISTINCT ON keys

def test_distinct_on_alias_key_is_resolved_into_the_core():
    con = duckdb.connect()
    con.execute(SEED)
    assert sorted(r[0] for r in con.execute(KEYED).fetchall()) == [100.0, 900.0]
    assert [r[0] for r in con.execute(KEYED_MOD).fetchall()] == [900.0]
    a, b = _fp(KEYED), _fp(KEYED_MOD)
    # DISTINCT ON is outside touchstone-proof/2: refused by name, never proof
    # grade. The optimizer's reading (a hypothesis, for candidates and diffs)
    # still resolves the key, so the two do not even look alike.
    assert a.canon_level not in PROOF_LEVELS and b.canon_level not in PROOF_LEVELS
    assert a.gaps == ("outside the supported grammar (touchstone-proof/2): uses DISTINCT",)
    assert a.core.hash() != b.core.hash()
    assert any("DISTINCT ON (SUM(" in f for f in a.core.filters)


@pytest.mark.parametrize("spelling", [
    "DISTINCT ON (SUM(refund))",           # the expression itself
    "DISTINCT ON (2)",                     # an ordinal
])
def test_equivalent_distinct_on_spellings_still_hash_equal(spelling):
    assert _fp(KEYED.replace("DISTINCT ON (k)", spelling)).core.hash() == _fp(KEYED).core.hash()


def test_distinct_on_alias_inside_an_expression_withholds_proof_grade():
    fp = _fp(KEYED.replace("DISTINCT ON (k)", "DISTINCT ON (k + 1)"))
    assert fp.canon_level not in PROOF_LEVELS
    assert any("DISTINCT ON" in g and "alias inside an expression" in g for g in fp.gaps)


def test_scan_reports_no_aliasing_for_different_distinct_on_keys(tmp_path):
    models = _estate(tmp_path, {"a": KEYED,
                                "b": KEYED_MOD.replace("revenue", "revenue_b")})
    result, store = run_scan(sql_dir=str(models), catalog_path=str(tmp_path / "catalog.yml"),
                             db_path=str(tmp_path / "s.db"))
    store.close()
    assert not [v for v in result.verdicts
                if v.verdict_type in (VerdictType.ALIASING, VerdictType.CERTIFIED)]
    # …nor does the engine, behind the policy's experimental label.
    assert not engine_kinds(result.verdicts) & {VerdictType.ALIASING, VerdictType.CERTIFIED}


def test_monitor_does_not_call_a_different_distinct_on_key_conforming(tmp_path):
    models = _estate(tmp_path, {"m": KEYED})
    _, store = run_scan(sql_dir=str(models), catalog_path=str(tmp_path / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"))
    registry = build_registry(load_catalog(tmp_path / "catalog.yml"),
                              load_sql_dir(models, "duckdb")[0], "duckdb")
    result = monitor_history(store, KEYED_MOD + ";\n", registry, "duckdb", min_runs=1,
                             authority_systems={"sql"})
    store.close()
    # The DISTINCT ON model is outside the proof grammar, so it is no
    # authority at all; nothing can conform to it.
    assert result["findings"]
    assert all(engine_kind(f) != "conforming_use" and f["kind"] != "conforming_use"
               for f in result["findings"])
    assert engine_funnel(result["funnel"])["conforming"] == 0
    headline_consistent(result)


# ------------------------------------- 2. definitions applicable at run time

NET = VIEW.format(extra="")
TOTAL_OLD = NET.replace("net_revenue", "total_revenue")
TOTAL_NEW = TOTAL_OLD.replace("SUM(amount) - SUM(refund_amount)", "SUM(amount)")
OCT2 = "2026-10-02T10:00:00Z"


def three_view_pilot(tmp_path, sql, start_time=OCT2, total_old_to="2026-10-01T00:00:00Z",
                     total_new_from="2026-10-01T00:00:00Z"):
    root = tmp_path / "pilot"
    views = root / "views"
    views.mkdir(parents=True)
    texts = {"revenue_mv.yaml": NET, "total_old_mv.yaml": TOTAL_OLD,
             "total_new_mv.yaml": TOTAL_NEW}
    for name, text in texts.items():
        (views / name).write_text(text, encoding="utf-8")
    (root / "catalog.yml").write_text(CATALOG, encoding="utf-8")
    (root / "history.jsonl").write_text(json.dumps(
        {"statement_id": "x", "statement_text": sql, "start_time": start_time}), encoding="utf-8")

    def entry(name, file, start, end):
        return {"name": name, "snapshot": f"views/{file}",
                "sha256": hashlib.sha256((views / file).read_bytes()).hexdigest(),
                "applies": {"from": start, "to": end}}
    (root / "pilot.yml").write_text(yaml.safe_dump({
        "history": "history.jsonl", "compare_against": "as_of_execution",
        "catalog": "catalog.yml",
        "authority": {"system": "uc_metrics", "metric_views": [
            entry("main.finance.revenue_mv", "revenue_mv.yaml", "2026-09-01T00:00:00Z", None),
            entry("main.finance.total_old_mv", "total_old_mv.yaml", "2026-09-01T00:00:00Z",
                  total_old_to),
            entry("main.finance.total_new_mv", "total_new_mv.yaml", total_new_from, None)]}}),
        encoding="utf-8")
    models = tmp_path / "models"
    models.mkdir()
    (models / "m.sql").write_text(MODEL, encoding="utf-8")
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(views),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "s.db"), dialect="databricks")
    try:
        pilot = load_pilot(root, "databricks")
        registry = build_registry(load_catalog(root / "catalog.yml"),
                                  load_sql_dir(models, "databricks")[0], "databricks")
        return monitor_history(store, read_history(pilot.history), registry, "databricks",
                               min_runs=1, pilot=pilot)
    finally:
        store.close()


VIEW_SQL = "SELECT MEASURE(net_revenue) AS total_revenue FROM main.finance.revenue_mv"
RAW_SQL = (f"SELECT {GOVERNED} AS total_revenue FROM main.finance.orders "
           f"WHERE status = 'completed'")


@pytest.mark.parametrize("sql", [VIEW_SQL, RAW_SQL])
def test_expired_equivalent_definition_is_not_matched(tmp_path, sql):
    """On Oct 2 total_revenue means SUM(amount): net revenue presented as
    total revenue is a violation, judged against the definition that applied."""
    result = three_view_pilot(tmp_path, sql)
    (f,) = result["findings"]
    assert (engine_kind(f), f["concept"]) == ("definition_violation", "total_revenue")
    assert f["kind"] == "suspected_difference"
    assert "total_old_mv" not in json.dumps(f)
    headline_consistent(result)


@pytest.mark.parametrize("sql", [VIEW_SQL, RAW_SQL])
def test_while_the_equivalent_definition_applies_the_names_agree(tmp_path, sql):
    """On Sep 15 the OLD total_revenue (equal to net_revenue) applies: the
    engine's rules find the same query conforming, as total_revenue, against
    the old view (reported as an experimental match under the policy)."""
    result = three_view_pilot(tmp_path, sql, start_time="2026-09-15T10:00:00Z")
    (f,) = result["findings"]
    assert (engine_kind(f), f["concept"]) == ("conforming_use", "total_revenue")
    assert f["kind"] == "unverifiable" and codes(f) == ["positive_suspended"]
    headline_consistent(result)


def test_time_disjoint_versions_are_not_contested(tmp_path):
    result = three_view_pilot(tmp_path, "SELECT MEASURE(net_revenue) AS net_revenue "
                                        "FROM main.finance.revenue_mv")
    assert "total_revenue" not in result["reference"]["contested_concepts"]
    assert engine_funnel(result["funnel"])["contested"] == 0
    # Overlapping windows with different definitions ARE a dispute.
    result = three_view_pilot(tmp_path / "overlap", VIEW_SQL,
                              total_old_to="2026-10-05T00:00:00Z")
    assert "total_revenue" in result["reference"]["contested_concepts"]
    # …and an execution inside the overlap sees two applicable definitions
    # that disagree: the equivalent one may not be presented as THE match.
    (f,) = result["findings"]
    assert engine_kind(f) == "contested_use" and f["concept"] == "total_revenue"
    assert f["kind"] == "unverifiable" and codes(f) == ["positive_suspended"]


def test_calculation_matching_only_an_expired_definition_is_context(tmp_path):
    """The raw query's calculation equals the OLD total_revenue only (net_revenue
    is a different measure name here); the match is reported as not evidenced,
    and the verdict comes from the applicable definition."""
    sql = (f"SELECT {GOVERNED} AS total_revenue FROM main.finance.orders "
           f"WHERE status = 'completed'")
    result = three_view_pilot(tmp_path, sql)
    (f,) = result["findings"]
    assert engine_kind(f) == "definition_violation"
    assert f["kind"] == "suspected_difference"
    assert engine_codes(f) and "total_new_mv" in f["summary"]
