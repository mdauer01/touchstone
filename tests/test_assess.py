"""The advisory classifier's first delivery: record, criteria, providers, runner.

Contract tests only, with no network. A run on the `fake` provider shows the
machinery works and measures nothing about classification, and every report
says so. The labeled metric uses are business-definition data, kept separate
from the engine-correctness counterexamples.
"""

from __future__ import annotations

import json
import socket

import pytest

from semantix.assess import ADVISORY_LABELS
from semantix.assess.criteria import CHANGED, aggregate, load
from semantix.assess.providers import (ProviderConfig, get_provider, render_prompt)
from semantix.assess.record import FORMAT, OBSERVATIONS_NOTE, validate, without_observations
from semantix.assess.runner import (LABELS, UNMEASURED, build_record, gold_advisory, items,
                                    load_dataset, run_benchmark, semantix_alone)


@pytest.fixture(scope="module")
def dataset():
    return load_dataset()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the assess tests must not open a network connection")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ------------------------------------------------------------------ record

def test_every_dataset_item_yields_a_valid_record(dataset):
    for _, item in items(dataset):
        record = build_record(dataset, item)
        assert validate(record) == [], item["id"]
        assert record["format"] == FORMAT
        assert record["execution"]["statement"] == item["sql"]   # the original text
        # The label and its rationale never travel with the evidence.
        text = json.dumps(record)
        assert item["rationale"] not in text and '"label"' not in text


def test_the_validator_enforces_the_schema(dataset):
    record = build_record(dataset, items(dataset)[0][1])
    broken = {**record, "execution": {k: v for k, v in record["execution"].items()
                                      if k != "statement"}}
    assert any("missing statement" in e for e in validate(broken))
    assert any("unexpected field" in e for e in validate({**record, "verdict": "CERTIFIED"}))
    assert validate({**record, "format": "semantix-comparison/0"})
    claimed = {**record, "claimed_metric": {"name": None, "identity_evidence": [
        {"source": "guess", "value": "x"}]}}
    assert any("must be one of" in e for e in validate(claimed))


def test_observations_are_unverified_and_removable(dataset):
    item = items(dataset)[0][1]
    record = build_record(dataset, item, {"finding_kind": "suspected_difference",
                                          "reasons": [{"code": "x"}]})
    assert record["unverified_observations"]["note"] == OBSERVATIONS_NOTE
    assert "UNVERIFIED" in OBSERVATIONS_NOTE
    assert "unverified_observations" not in without_observations(record)
    assert validate(without_observations(record)) == []


def test_the_prompt_puts_the_statement_and_definition_before_semantix(dataset):
    item = items(dataset)[0][1]
    record = build_record(dataset, item, {"finding_kind": "unverifiable"})
    prompt = render_prompt(record, load(1))
    i_stmt = prompt.index("## Statement as it ran")
    i_def = prompt.index("## Authoritative definition")
    i_obs = prompt.index("## semantix's observations — UNVERIFIED")
    assert i_stmt < i_def < i_obs < prompt.index("## Questions")
    assert "## semantix's observations" not in render_prompt(without_observations(record),
                                                             load(1))


# ---------------------------------------------------------------- criteria

def test_criteria_have_an_unknown_answer_for_every_component():
    criteria = load(1)
    assert set(criteria.components) == set(CHANGED)
    for name in criteria.components:
        assert "unknown" in criteria.answers(name)


def test_the_label_is_computed_by_fixed_code_never_by_the_model():
    criteria = load(1)
    same = {"identity": "same_metric", "formula": "same", "population": "preserved",
            "grain": "permitted", "source": "same"}
    assert aggregate(same, criteria) == "likely_permitted"
    for component, changed in CHANGED.items():
        assert aggregate({**same, component: changed}, criteria) == "suspected_drift"
        assert aggregate({**same, component: "unknown"}, criteria) == "insufficient_evidence"
    # A missing or invented answer is unknown — never a match.
    assert aggregate({**same, "formula": "looks fine"}, criteria) == "insufficient_evidence"
    assert aggregate({}, criteria) == "insufficient_evidence"
    # One change outranks any number of unknowns.
    assert aggregate({"formula": "changed"}, criteria) == "suspected_drift"


# --------------------------------------------------------------- providers

def test_only_the_fake_backend_runs_in_this_delivery():
    with pytest.raises(ValueError):
        ProviderConfig("openai-direct")
    for kind in ("api", "databricks", "snowflake"):
        with pytest.raises(NotImplementedError, match="interface"):
            get_provider(ProviderConfig(kind, model="m", endpoint="e"))
    fake = get_provider(ProviderConfig("fake"))
    assert fake.measures_performance is False


def test_the_fake_backend_is_deterministic_and_aggregates_through_the_criteria(dataset):
    records = [build_record(dataset, i) for _, i in items(dataset, "dev")]
    a = get_provider(ProviderConfig("fake", options={"seed": "s"})).assess(records, load(1))
    b = get_provider(ProviderConfig("fake", options={"seed": "s"})).assess(records, load(1))
    assert [x.as_dict() for x in a] == [x.as_dict() for x in b]
    for x in a:
        assert x.label == aggregate(x.answers, load(1))
        assert abs(sum(x.probabilities.values()) - 1.0) < 1e-9
        assert set(x.probabilities) == set(ADVISORY_LABELS)


# ------------------------------------------------------------------ dataset

def test_the_dataset_is_split_by_family_and_labels_every_class(dataset):
    splits = {}
    for f in dataset["families"]:
        splits.setdefault(f["family"], set()).add(f["split"])
    assert all(len(s) == 1 for s in splits.values())
    assert items(dataset, "dev") and items(dataset, "test")
    labels = {i["label"] for _, i in items(dataset)}
    assert labels == set(LABELS)
    assert {gold_advisory(lab) for lab in labels} == set(ADVISORY_LABELS)


# ------------------------------------------------------------------- runner

def test_a_fake_run_reports_the_machinery_and_says_performance_is_unmeasured(dataset):
    report = run_benchmark(get_provider(ProviderConfig("fake")), dataset=dataset,
                           split="test")
    assert report["performance_measured"] is False and report["note"] == UNMEASURED
    overall = report["arms"]["without_observations"]["overall"]
    assert overall["n"] == len(items(dataset, "test"))
    for key in ("missed_drift", "false_alerts", "abstention", "calibration", "latency_ms",
                "cost_usd", "confusion"):
        assert key in overall
    assert set(report["arms"]["without_observations"]["by_family"]) == set(report["families"])
    assert "not run" in report["baselines"]["plain_prompt_model"]
    assert "with_observations" not in report["arms"]


def test_observations_add_the_ablation_and_the_semantix_alone_baseline(dataset):
    obs = {i["id"]: {"finding_kind": "suspected_difference"} for _, i in items(dataset, "dev")}
    report = run_benchmark(get_provider(ProviderConfig("fake")), dataset=dataset,
                           split="dev", observations=obs)
    assert set(report["arms"]) == {"without_observations", "with_observations"}
    alone = report["baselines"]["semantix_observations_alone"]
    assert alone["missed_drift"]["count"] == 0      # it flags everything here
    assert semantix_alone({"finding_kind": "unverifiable",
                           "experimental": {"kind": "conforming_use"}}) == "likely_permitted"
    assert semantix_alone(None) == "insufficient_evidence"


# ------------------------------------------------- the monitor's export

def test_the_monitor_exports_valid_records_with_the_statement_as_it_ran(tmp_path):
    from .test_monitor_conformance import GOVERNED, _rows, run

    sql = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
           f"WHERE status = 'completed' AND country = 'US'")
    result = run(tmp_path, _rows(sql, sql.replace("AND country = 'US'", "")))
    assert "comparisons" not in result          # only when asked
    from semantix.adapters.catalog import load_catalog
    from semantix.adapters.sql_dir import load_sql_dir
    from semantix.engine import run_scan
    from semantix.services.history import read_history
    from semantix.services.monitor import build_registry, monitor_history
    from semantix.services.pilot import load_pilot

    from .test_monitor_conformance import make_pilot

    root, models = make_pilot(tmp_path / "b", _rows(sql))
    _, store = run_scan(sql_dir=str(models), uc_metrics_path=str(root / "views"),
                        catalog_path=str(root / "catalog.yml"),
                        db_path=str(tmp_path / "b.db"), dialect="databricks")
    try:
        pilot = load_pilot(root, "databricks")
        registry = build_registry(load_catalog(root / "catalog.yml"),
                                  load_sql_dir(models, "databricks")[0], "databricks")
        plain = monitor_history(store, read_history(pilot.history), registry, "databricks",
                                min_runs=1, pilot=pilot)
        exported = monitor_history(store, read_history(pilot.history), registry,
                                   "databricks", min_runs=1, pilot=pilot, export=True)
    finally:
        store.close()
    # Findings, funnel and everything else are unchanged by exporting.
    assert {k: v for k, v in exported.items() if k != "comparisons"} == plain
    (record,) = exported["comparisons"]
    assert validate(record) == []
    assert record["execution"]["statement"] == sql            # as it ran, not re-rendered
    assert record["definition"]["snapshot_text"] and record["definition"]["snapshot_sha256"]
    assert record["definition"]["required_filters"] == ["status = 'completed'"]
    assert record["unverified_observations"]["note"] == OBSERVATIONS_NOTE
    assert record["unverified_observations"]["finding_kind"] == "unverifiable"


def test_the_cli_writes_comparisons_and_the_benchmark_report(tmp_path, capsys):
    from semantix.cli import main

    out = tmp_path / "report.json"
    assert main(["assess", "benchmark", "--split", "dev", "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "UNMEASURED" in printed and "[not a measurement]" in printed
    report = json.loads(out.read_text())
    assert report["performance_measured"] is False and report["split"] == "dev"
    assert main(["assess", "benchmark", "--provider", "snowflake"]) == 2


def test_the_blind_pass_covers_every_item_and_every_disagreement_is_adjudicated(dataset):
    import yaml

    from semantix.assess.runner import DATASET_DIR

    blind = {b["id"]: b["label"] for b in
             yaml.safe_load((DATASET_DIR / "blind_labels.yaml").read_text())["items"]}
    labels = {i["id"]: i["label"] for _, i in items(dataset)}
    assert set(blind) == set(labels)
    decisions = {d["id"]: d for d in dataset["adjudication"]["decisions"]}
    for iid, label in labels.items():
        if iid in decisions:
            d = decisions[iid]
            assert d["blind"] == blind[iid] and d["decision"] == label and d["why"]
        else:
            assert blind[iid] == label, f"{iid}: unadjudicated disagreement"
