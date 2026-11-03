"""Inputs a real third-party project actually contains.

Round-three adversarial review, 2026-07-27. Every case here is ordinary in a
real estate and every one either crashed the scan, silently mis-identified a
relation, or read a file it had no business reading.
"""

import json


from touchstone.adapters.catalog import load_catalog
from touchstone.adapters.dbt import load_dbt_detailed
from touchstone.adapters.metrics_yaml import load_metrics
from touchstone.adapters.sql_dir import load_sql_dir
from touchstone.engine import run_scan

from .invariants import check_scan


# ------------------------------------------------------ relation identity

def test_dbt_relations_are_three_part_like_real_manifests(tmp_path):
    """Compiled SQL, relation_name and semantic_models all refer to models as
    database.schema.alias. Composing two-part names meant dependency edges,
    registry keys and lineage never matched the SQL itself."""
    project = tmp_path / "p"
    (project / "target").mkdir(parents=True)
    (project / "target" / "manifest.json").write_text(json.dumps({
        "nodes": {
            "model.p.a": {
                "resource_type": "model", "name": "a", "alias": "a",
                "database": "ANALYTICS_DB", "schema": "marts",
                "compiled_code": "select sum(x) as m from ANALYTICS_DB.raw.t",
                "depends_on": {"nodes": []},
            },
            "model.p.b": {
                "resource_type": "model", "name": "b", "alias": "b",
                "database": "ANALYTICS_DB", "schema": "marts",
                "relation_name": '"ANALYTICS_DB"."marts"."b_custom"',
                "compiled_code": "select sum(x) as m from ANALYTICS_DB.raw.t",
                "depends_on": {"nodes": []},
            },
        }
    }))
    artifacts = {a.object_id: a.relation for a in load_dbt_detailed(project)[0]}
    assert artifacts["model.p.a"] == "ANALYTICS_DB.marts.a"
    # An explicit relation_name wins, unquoted.
    assert artifacts["model.p.b"] == "ANALYTICS_DB.marts.b_custom"


def test_compiled_path_cannot_escape_the_project(tmp_path):
    """`compiled_path` is untrusted third-party input."""
    secret = tmp_path / "secret.sql"
    secret.write_text("select 'private' as leaked")
    project = tmp_path / "p"
    (project / "target").mkdir(parents=True)
    (project / "target" / "manifest.json").write_text(json.dumps({
        "nodes": {"model.p.evil": {
            "resource_type": "model", "name": "evil", "alias": "evil",
            "schema": "marts", "compiled_path": "../../secret.sql",
            "depends_on": {"nodes": []},
        }}
    }))
    artifacts, skipped = load_dbt_detailed(project)
    assert all("private" not in a.sql for a in artifacts)


# ------------------------------------------------------------ catalog depth

def test_catalog_describes_a_three_part_warehouse(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text(
        "ANALYTICS_DB:\n"
        "  raw:\n"
        "    orders:\n"
        "      order_id: bigint\n"
        "      amount: 'decimal(18,2)'\n"
    )
    catalog = load_catalog(p)
    assert catalog["ANALYTICS_DB.raw.orders"]["order_id"] == "bigint"


def test_catalog_still_reads_two_part_and_flat_shapes(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("raw:\n  orders:\n    order_id: bigint\n")
    assert "raw.orders" in load_catalog(p)
    q = tmp_path / "d.yml"
    q.write_text("raw.orders:\n  order_id: bigint\n")
    assert "raw.orders" in load_catalog(q)


# ------------------------------------------------------------ hostile files

def test_a_non_utf8_sql_file_does_not_kill_the_scan(tmp_path):
    root = tmp_path / "sql"
    (root / "analytics").mkdir(parents=True)
    (root / "analytics" / "good.sql").write_text("select sum(x) as m from raw.t")
    (root / "analytics" / "bad.sql").write_bytes(b"select \xff\xfe sum(x) as m from raw.t")
    artifacts, _skipped = load_sql_dir(root)
    assert any(a.object_id.endswith("good.sql") for a in artifacts)


def test_non_scalar_yaml_fields_do_not_crash_the_scan(tmp_path):
    """A mapping where a string belongs reached the store as a bind parameter
    and killed the scan mid-write with a SQLite type error."""
    p = tmp_path / "m.yml"
    p.write_text(
        "metrics:\n"
        "  - name: revenue\n"
        "    model: {schema: analytics, table: fct_orders}\n"
        "    expr: sum(amount)\n"
    )
    spec = load_metrics(p)[0]
    assert spec.model is None
    assert spec.untranslatable


def test_a_scan_over_hostile_inputs_completes_and_holds_invariants(tmp_path):
    """The composite: everything above in one estate."""
    root = tmp_path / "sql"
    (root / "analytics").mkdir(parents=True)
    (root / "analytics" / "good.sql").write_text(
        "select sum(amount) as revenue from raw.orders"
    )
    (root / "analytics" / "bad_bytes.sql").write_bytes(b"select \xff sum(x) from raw.t")
    (root / "analytics" / "unparseable.sql").write_text(
        "select * from raw.orders model dimension by (order_id) measures (amount) rules ()"
    )
    (tmp_path / "catalog.yml").write_text(
        "raw:\n  orders:\n    order_id: bigint\n    amount: 'decimal(18,2)'\n"
    )
    (tmp_path / "m.yml").write_text(
        "metrics:\n  - name: revenue\n    model: [not, a, string]\n    expr: sum(amount)\n"
    )

    result, store = run_scan(
        sql_dir=str(root), metrics_path=str(tmp_path / "m.yml"),
        catalog_path=str(tmp_path / "catalog.yml"), db_path=str(tmp_path / "s.db"),
    )
    assert result.artifacts >= 2
    assert not check_scan(result, store), "\n".join(check_scan(result, store))
