"""Both sides of the reconciliation live in the same manifest.

Round-two adversarial review, finding 34. A dbt project using the Semantic
Layer carries `metrics` and `semantic_models` beside `nodes`, and the adapter
read only `nodes` — so every declared metric was reported as a SHADOW_METRIC
(computed but undeclared) while its declaration sat unread in the file
already open.
"""

import json
import shutil

from semantix.adapters.dbt import load_dbt_detailed, load_dbt_metrics
from semantix.engine import run_scan
from semantix.models import VerdictType


def with_semantic_layer(acme, tmp_path):
    """The acme project plus a MetricFlow declaration of net_revenue that
    matches finance's model and therefore contradicts growth's."""
    project = tmp_path / "proj"
    shutil.copytree(acme / "dbt_project", project)
    manifest_path = project / "target" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    # The real MetricFlow shape: measures are DEFINED on the semantic model
    # and a metric merely REFERENCES one by name. Reading agg/expr off the
    # reference (as the first version of this adapter did) never resolves.
    manifest["semantic_models"] = {
        "semantic_model.acme.orders": {
            "name": "orders",
            "node_relation": {"relation_name": "analytics.fct_orders", "alias": "fct_orders"},
            "measures": [
                {"name": "net_revenue_measure", "agg": "sum",
                 "expr": "amount - refund_amount"},
            ],
        }
    }
    manifest["metrics"] = {
        "metric.acme.net_revenue": {
            "name": "net_revenue",
            "description": "Revenue net of refunds.",
            "type": "simple",
            "original_file_path": "models/metrics.yml",
            "type_params": {"measure": {"name": "net_revenue_measure"}},
        },
        # A composed metric the SemanticCore cannot represent. It must be
        # reported as untranslatable, never guessed into a wrong claim.
        "metric.acme.margin": {
            "name": "margin", "type": "ratio",
            "type_params": {"numerator": "net_revenue", "denominator": "gmv"},
        },
    }
    manifest_path.write_text(json.dumps(manifest))
    return project


def test_simple_metrics_are_read_from_the_manifest(acme, tmp_path):
    project = with_semantic_layer(acme, tmp_path)
    specs = {s.name: s for s in load_dbt_metrics(project)}

    assert specs["net_revenue"].expr == "sum(amount - refund_amount)"
    assert specs["net_revenue"].model == "analytics.fct_orders"
    assert specs["net_revenue"].system == "dbt_metrics"
    assert not specs["net_revenue"].untranslatable


def test_composed_metrics_are_reported_not_guessed(acme, tmp_path):
    project = with_semantic_layer(acme, tmp_path)
    margin = {s.name: s for s in load_dbt_metrics(project)}["margin"]
    assert margin.expr is None
    assert margin.untranslatable
    assert "ratio" in margin.untranslatable[0]


def test_metric_level_filters_make_the_claim_inexpressible(acme, tmp_path):
    """MetricFlow filters are Jinja templates (`Dimension(...)`) we cannot
    render. Dropping them silently produced a claim without its filter — a
    different claim, then reconciled as if it were the declared one."""
    project = with_semantic_layer(acme, tmp_path)
    manifest_path = project / "target" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["metrics"]["metric.acme.net_revenue"]["filter"] = {
        "where_filters": [
            {"where_sql_template": "{{ Dimension('order__status') }} = 'completed'"}
        ]
    }
    manifest_path.write_text(json.dumps(manifest))

    spec = {s.name: s for s in load_dbt_metrics(project)}["net_revenue"]
    assert spec.untranslatable
    assert "filter" in spec.untranslatable[0]


def test_a_manifest_without_a_semantic_layer_yields_nothing(acme):
    assert load_dbt_metrics(acme / "dbt_project") == []


def test_declared_dbt_metrics_reconcile_against_the_code(acme, tmp_path):
    """The point of the fix: with no separate metrics file, a dbt project
    alone now produces a real verdict instead of a shadow metric."""
    project = with_semantic_layer(acme, tmp_path)
    result, _ = run_scan(
        dbt_path=str(project),
        catalog_path=str(acme / "catalog.yml"),
        db_path=str(tmp_path / "s.db"),
    )
    net_revenue = [v for v in result.verdicts if v.concept_label == "net_revenue"]
    kinds = {v.verdict_type for v in net_revenue}
    assert VerdictType.SHADOW_METRIC not in kinds, (
        "a declared metric was reported as undeclared"
    )
    assert VerdictType.CONTRADICTION in kinds, (
        "growth's model disagrees with the declaration and must be caught"
    )


def test_non_model_node_types_are_disclosed(acme, tmp_path):
    """Snapshots define real transformations. Ignoring them silently made
    coverage look complete when it was not."""
    project = tmp_path / "proj2"
    shutil.copytree(acme / "dbt_project", project)
    manifest_path = project / "target" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["nodes"]["snapshot.acme.orders_snap"] = {
        "resource_type": "snapshot", "name": "orders_snap", "alias": "orders_snap",
        "schema": "snapshots", "original_file_path": "snapshots/orders.sql",
        "depends_on": {"nodes": []},
    }
    manifest_path.write_text(json.dumps(manifest))

    _, skipped = load_dbt_detailed(project)
    assert any(s["object_id"] == "snapshot.acme.orders_snap" for s in skipped)
