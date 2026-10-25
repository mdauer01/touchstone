"""Databricks-specific behaviour the conformance rules rely on — OPT-IN.

The DuckDB oracle cannot execute MEASURE(), so Shape B (a query over an
approved metric view) rests on one assumption about the Databricks runtime:
that `MEASURE(m)` over a metric view, filtered and grouped by its
dimensions, computes the view's declared measure over its source under the
view filter AND those slices. These tests execute both sides on a real
workspace and require exact agreement, and they check that semantix's rules
call the governed query conforming in the first place.

Skipped unless all of these are set (nothing here runs in CI):

    SEMANTIX_DATABRICKS_HOST       workspace host, e.g. dbc-xxxx.cloud.databricks.com
    SEMANTIX_DATABRICKS_HTTP_PATH  SQL warehouse HTTP path
    SEMANTIX_DATABRICKS_TOKEN      access token
    SEMANTIX_DATABRICKS_SCHEMA     a writable catalog.schema for test objects

and `pip install -e '.[databricks]'`. The tests create and drop
`semantix_it_orders` and `semantix_it_revenue_mv` in that schema. Status:
written against the metric-view YAML 1.1 spec; NOT YET RUN against a
workspace — the first run is what qualifies them.
"""

from __future__ import annotations

import decimal
import os
from pathlib import Path

import pytest
import sqlglot
import yaml

from semantix.adapters.uc_metrics import _read_view
from semantix.engine import build_claim
from semantix.extraction.registry import SchemaRegistry
from semantix.services.conformance import Authority, check_metric_view

_ENV = ("SEMANTIX_DATABRICKS_HOST", "SEMANTIX_DATABRICKS_HTTP_PATH",
        "SEMANTIX_DATABRICKS_TOKEN", "SEMANTIX_DATABRICKS_SCHEMA")
pytestmark = pytest.mark.skipif(
    not all(os.environ.get(k) for k in _ENV),
    reason="Databricks integration tests are opt-in: set " + ", ".join(_ENV))

ROWS = [
    # amount, refund_amount, status, country, channel, order_date
    (100.0, 10.0, "completed", "US", "web", "2026-01-05"),
    (50.0, 0.0, "pending", "US", "app", "2026-01-20"),
    (75.0, 5.0, "completed", "CA", "app", "2026-02-02"),
    (20.0, 20.0, "cancelled", "US", "web", "2026-02-15"),
    (300.0, None, "completed", "US", None, "2026-03-01"),
    (None, 1.0, "completed", None, "web", None),
    (1e16, 1.0, "completed", "US", "web", "2026-03-02"),
    (-1e16, 0.0, "completed", "US", "app", "2026-03-03"),
]
VIEW_YAML = """version: 1.1
source: {orders}
filter: status = 'completed'
dimensions:
  - name: country
    expr: country
  - name: channel
    expr: channel
  - name: order_date
    expr: order_date
  - name: month
    expr: DATE_TRUNC('MONTH', order_date)
measures:
  - name: net_revenue
    expr: SUM(amount) - SUM(refund_amount)
"""


@pytest.fixture(scope="module")
def workspace():
    sql = pytest.importorskip("databricks.sql")
    schema = os.environ["SEMANTIX_DATABRICKS_SCHEMA"]
    orders, view = f"{schema}.semantix_it_orders", f"{schema}.semantix_it_revenue_mv"
    con = sql.connect(server_hostname=os.environ["SEMANTIX_DATABRICKS_HOST"],
                      http_path=os.environ["SEMANTIX_DATABRICKS_HTTP_PATH"],
                      access_token=os.environ["SEMANTIX_DATABRICKS_TOKEN"])
    cur = con.cursor()
    cur.execute(f"CREATE OR REPLACE TABLE {orders} (amount DOUBLE, refund_amount DOUBLE, "
                f"status STRING, country STRING, channel STRING, order_date DATE)")
    values = ", ".join(
        "(" + ", ".join("NULL" if v is None else (f"'{v}'" if isinstance(v, str) else repr(v))
                        for v in row[:5])
        + (", NULL" if row[5] is None else f", DATE '{row[5]}'") + ")"
        for row in ROWS)
    cur.execute(f"INSERT INTO {orders} VALUES {values}")
    cur.execute(f"CREATE OR REPLACE VIEW {view} WITH METRICS LANGUAGE YAML AS $$\n"
                f"{VIEW_YAML.format(orders=orders)}$$")
    try:
        yield cur, orders, view
    finally:
        cur.execute(f"DROP VIEW IF EXISTS {view}")
        cur.execute(f"DROP TABLE IF EXISTS {orders}")
        cur.close()
        con.close()


def _rows(cur, sql):
    cur.execute(sql)
    out = []
    for row in cur.fetchall():
        out.append(tuple(decimal.Decimal(v).normalize() if isinstance(v, float) else v
                         for v in row))
    return sorted(out, key=repr)


def _conforms(view, sql, orders):
    """semantix's own verdict on the governed query."""
    specs, skipped = [], []
    _read_view(Path("semantix_it_revenue_mv.yaml"),
               yaml.safe_load(VIEW_YAML.format(orders=orders)), "databricks", specs, skipped)
    (spec,) = specs
    registry = SchemaRegistry({orders: {"amount": "DOUBLE", "refund_amount": "DOUBLE",
                                        "status": "STRING", "country": "STRING",
                                        "channel": "STRING", "order_date": "DATE"}})
    claim = build_claim(spec, registry, "databricks")
    auth = Authority(concept="net_revenue", system="uc_metrics", name="net_revenue",
                     core=claim.core, dimensions=spec.dimensions, view=view.lower(),
                     measure="net_revenue")
    (proj,) = check_metric_view(sqlglot.parse_one(sql, read="databricks"),
                                {view.lower(): {"net_revenue": auth}}, "databricks")
    return proj.outcome.status


def test_measure_with_slice_and_grouping_is_the_shape_a_expansion(workspace):
    cur, orders, view = workspace
    governed = (f"SELECT channel, MEASURE(net_revenue) AS net_revenue FROM {view} "
                f"WHERE country = 'US' GROUP BY channel")
    assert _conforms(view, governed, orders) == "conforming"
    expansion = (f"SELECT channel, SUM(amount) - SUM(refund_amount) AS net_revenue "
                 f"FROM {orders} WHERE status = 'completed' AND country = 'US' "
                 f"GROUP BY channel")
    assert _rows(cur, governed) == _rows(cur, expansion)


def test_view_filter_applies_without_a_where(workspace):
    cur, orders, view = workspace
    governed = f"SELECT MEASURE(net_revenue) AS net_revenue FROM {view}"
    assert _conforms(view, governed, orders) == "conforming"
    assert _rows(cur, governed) == _rows(
        cur, f"SELECT SUM(amount) - SUM(refund_amount) FROM {orders} WHERE status = 'completed'")


def test_date_trunc_dimension_groups_like_its_expression(workspace):
    cur, orders, view = workspace
    governed = (f"SELECT month, MEASURE(net_revenue) AS net_revenue FROM {view} "
                f"GROUP BY month")
    assert _conforms(view, governed, orders) == "conforming"
    assert _rows(cur, governed) == _rows(
        cur, f"SELECT DATE_TRUNC('MONTH', order_date), SUM(amount) - SUM(refund_amount) "
             f"FROM {orders} WHERE status = 'completed' GROUP BY 1")
