"""Apply the global invariants to every estate shape we can construct.

The estates below are deliberately awkward — partial compiles, unparseable
files, empty projects, duplicate relations — because those are the shapes a
real third-party project has and the shapes three rounds of example-based
tests kept missing.
"""

import shutil

import pytest

from touchstone.engine import run_scan

from .invariants import check_scan

CATALOG = (
    "raw:\n"
    "  orders:\n"
    "    order_id: bigint\n"
    "    customer_id: bigint\n"
    "    status: varchar\n"
    "    amount: 'decimal(18,2)'\n"
    "    refund_amount: 'decimal(18,2)'\n"
)

GOOD = "select sum(amount - refund_amount) as net_revenue from raw.orders where status = 'completed'"
RIVAL = "select sum(amount) as net_revenue from raw.orders"
UNPARSEABLE = "select * from raw.orders model dimension by (order_id) measures (amount) rules ()"
NO_MEASURE = "select order_id, status from raw.orders"
UNION = ("select sum(amount) as net_revenue from raw.orders "
         "union all select sum(refund_amount) as net_revenue from raw.orders")


def sql_estate(tmp_path, models: dict[str, str]):
    root = tmp_path / "sql"
    (root / "analytics").mkdir(parents=True)
    for name, sql in models.items():
        (root / "analytics" / f"{name}.sql").write_text(sql)
    (tmp_path / "catalog.yml").write_text(CATALOG)
    return root


ESTATES = {
    "single clean model": {"a": GOOD},
    "two models disagreeing": {"a": GOOD, "b": RIVAL},
    "one unparseable sibling": {"a": GOOD, "bad": UNPARSEABLE},
    "only unparseable": {"bad": UNPARSEABLE},
    "model with no measure": {"a": GOOD, "plain": NO_MEASURE},
    "set operation": {"a": GOOD, "u": UNION},
    "everything at once": {"a": GOOD, "b": RIVAL, "bad": UNPARSEABLE,
                           "plain": NO_MEASURE, "u": UNION},
}


@pytest.mark.parametrize("name", list(ESTATES))
def test_invariants_hold_for_sql_estates(name, tmp_path):
    root = sql_estate(tmp_path, ESTATES[name])
    result, store = run_scan(
        sql_dir=str(root), catalog_path=str(tmp_path / "catalog.yml"),
        db_path=str(tmp_path / "s.db"),
    )
    problems = check_scan(result, store)
    assert not problems, f"[{name}]\n" + "\n".join(problems)


def test_invariants_hold_for_the_demo_estate(acme, tmp_path):
    result, store = run_scan(
        dbt_path=str(acme / "dbt_project"), metrics_path=str(acme / "metrics.yml"),
        catalog_path=str(acme / "catalog.yml"), db_path=str(tmp_path / "s.db"),
    )
    assert not check_scan(result, store), "\n".join(check_scan(result, store))


def test_invariants_hold_on_a_first_scan_of_a_partial_compile(acme, tmp_path):
    """The case round three found: a FRESH store, so no history exists to
    establish relevance. This is the documented CI flow."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    (estate / "dbt_project/target/compiled/acme/models/growth/"
     "growth_net_revenue.sql").unlink()

    result, store = run_scan(
        dbt_path=str(estate / "dbt_project"), metrics_path=str(estate / "metrics.yml"),
        catalog_path=str(estate / "catalog.yml"), db_path=str(tmp_path / "s.db"),
    )
    assert result.skipped, "precondition: the estate must be incompletely read"
    problems = check_scan(result, store)
    assert not problems, "\n".join(problems)


def test_invariants_hold_after_a_rescan(acme, tmp_path):
    """Second scans exercise the history-dependent paths."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"
    run_scan(dbt_path=str(estate / "dbt_project"), metrics_path=str(estate / "metrics.yml"),
             catalog_path=str(estate / "catalog.yml"), db_path=str(db))
    result, store = run_scan(
        dbt_path=str(estate / "dbt_project"), metrics_path=str(estate / "metrics.yml"),
        catalog_path=str(estate / "catalog.yml"), db_path=str(db),
    )
    assert not check_scan(result, store), "\n".join(check_scan(result, store))


def test_invariants_hold_when_the_declaration_appears_after_the_code(acme, tmp_path):
    """The ordinary adoption sequence — scan the code, then add the metrics
    layer — which round three showed renames the concept and voids prior
    knowledge."""
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "s.db"
    run_scan(dbt_path=str(estate / "dbt_project"),
             catalog_path=str(estate / "catalog.yml"), db_path=str(db))
    (estate / "dbt_project/target/compiled/acme/models/marts/total_gmv.sql").unlink()
    result, store = run_scan(
        dbt_path=str(estate / "dbt_project"), metrics_path=str(estate / "metrics.yml"),
        catalog_path=str(estate / "catalog.yml"), db_path=str(db),
    )
    problems = check_scan(result, store)
    assert not problems, "\n".join(problems)
