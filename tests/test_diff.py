"""semantix diff: only newly introduced verdicts reach the PR comment."""

import shutil

from semantix.cli import main
from semantix.engine import run_scan
from semantix.services.diff import diff_verdicts, markdown_comment
from semantix.store import Store

FIXED_GROWTH = """select date_trunc('month', order_date) as revenue_month,
       sum(amount - refund_amount) as net_revenue
from analytics.fct_orders
where status = 'completed'
group by 1
"""


def _scan(estate, db, label):
    return run_scan(
        dbt_path=str(estate / "dbt_project"),
        metrics_path=str(estate / "metrics.yml"),
        catalog_path=str(estate / "catalog.yml"),
        db_path=str(db),
        label=label,
    )


def _growth_model(estate):
    return (estate / "dbt_project/target/compiled/acme/models/growth"
            / "growth_net_revenue.sql")


def test_fixing_the_contradiction_shows_as_resolved(acme, tmp_path):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "d.db"

    _scan(estate, db, "before")
    _growth_model(estate).write_text(FIXED_GROWTH)
    _scan(estate, db, "after")

    diff = diff_verdicts(Store(str(db)))
    assert [v["verdict"] for v in diff["resolved"]] == ["CONTRADICTION"]
    assert "CERTIFIED" in [v["verdict"] for v in diff["new"]]
    assert diff["unchanged"] >= 3  # gmv, customer_ltv, refund_rate untouched

    md = markdown_comment(diff)
    assert "resolved by this change" in md
    assert "`net_revenue`" in md


def test_breaking_a_metric_is_a_new_verdict_and_fails_the_gate(acme, tmp_path, capsys):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "d.db"

    _growth_model(estate).write_text(FIXED_GROWTH)  # start healthy
    _scan(estate, db, "before")
    _growth_model(estate).write_text(  # PR breaks it
        "select sum(amount) as net_revenue from analytics.stg_orders\n")
    _scan(estate, db, "after")

    md_out = tmp_path / "pr.md"
    rc = main(["diff", "--db", str(db), "--markdown", str(md_out),
               "--fail-on-new", "contradiction"])
    assert rc == 1  # the PR introduced a contradiction → gate fails
    md = md_out.read_text()
    assert "newly introduced verdict" in md
    assert "CONTRADICTION" in md
    assert "🔴" in md


def test_no_changes_means_clean_comment_and_exit_zero(acme, tmp_path):
    estate = tmp_path / "estate"
    shutil.copytree(acme, estate)
    db = tmp_path / "d.db"
    _scan(estate, db, "a")
    _scan(estate, db, "b")

    diff = diff_verdicts(Store(str(db)))
    assert diff["new"] == [] and diff["resolved"] == []
    assert "no new semantic verdicts" in markdown_comment(diff)
    assert main(["diff", "--db", str(db), "--fail-on-new", "any"]) == 0


def test_first_scan_has_no_baseline(acme, tmp_path):
    db = tmp_path / "d.db"
    _scan(acme, db, "only")
    assert diff_verdicts(Store(str(db))) is None
    assert main(["diff", "--db", str(db)]) == 2
