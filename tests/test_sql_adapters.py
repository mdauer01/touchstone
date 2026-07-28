"""Raw-SQL intake: estates without dbt still get verdicts."""

import pytest

from semantix.adapters.sql_dir import load_sql_dir, load_views_file
from semantix.engine import run_scan

FINANCE_SQL = """-- semantix: relation=analytics.finance_net_revenue
select date_trunc('month', order_date) as revenue_month,
       sum(amount - refund_amount) as net_revenue
from analytics.fct_orders
where status = 'completed'
group by 1
"""

FCT_ORDERS_SQL = """select * from raw.orders where status <> 'cancelled'
"""

VIEWS_DUMP = """
CREATE VIEW analytics.fct_orders AS
  SELECT * FROM raw.orders WHERE status <> 'cancelled';
CREATE OR REPLACE VIEW analytics.growth_net_revenue AS
  SELECT date_trunc('month', order_date) AS revenue_month,
         SUM(amount) AS net_revenue
  FROM analytics.fct_orders
  WHERE status IN ('completed', 'pending')
  GROUP BY 1;
"""


def test_sql_dir_naming_directive_and_topo_order(tmp_path):
    (tmp_path / "analytics").mkdir()
    (tmp_path / "analytics" / "fct_orders.sql").write_text(FCT_ORDERS_SQL)
    (tmp_path / "finance.sql").write_text(FINANCE_SQL)  # directive names it

    artifacts, _skipped = load_sql_dir(tmp_path)
    relations = [a.relation for a in artifacts]
    assert "analytics.finance_net_revenue" in relations  # directive won
    assert "analytics.fct_orders" in relations           # path-derived
    # dependency order: fct_orders registers before the model reading it
    assert relations.index("analytics.fct_orders") < relations.index(
        "analytics.finance_net_revenue")
    assert artifacts[-1].depends_on == ["analytics.fct_orders"]


def test_views_dump_parses_create_statements(tmp_path):
    dump = tmp_path / "views.sql"
    dump.write_text(VIEWS_DUMP)
    artifacts, _skipped = load_views_file(dump)
    assert [a.relation for a in artifacts] == [
        "analytics.fct_orders", "analytics.growth_net_revenue"]
    assert all(a.system == "views" for a in artifacts)
    assert "SUM(amount)" in artifacts[1].sql


def test_scan_without_dbt_reaches_a_contradiction(acme, tmp_path):
    estate = tmp_path / "sqlonly"
    (estate / "analytics").mkdir(parents=True)
    (estate / "analytics" / "fct_orders.sql").write_text(FCT_ORDERS_SQL)
    (estate / "finance.sql").write_text(FINANCE_SQL)
    views = tmp_path / "growth_views.sql"
    views.write_text(VIEWS_DUMP.split(";", 1)[1])  # only the growth view

    result, _ = run_scan(
        sql_dir=str(estate),
        views_path=str(views),
        metrics_path=str(acme / "metrics.yml"),
        catalog_path=str(acme / "catalog.yml"),
        db_path=str(tmp_path / "s.db"),
    )
    by_label = {v.concept_label: v for v in result.verdicts}
    v = by_label["net_revenue"]
    assert v.verdict_type.value == "CONTRADICTION"
    assert "missing filter status = 'completed'" in v.summary
    # cross-source systems both contributed fingerprints
    systems = {fp.provenance.system for fp in result.fingerprints}
    assert systems == {"sql", "views"}


def test_engine_requires_at_least_one_source(tmp_path):
    with pytest.raises(ValueError, match="no computed-SQL source"):
        run_scan(db_path=str(tmp_path / "x.db"))
