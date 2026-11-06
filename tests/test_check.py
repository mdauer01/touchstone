"""`touchstone check` end to end: which definition a use is compared with,
whether it applied when the statement ran, and that nothing it could not
compare disappears from the report."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from touchstone.assess.criteria import aggregate, load
from touchstone.assess.providers import Assessment
from touchstone.cli import main
from touchstone.report.check import render_check
from touchstone.services.check import run_check

ROOT = Path(__file__).resolve().parents[1]
REVENUE = """version: 1.1
source: cat.sales.lineitem
filter: l_returnflag <> 'R'
measures:
  - name: revenue
    expr: SUM(l_extendedprice * (1 - l_discount))
"""
ORDER_REVENUE = """version: 1.1
source: cat.sales.orders
measures:
  - name: revenue
    expr: SUM(o_totalprice)
"""
CUSTOMERS = """version: 1.1
source: cat.sales.orders
measures:
  - name: active_customers
    expr: APPROX_COUNT_DISTINCT(o_custkey)
"""


class Preserved:
    """A stub model that answers "preserved" for whatever it is sent, and
    keeps what it was sent."""

    name, model, measures_performance = "stub", None, True

    def __init__(self):
        self.seen: list[dict] = []

    def assess(self, records, criteria):
        self.seen += records
        out = []
        for r in records:
            answers = {"comparison": "preserved", "presented_as": "revenue",
                       "statement_clause": "", "definition_clause": "", "explanation": ""}
            out.append(Assessment(record_id=r["record_id"], provider=self.name, model=None,
                                  criteria_version=criteria.version, answers=answers,
                                  label=aggregate(answers, criteria)))
        return out


def package(tmp_path, rows, views=(("cat.sales.revenue_mv", REVENUE),),
            applies_from="2026-11-01T00:00:00Z"):
    root = tmp_path / "pilot"
    (root / "views").mkdir(parents=True)
    entries = []
    for name, text in views:
        path = root / "views" / (name.rsplit(".", 1)[-1] + ".yaml")
        path.write_text(text, encoding="utf-8")
        entries.append({"name": name, "snapshot": f"views/{path.name}",
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "applies": {"from": applies_from, "to": None}})
    with open(root / "history.jsonl", "w", encoding="utf-8") as f:
        for i, (sql, start) in enumerate(rows):
            f.write(json.dumps({"statement_id": f"s{i}", "statement_text": sql,
                                "start_time": start, "client_application": "editor"}) + "\n")
    manifest = {"history": "history.jsonl", "compare_against": "as_of_execution",
                "window": {"from": "2026-10-01T00:00:00Z", "to": "2026-12-01T00:00:00Z"},
                "authority": {"system": "uc_metrics", "metric_views": entries}}
    (root / "pilot.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return root


IN_FORCE = "2026-11-10T09:00:00Z"
SQL = ("SELECT SUM(l_extendedprice * (1 - l_discount)) AS revenue FROM cat.sales.lineitem "
       "WHERE l_returnflag <> 'R'")


def check(root, provider=None):
    provider = provider or Preserved()
    return run_check(root, provider, load(6)), provider


def states(result):
    return sorted((v.get("label") or "not_assessed", v.get("view")) for v in result["verdicts"])


def test_measure_on_a_view_is_that_views_measure(tmp_path):
    root = package(tmp_path, [("SELECT MEASURE(revenue) AS revenue FROM cat.sales.order_mv",
                               IN_FORCE)],
                   views=(("cat.sales.revenue_mv", REVENUE), ("cat.sales.order_mv", ORDER_REVENUE)))
    result, provider = check(root)
    assert [r["definition"]["view"] for r in provider.seen] == ["cat.sales.order_mv"]
    assert "SUM(o_totalprice)" in provider.seen[0]["definition"]["snapshot_text"]


def test_a_source_read_picks_the_definition_over_it(tmp_path):
    root = package(tmp_path, [(SQL, IN_FORCE)],
                   views=(("cat.sales.revenue_mv", REVENUE), ("cat.sales.order_mv", ORDER_REVENUE)))
    result, provider = check(root)
    assert [r["definition"]["view"] for r in provider.seen] == ["cat.sales.revenue_mv"]


def test_a_name_two_views_declare_is_not_resolved_to_the_first(tmp_path):
    root = package(tmp_path, [("SELECT SUM(amount) AS revenue FROM cat.other.ledger", IN_FORCE)],
                   views=(("cat.sales.revenue_mv", REVENUE), ("cat.sales.order_mv", ORDER_REVENUE)))
    result, provider = check(root)
    assert provider.seen == []
    [gap] = result["verdicts"]
    assert gap["label"] is None and "more than one approved definition" in gap["error"]
    assert result["failed"] == [] and result["not_assessed"] == [gap["record_id"]]


def test_a_definition_that_did_not_apply_yet_is_not_compared(tmp_path):
    root = package(tmp_path, [(SQL, "2026-10-15T09:00:00Z"), (SQL, None), (SQL, IN_FORCE)])
    result, provider = check(root)
    # Only the run inside the definition's window reaches the model.
    assert len(provider.seen) == 1
    assert provider.seen[0]["occurrences"]["executions"] == 1
    reasons = sorted(v["error"] for v in result["verdicts"] if v.get("label") is None)
    assert len(reasons) == 2
    assert "did not apply (it applies from 2026-11-01" in reasons[0]
    assert "start time is missing" in reasons[1]
    assert states(result).count(("no_issue_flagged", "cat.sales.revenue_mv")) == 1


def test_a_measure_the_sql_rules_cannot_represent_is_still_checked(tmp_path):
    root = package(tmp_path, [("SELECT APPROX_COUNT_DISTINCT(o_custkey) AS active_customers "
                               "FROM cat.sales.orders", IN_FORCE)],
                   views=(("cat.sales.customers_mv", CUSTOMERS),))
    result, provider = check(root)
    assert [r["definition"]["name"] for r in provider.seen] == ["active_customers"]
    assert "APPROX_COUNT_DISTINCT" in provider.seen[0]["definition"]["expr"]


def test_unparseable_sql_that_mentions_a_metric_is_reported(tmp_path):
    root = package(tmp_path, [("SELECT SUM(l_extendedprice) AS revenue FROM lineitem WHERE (((",
                               IN_FORCE),
                              ("SELEC nonsense FROM ((( elsewhere", IN_FORCE)])
    result, provider = check(root)
    assert provider.seen == []
    [gap] = result["verdicts"]
    assert "could not be parsed" in gap["error"] and "revenue" in gap["error"]
    assert result["history"]["unparseable"] == 2
    assert result["history"]["unparseable_unrelated"] == 1
    page = render_check(result)
    assert "Not assessed (1)" in page and "could not be parsed" in page
    assert "1 of them mention no governed" in page


def test_check_refuses_criteria_without_three_states(tmp_path, capsys):
    root = package(tmp_path, [(SQL, IN_FORCE)])
    code = main(["check", "--pilot", str(root), "--provider", "fake", "--criteria", "4",
                 "--out", str(tmp_path / "out")])
    assert code == 2
    assert "three review states" in capsys.readouterr().err


def test_check_writes_every_use_including_what_it_could_not_compare(tmp_path):
    root = package(tmp_path, [(SQL, IN_FORCE), (SQL, "2026-10-15T09:00:00Z")])
    out = tmp_path / "out"
    assert main(["check", "--pilot", str(root), "--provider", "fake", "--out", str(out)]) == 0
    findings = [json.loads(line) for line in (out / "findings.jsonl").read_text().splitlines()]
    assert len(findings) == 2
    assert sum(1 for f in findings if f.get("label") is None) == 1
    assert "Not assessed (1)" in (out / "report.html").read_text()


def test_session_evidence_reaches_the_prompt_only_when_recorded(tmp_path):
    from touchstone.assess.providers import render_prompt

    root = package(tmp_path, [(SQL, IN_FORCE)])
    result, provider = check(root)
    record = provider.seen[0]
    assert "## The session it ran in" not in render_prompt(record, load(6))
    record = {**record, "session_evidence": {"context": {"catalog": "cat"},
                                             "events": ["USE SCHEMA sales"], "unresolved": []}}
    prompt = render_prompt(record, load(6))
    assert "## The session it ran in" in prompt and '"USE SCHEMA sales"' in prompt


def test_the_minimal_example_runs(tmp_path):
    out = tmp_path / "out"
    assert main(["check", "--pilot", str(ROOT / "examples/minimal"), "--provider", "fake",
                 "--out", str(out)]) == 0
    findings = [json.loads(line) for line in (out / "findings.jsonl").read_text().splitlines()]
    assert len(findings) == 3 and all(f.get("label") for f in findings)


def test_the_published_fresh_test_findings_rescore_to_the_reported_result():
    from touchstone.benchmark.harness import score

    test = ROOT / "src/touchstone/benchmark/v6_fresh"
    result = score(test / "findings.jsonl", test)     # refuses findings that are not sealed
    assert (result["recall"], result["precision"], result["release_bar_met"]) == (1.0, 1.0, True)
    assert result["expected_vs_model"] == {"review_needed / review_needed": 7,
                                           "no_issue_flagged / no_issue_flagged": 15,
                                           "could_not_assess / could_not_assess": 4}


def test_current_mode_leaves_out_past_versions(tmp_path):
    root = package(tmp_path, [(SQL, IN_FORCE)])
    manifest = yaml.safe_load((root / "pilot.yml").read_text())
    manifest["compare_against"] = "current"
    manifest["authority"]["metric_views"][0]["applies"]["to"] = "2026-11-05T00:00:00Z"
    (root / "pilot.yml").write_text(yaml.safe_dump(manifest))
    result, provider = check(root)
    assert provider.seen == [] and result["verdicts"] == []
