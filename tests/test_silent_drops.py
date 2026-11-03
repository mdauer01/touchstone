"""Nothing may leave the scan unaccounted for.

A dropped artifact is a dropped definition, and a dropped definition is a
missing contradictor — so silent ingestion loss does not merely reduce
coverage, it manufactures CERTIFIED verdicts. Found by adversarial review,
2026-07-25.
"""

import json
import shutil

from touchstone.adapters.base import ComputedArtifact, duplicate_relations, topo_sort
from touchstone.adapters.dbt import load_dbt_detailed
from touchstone.engine import run_scan
from touchstone.models import VerdictType

from .policy import engine_kinds


def artifact(system, object_id, relation, sql="select 1", depends_on=()):
    return ComputedArtifact(system=system, object_id=object_id, relation=relation,
                            sql=sql, depends_on=list(depends_on))


def test_topo_sort_is_total_over_artifacts_not_relations():
    """Two artifacts targeting one relation must BOTH survive; keying the sort
    by relation silently deleted one of them."""
    arts = [
        artifact("dbt", "model.a", "analytics.revenue"),
        artifact("views", "analytics.revenue", "analytics.revenue"),
        artifact("dbt", "model.b", "analytics.other", depends_on=["analytics.revenue"]),
    ]
    ordered = topo_sort(arts)
    assert len(ordered) == 3
    assert {(a.system, a.object_id) for a in ordered} == {
        ("dbt", "model.a"), ("views", "analytics.revenue"), ("dbt", "model.b"),
    }
    # the dependent still sorts after BOTH producers of what it reads
    positions = {(a.system, a.object_id): i for i, a in enumerate(ordered)}
    assert positions[("dbt", "model.b")] > positions[("dbt", "model.a")]
    assert positions[("dbt", "model.b")] > positions[("views", "analytics.revenue")]


def test_topo_sort_survives_self_reference_and_cycles():
    arts = [
        artifact("dbt", "model.a", "analytics.a", depends_on=["analytics.a"]),
        artifact("dbt", "model.b", "analytics.b", depends_on=["analytics.c"]),
        artifact("dbt", "model.c", "analytics.c", depends_on=["analytics.b"]),
    ]
    assert len(topo_sort(arts)) == 3


def test_duplicate_relations_are_reported():
    arts = [
        artifact("dbt", "model.a", "analytics.revenue"),
        artifact("views", "v1", "analytics.revenue"),
        artifact("dbt", "model.c", "analytics.other"),
    ]
    assert duplicate_relations(arts) == {
        "analytics.revenue": ["dbt:model.a", "views:v1"]
    }


def test_two_definitions_of_one_relation_contradict_instead_of_certifying(tmp_path):
    """The end-to-end consequence: before the fix one of these was deleted, so
    the survivor matched the declaration and the scan reported CERTIFIED."""
    sql_dir = tmp_path / "sql"
    (sql_dir / "finance").mkdir(parents=True)
    (sql_dir / "growth").mkdir(parents=True)
    (sql_dir / "finance" / "revenue.sql").write_text(
        "-- touchstone: relation=analytics.revenue\n"
        "select sum(amount - refund_amount) as revenue from raw.orders "
        "where status = 'completed'\n"
    )
    (sql_dir / "growth" / "revenue.sql").write_text(
        "-- touchstone: relation=analytics.revenue\n"
        "select sum(amount) as revenue from raw.orders\n"
    )
    catalog = tmp_path / "catalog.yml"
    catalog.write_text(
        "raw:\n  orders:\n    order_id: bigint\n    status: varchar\n"
        "    amount: 'decimal(18,2)'\n    refund_amount: 'decimal(18,2)'\n"
    )

    result, _ = run_scan(sql_dir=str(sql_dir), catalog_path=str(catalog),
                         db_path=str(tmp_path / "s.db"))

    assert result.artifacts == 2
    assert len(result.fingerprints) == 2
    assert "analytics.revenue" in result.duplicate_relations
    # The engine contradicts (and never certifies); under the conclusions
    # policy that surfaces as a suspected divergence.
    assert VerdictType.CONTRADICTION in engine_kinds(result.verdicts)
    assert VerdictType.CERTIFIED not in engine_kinds(result.verdicts)
    assert any(v.verdict_type == VerdictType.SUSPECTED_DIVERGENCE for v in result.verdicts)


def test_dbt_nodes_without_compiled_sql_are_surfaced_not_dropped(tmp_path, acme):
    project = tmp_path / "proj"
    shutil.copytree(acme / "dbt_project", project)
    manifest = json.loads((project / "target" / "manifest.json").read_text())
    # A model whose Jinja was never rendered — the ordinary "forgot dbt compile"
    # case, and previously invisible.
    manifest["nodes"]["model.acme.uncompiled"] = {
        "resource_type": "model", "name": "uncompiled", "alias": "uncompiled",
        "schema": "analytics", "original_file_path": "models/uncompiled.sql",
        "raw_code": "select {{ var('x') }} as m from analytics.fct_orders",
        "depends_on": {"nodes": []},
    }
    (project / "target" / "manifest.json").write_text(json.dumps(manifest))

    artifacts, skipped = load_dbt_detailed(project)
    assert [s["object_id"] for s in skipped] == ["model.acme.uncompiled"]
    assert "dbt compile" in skipped[0]["reason"]
    assert skipped[0]["relation"] == "analytics.uncompiled"
    assert all(a.object_id != "model.acme.uncompiled" for a in artifacts)
    assert len(artifacts) == 7  # the readable estate is unaffected


def test_a_wholly_uncompiled_project_is_loudly_empty(tmp_path, acme):
    """The worst case: a manifest with no compiled output at all. Every model
    must be reported as skipped rather than the scan quietly finding nothing."""
    project = tmp_path / "bare"
    (project / "target").mkdir(parents=True)
    (project / "target" / "manifest.json").write_text(
        (acme / "dbt_project" / "target" / "manifest.json").read_text()
    )
    artifacts, skipped = load_dbt_detailed(project)
    assert artifacts == []
    assert len(skipped) == 7


def test_scan_reports_artifacts_that_yielded_no_measure(acme, tmp_path):
    result, _ = run_scan(
        dbt_path=str(acme / "dbt_project"),
        catalog_path=str(acme / "catalog.yml"),
        db_path=str(tmp_path / "s.db"),
    )
    # stg_orders and fct_orders are pass-through models with no aggregate.
    assert result.unmeasured
    assert result.stats()["unmeasured"] == len(result.unmeasured)
