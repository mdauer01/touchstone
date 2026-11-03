"""The CI gate must not go green because the job ran twice.

Round-two adversarial review, finding 37. `diff` defaults to the two most
recent scans, so a re-run — a retried CI job, a flaky rerun, scanning the PR
branch twice — makes the baseline the PR's OWN previous scan. The regression
it was supposed to catch then appears in both, `new` is empty, and
`--fail-on-new` passes.
"""

import shutil

import pytest

from touchstone.cli import main
from touchstone.engine import run_scan
from touchstone.models import VerdictType
from touchstone.services.diff import diff_verdicts
from touchstone.store import Store

from .policy import engine_verdict

GROWTH = "dbt_project/target/compiled/acme/models/growth/growth_net_revenue.sql"
ALIGNED = (
    "select date_trunc('month', o.order_date) as revenue_month,\n"
    "       sum(o.amount - o.refund_amount) as net_revenue\n"
    "from analytics.fct_orders as o where o.status = 'completed' group by 1\n"
)


def scan(estate, db):
    return run_scan(
        dbt_path=str(estate / "dbt_project"),
        metrics_path=str(estate / "metrics.yml"),
        catalog_path=str(estate / "catalog.yml"),
        db_path=str(db),
    )


@pytest.fixture
def estate(acme, tmp_path):
    e = tmp_path / "estate"
    shutil.copytree(acme, e)
    return e


def test_rescanning_an_unchanged_estate_does_not_hide_a_regression(estate, tmp_path):
    """main (clean) -> PR (contradiction) -> PR again. The contradiction is
    still newly introduced relative to main, and the gate must still fail."""
    db = tmp_path / "s.db"

    # "main": growth agrees with the declaration
    (estate / GROWTH).write_text(ALIGNED)
    scan(estate, db)

    # "PR": growth diverges — a real regression
    (estate / GROWTH).write_text(
        "select date_trunc('month', o.order_date) as revenue_month,\n"
        "       sum(o.amount) as net_revenue\n"
        "from analytics.stg_orders as o group by 1\n"
    )
    scan(estate, db)

    store = Store(str(db))
    first = diff_verdicts(store)
    # The diff reads effective verdicts: the engine's CONTRADICTION surfaces
    # as a suspected divergence under the conclusions policy.
    assert any(engine_verdict(v) == VerdictType.CONTRADICTION
               and v["verdict"] == "SUSPECTED_DIVERGENCE" for v in first["new"]), (
        "the regression must be newly introduced on the first diff"
    )

    # The job is retried. Nothing about the estate changed.
    scan(estate, db)
    second = diff_verdicts(Store(str(db)))
    assert any(engine_verdict(v) == VerdictType.CONTRADICTION
               and v["verdict"] == "SUSPECTED_DIVERGENCE" for v in second["new"]), (
        "a re-run of an unchanged estate hid the regression"
    )


def test_cli_gate_still_fails_after_a_rerun(estate, tmp_path):
    db = str(tmp_path / "s.db")
    (estate / GROWTH).write_text(ALIGNED)
    scan(estate, db)
    (estate / GROWTH).write_text(
        "select date_trunc('month', o.order_date) as revenue_month,\n"
        "       sum(o.amount) as net_revenue\n"
        "from analytics.stg_orders as o group by 1\n"
    )
    scan(estate, db)
    scan(estate, db)  # retry

    assert main(["diff", "--db", db, "--fail-on-new", "contradiction"]) == 1


def test_a_genuine_fix_still_reads_as_resolved(estate, tmp_path):
    """The gate must not become permanently red: fixing the regression has to
    show up as resolved."""
    db = tmp_path / "s.db"
    scan(estate, db)                       # contradiction present
    (estate / GROWTH).write_text(ALIGNED)  # fixed
    scan(estate, db)

    diff = diff_verdicts(Store(str(db)))
    # Effective verdicts: the engine's CONTRADICTION is a suspected divergence.
    assert any(engine_verdict(v) == VerdictType.CONTRADICTION
               and v["verdict"] == "SUSPECTED_DIVERGENCE" for v in diff["resolved"])
    assert not any(engine_verdict(v) == VerdictType.CONTRADICTION for v in diff["new"])
    assert not any(v["verdict"] == "SUSPECTED_DIVERGENCE" for v in diff["new"])
