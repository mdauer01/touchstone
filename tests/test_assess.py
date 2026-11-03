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

from touchstone.assess import ADVISORY_LABELS
from touchstone.assess.criteria import CHANGED, aggregate, load
from touchstone.assess.providers import (ProviderConfig, get_provider, render_prompt)
from touchstone.assess.record import FORMAT, OBSERVATIONS_NOTE, validate, without_observations
from touchstone.assess.runner import (LABELS, UNMEASURED, build_record, gold_advisory, items,
                                    load_dataset, run_benchmark, touchstone_alone)


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
    assert validate({**record, "format": "touchstone-comparison/0"})
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


def test_the_prompt_puts_the_statement_and_definition_before_touchstone(dataset):
    item = items(dataset)[0][1]
    record = build_record(dataset, item, {"finding_kind": "unverifiable"})
    prompt = render_prompt(record, load(1))
    i_stmt = prompt.index("## Statement as it ran")
    i_def = prompt.index("## Authoritative definition")
    i_obs = prompt.index("## touchstone's observations — UNVERIFIED")
    assert i_stmt < i_def < i_obs < prompt.index("## Questions")
    assert "## touchstone's observations" not in render_prompt(without_observations(record),
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

def test_fake_and_databricks_run_and_the_others_are_not_implemented(monkeypatch):
    with pytest.raises(ValueError):
        ProviderConfig("openai-direct")
    for kind in ("api", "snowflake"):
        with pytest.raises(NotImplementedError, match="not implemented yet"):
            get_provider(ProviderConfig(kind, model="m", endpoint="e"))
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="DATABRICKS_TOKEN"):
        get_provider(ProviderConfig("databricks", model="system.ai.m", endpoint="abc123",
                                    options={"host": "https://x.cloud.databricks.com"}))
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
    for key in ("drift", "permitted", "insufficient_evidence", "abstention", "calibration",
                "latency_ms", "cost_usd", "confusion"):
        assert key in overall
    assert set(report["arms"]["without_observations"]["by_family"]) == set(report["families"])
    assert "not run" in report["baselines"]["plain_prompt_model"]
    assert "with_observations" not in report["arms"]


def test_observations_add_the_ablation_and_the_touchstone_alone_baseline(dataset):
    obs = {i["id"]: {"finding_kind": "suspected_difference"} for _, i in items(dataset, "dev")}
    report = run_benchmark(get_provider(ProviderConfig("fake")), dataset=dataset,
                           split="dev", observations=obs)
    assert set(report["arms"]) == {"without_observations", "with_observations"}
    alone = report["baselines"]["touchstone_observations_alone"]
    assert alone["drift"]["flagged"]["count"] == alone["drift"]["of"]   # flags everything
    assert alone["drift"]["labeled_permitted"]["count"] == 0
    assert touchstone_alone({"finding_kind": "unverifiable",
                           "experimental": {"kind": "conforming_use"}}) == "likely_permitted"
    assert touchstone_alone(None) == "insufficient_evidence"


# ------------------------------------------------- the monitor's export

def test_the_monitor_exports_valid_records_with_the_statement_as_it_ran(tmp_path):
    from .test_monitor_conformance import GOVERNED, _rows, run

    sql = (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
           f"WHERE status = 'completed' AND country = 'US'")
    result = run(tmp_path, _rows(sql, sql.replace("AND country = 'US'", "")))
    assert "comparisons" not in result          # only when asked
    from touchstone.adapters.catalog import load_catalog
    from touchstone.adapters.sql_dir import load_sql_dir
    from touchstone.engine import run_scan
    from touchstone.services.history import read_history
    from touchstone.services.monitor import build_registry, monitor_history
    from touchstone.services.pilot import load_pilot

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
    from touchstone.cli import main

    out = tmp_path / "report.json"
    assert main(["assess", "benchmark", "--split", "dev", "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "UNMEASURED" in printed and "[not a measurement]" in printed
    report = json.loads(out.read_text())
    assert report["performance_measured"] is False and report["split"] == "dev"
    assert main(["assess", "benchmark", "--provider", "snowflake"]) == 2


def test_the_blind_pass_covers_every_item_and_every_disagreement_is_adjudicated(dataset):
    import yaml

    from touchstone.assess.runner import DATASET_DIR

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



# ------------------------------- evidence delivery and reporting (review of dec798f)

def test_every_field_that_decides_an_answer_reaches_the_prompt(dataset):
    """temp-01 and temp-02 differ ONLY in the temp view an earlier session
    statement created (one drops nonpositive amounts, one passes everything
    through); they are labeled drift and permitted. Their prompts were
    identical without touchstone's observations."""
    by_id = {i["id"]: i for _, i in items(dataset)}
    p1 = render_prompt(build_record(dataset, by_id["temp-01"]), load(1))
    p2 = render_prompt(build_record(dataset, by_id["temp-02"]), load(1))
    assert p1 != p2
    assert "CREATE TEMP VIEW o AS SELECT * FROM main.finance.orders WHERE amount > 0" in p1
    assert "WHERE amount > 0" not in p2
    record = build_record(dataset, by_id["slices-01"])
    prompt = render_prompt(record, load(1))
    assert record["execution"]["start_time"] in prompt               # when it ran
    assert '"applies"' in prompt and record["definition"]["applies"]["from"] in prompt
    assert "## Governing variation policy" in prompt
    assert dataset["definitions"]["policy"].strip().splitlines()[0] in prompt
    assert "no earlier statement of this session was recorded" in prompt
    order = [prompt.index(h) for h in ("## Statement as it ran", "## Execution",
                                       "## Session before this statement",
                                       "## Authoritative definition",
                                       "## Governing variation policy", "## Questions")]
    assert order == sorted(order)


def test_the_model_and_the_monitor_baseline_see_the_same_execution(dataset):
    from touchstone.assess.runner import execution_context

    for _, item in items(dataset):
        record = build_record(dataset, item)
        ctx = execution_context(dataset, item)
        assert record["execution"]["start_time"] == ctx["start_time"] is not None
        assert record["execution"]["session_id"] == ctx["session_id"]
        applies = dataset["definitions"]["applies"]
        assert ctx["start_time"] >= applies["from"]       # inside the validity window


def test_the_dataset_policy_is_the_readme_policy(dataset):
    from touchstone.assess.runner import DATASET_DIR

    readme = " ".join((DATASET_DIR / "README.md").read_text().split())
    assert " ".join(dataset["definitions"]["policy"].split()) in readme


def test_undecided_drift_is_never_reported_as_caught():
    """Abstaining on every drift reassured no one wrongly — and caught
    nothing. All three are reported together."""
    from touchstone.assess.runner import _Fixed, _metrics, _row

    m = _metrics([_row("suspected_drift", _Fixed("insufficient_evidence"))] * 15)
    assert m["drift"]["of"] == 15
    assert m["drift"]["flagged"]["count"] == 0
    assert m["drift"]["labeled_permitted"]["count"] == 0
    assert m["drift"]["undecided"]["count"] == 15
    assert m["drift"]["failed"]["count"] == 0
    assert "missed_drift" not in m


def test_the_cli_reports_flagged_permitted_and_undecided(capsys):
    from touchstone.cli import main

    assert main(["assess", "benchmark", "--split", "test"]) == 0
    out = capsys.readouterr().out
    assert "flagged" in out and "labeled permitted" in out and "undecided" in out
    assert "missed drift" not in out


def test_the_review_sheet_is_blind_and_flags_what_needs_a_human(dataset, tmp_path):

    from touchstone.assess.runner import review_sheet
    from touchstone.cli import main

    rows = review_sheet(dataset)
    assert len(rows) == len(items(dataset))
    assert all(r["label"] == "" and r["rationale"] == "" for r in rows)
    flagged = {r["id"]: r["priority"] for r in rows if r["priority"]}
    for d in dataset["adjudication"]["decisions"]:
        assert "contested" in flagged[d["id"]]
    assert "grouping policy" in flagged["time-05"]
    out = tmp_path / "sheet.csv"
    assert main(["assess", "review-sheet", "--out", str(out)]) == 0
    text = out.read_text() + out.with_name("sheet.guide.md").read_text()
    for _, item in items(dataset):
        assert item["rationale"] not in text      # never the labels' reasons


def test_compare_labels_reports_every_disagreement_and_changes_nothing(dataset, tmp_path):
    import csv

    from touchstone.cli import main

    out = tmp_path / "sheet.csv"
    main(["assess", "review-sheet", "--out", str(out)])
    rows = list(csv.DictReader(out.open()))
    labels = {i["id"]: i["label"] for _, i in items(dataset)}
    for r in rows:
        r["label"] = labels[r["id"]]
    rows[0]["label"] = ("permitted_variation" if labels[rows[0]["id"]] != "permitted_variation"
                        else "formula_drift")
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    before = {i["id"]: i["label"] for _, i in items(load_dataset())}
    assert main(["assess", "compare-labels", str(out)]) == 0
    assert {i["id"]: i["label"] for _, i in items(load_dataset())} == before



# ------------------------------------------- review tooling (round-18 review)

def test_the_sheet_does_not_depend_on_the_labels(dataset):
    """The priority column was computed partly from the label (it flagged
    exactly the grain_drift items): permuting every label must change nothing."""
    import copy
    import random

    from touchstone.assess.runner import review_sheet

    shuffled = copy.deepcopy(dataset)
    labels = [i["label"] for _, i in items(shuffled)]
    random.Random(7).shuffle(labels)
    for (_, item), label in zip(items(shuffled), labels):
        item["label"] = label
        item["rationale"] = "x"
    assert review_sheet(shuffled) == review_sheet(dataset)


def test_the_guide_carries_what_the_labels_rest_on(tmp_path):
    from touchstone.cli import main

    out = tmp_path / "s.csv"
    assert main(["assess", "review-sheet", "--out", str(out)]) == 0
    guide = (tmp_path / "s.guide.md").read_text()
    for needed in ("`amount` DOUBLE", "`refund_amount` is nullable",
                   "referential integrity are not stated", "main.finance.customers",
                   "main.finance.order_items", "session_statements", "blind_labels.yaml",
                   "adjudication.yaml", "metric view `main.finance.revenue_mv` over",
                   "`docs/assess.md` below its review instructions"):
        assert needed in guide, needed
    # Never overwritten silently: a filled sheet would be lost.
    assert main(["assess", "review-sheet", "--out", str(out)]) == 2
    assert main(["assess", "review-sheet", "--out", str(out), "--force"]) == 0


def _sheet(tmp_path, mutate, encoding="utf-8", header=None):
    import csv

    from touchstone.assess.runner import REVIEW_COLUMNS, review_sheet

    ds = load_dataset()
    rows = review_sheet(ds)
    labels = {i["id"]: i["label"] for _, i in items(ds)}
    for r in rows:
        r["label"] = labels[r["id"]]
    rows = mutate(rows)
    out = tmp_path / "filled.csv"
    with out.open("w", newline="", encoding=encoding) as fh:
        w = csv.DictWriter(fh, fieldnames=header or list(REVIEW_COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow({(header[REVIEW_COLUMNS.index(k)] if header else k): v
                        for k, v in r.items()})
    return out


def test_compare_labels_counts_distinct_ids_and_reports_duplicates(tmp_path, capsys):
    from touchstone.cli import main

    def dup(rows):
        first = dict(rows[0])
        conflict = dict(rows[1])
        conflict["label"] = ("formula_drift" if conflict["label"] != "formula_drift"
                             else "grain_drift")
        rows[2]["label"] = ""                               # never reviewed
        return rows + [first] * 5 + [conflict]
    assert main(["assess", "compare-labels", str(_sheet(tmp_path, dup))]) == 1
    out = capsys.readouterr().out
    assert "58/60 item(s) labeled" in out                   # distinct, conflicts unscored
    assert "labeled more than once" in out and "with different labels" in out
    assert "not reviewed" in out


def test_compare_labels_reads_excel_sheets_and_refuses_unreadable_ones(tmp_path, capsys):
    from touchstone.assess.runner import REVIEW_COLUMNS
    from touchstone.cli import main

    assert main(["assess", "compare-labels",
                 str(_sheet(tmp_path, lambda r: r, encoding="utf-8-sig"))]) == 0
    assert "60/60 item(s) labeled · agreed 60" in capsys.readouterr().out
    upper = [c.capitalize() for c in REVIEW_COLUMNS]
    assert main(["assess", "compare-labels",
                 str(_sheet(tmp_path, lambda r: r, header=upper))]) == 0   # headers: no case
    blank = _sheet(tmp_path, lambda rows: [{**r, "label": ""} for r in rows])
    assert main(["assess", "compare-labels", str(blank)]) == 1


# ------------------------------------------- evidence (round-18 review)

def test_the_stated_facts_reach_the_prompt_and_match_the_readme(dataset):
    from touchstone.assess.runner import DATASET_DIR

    readme = " ".join((DATASET_DIR / "README.md").read_text().replace("**", "").split())
    by_id = {i["id"]: i for _, i in items(dataset)}
    prompt = render_prompt(build_record(dataset, by_id["slices-07"]), load(1))
    for fact in dataset["definitions"]["stated_facts"]:
        assert fact in prompt
        assert " ".join(fact.split()) in readme


def test_relations_read_through_the_session_have_their_schemas(dataset):
    by_id = {i["id"]: i for _, i in items(dataset)}
    temp = build_record(dataset, by_id["temp-01"])
    # The temp view `o` reads what its body read; it is not a catalog relation.
    assert list(temp["schemas"]) == ["main.finance.orders"]
    view = build_record(dataset, by_id["renamed-05"])
    assert "main.finance.orders" in view["schemas"]           # the metric view's source


@pytest.mark.parametrize("item_id, reads, context", [
    # USE CATALOG dev: `finance.orders` is dev's; the schema is not evidenced.
    ("temp-03", ["dev.finance.orders"], {"catalog": "dev", "schema": None}),
    ("temp-05", ["main.finance.orders"], {"catalog": "main", "schema": "finance"}),
    # The temp view named orders shadows the table and reads main.finance.orders.
    ("temp-06", ["main.finance.orders"], {"catalog": "main", "schema": "finance"}),
    ("temp-04", ["orders"], None),                     # nothing fixes its catalog
])
def test_the_session_resolves_what_the_statement_reads(dataset, item_id, reads, context):
    from touchstone.assess.runner import session_reads

    by_id = {i["id"]: i for _, i in items(dataset)}
    record = build_record(dataset, by_id[item_id])
    assert list(record["schemas"]) == reads
    assert record["session_evidence"]["context"] == context
    assert session_reads(dataset, by_id[item_id]) == (reads, context)
    prompt = render_prompt(record, load(1))
    assert "not in the catalog" not in prompt or item_id == "temp-04"
    if item_id == "temp-04":
        assert "orders: not resolved to a catalog.schema.table name" in prompt


def test_use_drop_and_alter_read_nothing(dataset):
    from touchstone.assess.runner import session_reads

    item = {"id": "x", "metric": "net_revenue",
            "session": ["SET spark.sql.ansi.enabled = false",
                        "CREATE TEMP VIEW o AS SELECT * FROM main.finance.orders",
                        "ALTER VIEW o RENAME TO p", "USE CATALOG main", "USE SCHEMA finance",
                        "DROP VIEW IF EXISTS q"],
            "sql": "SELECT SUM(amount) AS net_revenue FROM p JOIN customers c USING (k)"}
    assert session_reads(dataset, item) == (
        ["main.finance.customers", "main.finance.orders"],
        {"catalog": "main", "schema": "finance"})


def test_prompts_carry_no_identity_that_decides_nothing(dataset):
    by_id = {i["id"]: i for _, i in items(dataset)}
    prompt = render_prompt(build_record(dataset, by_id["udf-03"]), load(1))
    assert "udf-03" not in prompt and '"benchmark"' not in prompt
    assert '"session_id"' not in prompt and '"user"' not in prompt


@pytest.mark.parametrize("applies, expect", [
    ({"from": None, "to": "2026-08-01T00:00:00Z"}, "2026-07-31T12:00:00Z"),
    ({"from": "2026-09-01T00:00:00Z", "to": None}, "2026-09-01T12:00:00Z"),
    ({}, "2026-09-01T12:00:00Z"),
])
def test_the_execution_time_is_inside_any_window(applies, expect):
    from touchstone.assess.runner import execution_time

    assert execution_time({"definitions": {"applies": applies}}) == expect


def test_a_zone_less_window_is_refused_clearly(tmp_path):
    import shutil

    import yaml

    from touchstone.assess.runner import DATASET_DIR

    copy = tmp_path / "ds"
    shutil.copytree(DATASET_DIR, copy)
    d = yaml.safe_load((copy / "definitions.yaml").read_text())
    d["applies"] = {"from": "2026-09-01T18:00:00", "to": "2026-09-03T00:00:00Z"}
    (copy / "definitions.yaml").write_text(yaml.safe_dump(d))
    with pytest.raises(ValueError, match="no time zone"):
        load_dataset(copy)
    d["applies"] = {"from": None, "to": "2026-09-03T00:00:00Z"}
    (copy / "definitions.yaml").write_text(yaml.safe_dump(d))
    load_dataset(copy)                                   # a window with only an end is fine


def test_the_execution_time_is_inside_the_window_or_the_dataset_is_refused(tmp_path):
    import shutil

    import yaml

    from touchstone.assess.runner import DATASET_DIR, execution_time

    ds = {"definitions": {"applies": {"from": "2026-09-01T18:00:00Z", "to": None}}}
    assert execution_time(ds) == "2026-09-02T06:00:00Z"
    short = {"definitions": {"applies": {"from": "2026-09-01T00:00:00Z",
                                         "to": "2026-09-01T06:00:00Z"}}}
    assert execution_time(short) == "2026-09-01T03:00:00Z"
    copy = tmp_path / "ds"
    shutil.copytree(DATASET_DIR, copy)
    d = yaml.safe_load((copy / "definitions.yaml").read_text())
    d["applies"] = {"from": "2026-09-01T00:00:00Z", "to": "2026-09-01T00:00:00Z"}
    (copy / "definitions.yaml").write_text(yaml.safe_dump(d))
    with pytest.raises(ValueError, match="validity window"):
        load_dataset(copy)


def test_a_finding_without_a_concept_reaches_the_observations(dataset):
    from touchstone.assess.runner import observations_from_monitor

    obs = observations_from_monitor(dataset, "dev")
    assert obs["temp-01"]["finding_kind"] == "unverifiable"
    assert obs["temp-01"]["reasons"]
    for o in obs.values():
        assert o["finding_kind"] is not None or o["reasons"][0]["code"] == "no_reading"


# ------------------------------------------- reporting (round-18 reviews)

class _Broken:
    name, model, measures_performance = "broken", "m", True
    confidence_source = "self-reported by the model"

    def __init__(self, fail=lambda i: True):
        self.fail = fail

    def assess(self, records, criteria):
        from touchstone.assess.providers import Assessment

        return [Assessment(record_id=r["record_id"], provider="broken", model="m",
                           criteria_version=1, answers={}, label="insufficient_evidence",
                           confidence=0.97 if self.fail(i) else 0.5, latency_ms=10.0,
                           error="HTTP 500: boom" if self.fail(i) else None)
                for i, r in enumerate(records)]


def test_failed_calls_are_their_own_bucket_never_the_models_undecided(dataset):
    report = run_benchmark(_Broken(), dataset=dataset, split="test")
    overall = report["arms"]["without_observations"]["overall"]
    d = overall["drift"]
    assert d["failed"]["count"] == d["of"] and d["undecided"]["count"] == 0
    assert d["flagged"]["count"] + d["labeled_permitted"]["count"] + \
        d["undecided"]["count"] + d["failed"]["count"] == d["of"]
    assert overall["answered"] == 0 and overall["abstention"]["of"] == 0
    assert overall["agreement_with_labels"] is None
    assert overall["calibration"]["scored"] == 0 and overall["calibration"]["ece"] is None
    assert overall["errors"]["count"] == overall["n"] and "HTTP 500" in overall["errors"]["by_kind"]
    assert report["performance_measured"] is False and report["partial"] is True
    assert any("NO call answered" in n for n in report["notes"])


def test_a_partial_outage_is_partial_and_scored_only_on_answers(dataset):
    report = run_benchmark(_Broken(fail=lambda i: i % 2 == 0), dataset=dataset, split="test")
    overall = report["arms"]["without_observations"]["overall"]
    assert 0 < overall["answered"] < overall["n"]
    assert report["performance_measured"] is True and report["partial"] is True
    assert overall["calibration"]["of"] == overall["answered"]
    assert any("the run is partial" in n for n in report["notes"])


def test_the_cli_exits_nonzero_when_nothing_answered(capsys, monkeypatch):
    from touchstone import cli
    from touchstone.assess import providers

    monkeypatch.setattr(providers, "get_provider", lambda config: _Broken())
    assert cli.main(["assess", "benchmark", "--split", "test"]) == 1
    out = capsys.readouterr()
    assert "failed 15/15" in out.out and "nothing was measured" in out.err
    assert "errors: 30" in out.out


def test_a_refused_credential_exits_2_without_a_traceback(capsys, monkeypatch):
    from touchstone import cli
    from touchstone.assess import providers
    from touchstone.assess.providers.databricks import DatabricksAuthError

    class Refused(_Broken):
        def assess(self, records, criteria):
            raise DatabricksAuthError("POST /api/2.0/sql/statements: HTTP 401: refused")
    monkeypatch.setattr(providers, "get_provider", lambda config: Refused())
    assert cli.main(["assess", "benchmark", "--split", "test"]) == 2
    assert "HTTP 401" in capsys.readouterr().err


def test_every_report_carries_label_provenance_and_calibration_counts(dataset, capsys):
    from touchstone.cli import main

    report = run_benchmark(get_provider(ProviderConfig("fake")), dataset=dataset,
                           split="test")
    assert report["labels"]["authored_by"] == "model"
    assert report["labels"]["human_review"] == "none yet"
    assert "gold_classes" not in report and "label_classes" in report
    cal = report["arms"]["without_observations"]["overall"]["calibration"]
    assert {"scored", "of", "out_of_range"} <= set(cal)
    assert report["confidence_source"].startswith("synthetic")
    assert "not implemented yet" in report["baselines"]["plain_prompt_model"]
    main(["assess", "benchmark", "--split", "test"])
    out = capsys.readouterr().out
    assert "labels: authored by model" in out and "human review: none yet" in out
    assert "insufficient evidence (n=" in out and "cost: " in out and "latency: " in out


def test_unreported_latency_and_cost_are_not_zero(dataset):
    obs = {i["id"]: {"finding_kind": "suspected_difference"} for _, i in items(dataset, "dev")}
    report = run_benchmark(get_provider(ProviderConfig("fake")), dataset=dataset,
                           split="dev", observations=obs)
    alone = report["baselines"]["touchstone_observations_alone"]
    assert alone["latency_ms"] is None and alone["cost_usd"] is None
    assert set(alone["by_family"]) == set(report["families"])


def test_another_metrics_finding_is_not_this_uses_reading(dataset):
    """An item about net_revenue whose SQL computes gross_revenue: touchstone's
    finding is about gross_revenue, and must not stand as its reading of the
    net_revenue use (it made 'labeled permitted' out of another metric's
    experimental match)."""
    from touchstone.assess.runner import observations_from_monitor

    item = {"id": "probe-01", "metric": "net_revenue", "label": "identity_mismatch",
            "rationale": "computes gross revenue under another name",
            "sql": "SELECT SUM(amount) AS gross_revenue FROM main.finance.orders "
                   "WHERE status = 'completed'"}
    ds = {**dataset, "families": [{"family": "probe", "split": "test", "items": [item]}]}
    obs = observations_from_monitor(ds)["probe-01"]
    experimental = obs.get("experimental") or {}
    assert "gross_revenue" not in json.dumps(experimental)
    assert touchstone_alone(obs) != "likely_permitted"


def test_exported_session_notes_say_what_is_uncertain():
    from types import SimpleNamespace

    from touchstone.services.monitor import _how_related

    ex = SimpleNamespace(session_id="S")
    log = SimpleNamespace(row=0, session=None, ts=None)
    assert "earlier in this log" in _how_related(log, 0, ex, None, True)
    unidentified = SimpleNamespace(row=1, session=None, ts="2026-10-02T09:00:00Z")
    note = _how_related(unidentified, 2, ex, "2026-10-02T10:00:00Z", False)
    assert "its session is not recorded" in note and "whether it ran in the same" in note
    # The statement's session is the one missing: say that, not the event's.
    recorded = SimpleNamespace(row=1, session="S1", ts="2026-10-02T09:00:00Z")
    note = _how_related(recorded, 2, SimpleNamespace(session_id=None), "2026-10-02T10:00:00Z",
                        False)
    assert "this statement's session is not recorded" in note
    same = SimpleNamespace(row=1, session="S", ts="2026-10-02T10:00:00Z")
    assert "whether it ran before" in _how_related(same, 2, ex, "2026-10-02T10:00:00Z", False)


class _Odd(_Broken):
    """Fails every call that carries touchstone's observations; otherwise answers
    — with a label outside the vocabulary on the first record and an
    out-of-range confidence on the second."""

    def assess(self, records, criteria):
        from touchstone.assess.providers import Assessment

        out = []
        for i, r in enumerate(records):
            dead = "unverified_observations" in r
            out.append(Assessment(record_id=r["record_id"], provider="odd", model="m",
                                  criteria_version=1, answers={},
                                  label="maybe" if i == 0 else "likely_permitted",
                                  confidence=1.7 if i == 1 else 0.6, latency_ms=1.0,
                                  error="HTTP 503: down" if dead else None))
        return out


def test_each_arm_says_whether_it_measured_anything(dataset, capsys, monkeypatch):
    from touchstone import cli
    from touchstone.assess import providers

    obs = {i["id"]: {"finding_kind": None} for _, i in items(dataset, "test")}
    report = run_benchmark(_Odd(), dataset=dataset, split="test", observations=obs)
    without, with_ = report["arms"]["without_observations"], report["arms"]["with_observations"]
    assert without["performance_measured"] is True and with_["performance_measured"] is False
    assert report["performance_measured"] is False
    # A label outside the vocabulary is a failed call, so the parts still sum.
    overall = without["overall"]
    assert overall["errors"]["by_kind"] == {"invalid label": 1}
    for cls in ("drift", "permitted", "insufficient_evidence"):
        part = overall[cls]
        assert sum(v["count"] for k, v in part.items() if k != "of") == part["of"]
    monkeypatch.setattr(providers, "get_provider", lambda config: _Odd())
    assert cli.main(["assess", "benchmark", "--split", "test", "--with-observations"]) == 1
    out = capsys.readouterr().out
    assert "  without observations\n" in out                  # it answered: measured
    assert "with observations [not a measurement]" in out
    assert "1 confidence(s) outside [0, 1] not scored" in out


def test_compare_labels_survives_short_rows_and_names_conflicts(tmp_path, capsys):
    from touchstone.cli import main

    short = tmp_path / "short.csv"
    short.write_text("label,id\ngrain_drift\nformula_drift,bogus-1\npermitted_variation,time-01\n")
    assert main(["assess", "compare-labels", str(short)]) == 1
    out = capsys.readouterr().out
    assert "(a row without an id)" in out and "bogus-1" in out
    conflict = tmp_path / "conflict.csv"
    conflict.write_text("id,label\ntime-01,grain_drift\ntime-01,formula_drift\n")
    assert main(["assess", "compare-labels", str(conflict)]) == 1
    assert "every labeled row has one of the problems listed above" in capsys.readouterr().err


def test_a_report_json_that_cannot_be_written_is_an_error_not_a_traceback(tmp_path, capsys):
    from touchstone.cli import main

    assert main(["assess", "benchmark", "--split", "test", "--json", str(tmp_path)]) == 2
    assert "could not write the report JSON" in capsys.readouterr().err


def test_the_baseline_never_calls_an_experimental_match_confirmed(capsys):
    from touchstone.cli import main

    main(["assess", "benchmark", "--split", "test", "--with-observations"])
    out = capsys.readouterr().out
    baseline = out[out.index("baseline: touchstone observations alone"):]
    assert "labeled permitted (experimental matches) 9/12" in baseline
    assert "confirmed" not in baseline


def test_compare_labels_names_no_single_cause_for_an_unscorable_sheet(tmp_path, capsys):
    from touchstone.cli import main

    sheet = tmp_path / "unknown.csv"
    sheet.write_text("id,label\nslice-99,grain_drift\n")
    assert main(["assess", "compare-labels", str(sheet)]) == 1
    captured = capsys.readouterr()
    assert "unknown ids: slice-99" in captured.out
    assert "every labeled row has one of the problems listed above" in captured.err


@pytest.mark.parametrize("session, sql, reads, context", [
    # Names compare without case, as Databricks identifiers do.
    (["USE `Main`.`Finance`"], "FROM orders", ["main.finance.orders"],
     {"catalog": "main", "schema": "finance"}),
    (["USE CATALOG MAIN", "USE SCHEMA FINANCE"], "FROM orders", ["main.finance.orders"],
     {"catalog": "main", "schema": "finance"}),
    # SET CATALOG, SET, an unparseable statement: the monitor calls every
    # short name session-dependent, so nothing resolves it here either.
    (["USE CATALOG main", "USE SCHEMA finance", "SET CATALOG dev"], "FROM orders",
     ["orders"], None),
    (["USE main.finance", "SELECT * FROM ("], "FROM orders", ["orders"], None),
    # Inside its own body, a CTE's name is the outer relation (the temp view).
    (["CREATE TEMP VIEW o AS SELECT * FROM main.finance.customers"],
     "WITH o AS (SELECT * FROM o) SELECT SUM(amount) AS net_revenue FROM o",
     ["main.finance.customers"], None),
    # A CTE is no relation; inside it, the name it shadows is the outer one.
    (["CREATE TEMP VIEW c AS SELECT * FROM main.finance.customers"],
     "WITH c AS (SELECT * FROM main.finance.orders) SELECT SUM(amount) AS net_revenue FROM c",
     ["main.finance.orders"], None),
    (["USE main.finance"],
     "WITH c AS (SELECT * FROM orders) SELECT SUM(amount) AS net_revenue FROM c",
     ["main.finance.orders"], {"catalog": "main", "schema": "finance"}),
    (["USE CATALOG main", "CREATE GLOBAL TEMP VIEW o AS SELECT * FROM main.finance.orders"],
     "FROM global_temp.o", ["main.finance.orders"], {"catalog": "main", "schema": None}),
    # A persistent view is found by the name it resolved to when created.
    (["USE main.finance", "CREATE VIEW v AS SELECT * FROM orders WHERE amount > 0",
      "USE CATALOG dev"], "FROM v", ["v"], {"catalog": "dev", "schema": None}),
    (["CREATE TEMP VIEW o (a, b) AS SELECT amount, status FROM main.finance.orders"],
     "FROM o", ["main.finance.orders"], None),
    (["CACHE TABLE o AS SELECT * FROM main.finance.orders"], "FROM o",
     ["main.finance.orders"], None),
])
def test_session_reads_follows_the_engine_and_the_monitor(dataset, session, sql, reads, context):
    from touchstone.assess.runner import session_reads

    item = {"id": "x", "metric": "net_revenue", "session": session,
            "sql": sql if sql.startswith("WITH") else f"SELECT SUM(amount) AS net_revenue {sql}"}
    assert session_reads(dataset, item) == (reads, context)


# ------------------------------------------- round-18 verification, second pass

def test_the_governed_view_and_the_permitted_filters_reach_the_prompt(dataset):
    by_id = {i["id"]: i for _, i in items(dataset)}
    prompt = render_prompt(build_record(dataset, by_id["renamed-05"]), load(1))
    assert "main.finance.revenue_mv: the governed metric view of the definition above" in prompt
    assert "not in the catalog" not in prompt
    assert "permitted filter dimensions: " in prompt


def test_offset_windows_are_placed_in_utc():
    from touchstone.assess.runner import execution_time

    assert execution_time({"definitions": {"applies": {"from": "2026-09-01T00:00:00+02:00"}}}) \
        == "2026-09-01T10:00:00Z"
    assert execution_time({"definitions": {"applies": {
        "from": None, "to": "2026-10-01T00:00:00+14:00"}}}) == "2026-09-29T22:00:00Z"


def test_a_window_with_only_an_end_runs_with_observations_and_unquoted_instants_load(tmp_path):
    import shutil

    from touchstone.assess.runner import DATASET_DIR, observations_from_monitor

    copy = tmp_path / "ds"
    shutil.copytree(DATASET_DIR, copy)
    text = (copy / "definitions.yaml").read_text()
    import re
    text = re.sub(r"applies:\n(\s+)from: .*\n(\s+)to: .*\n",
                  "applies:\n\\1from: null\n\\2to: 2026-12-01T00:00:00Z\n", text)
    (copy / "definitions.yaml").write_text(text)
    ds = load_dataset(copy)
    assert ds["definitions"]["applies"]["to"] == "2026-12-01T00:00:00Z"   # text, not datetime
    obs = observations_from_monitor(ds, "dev")
    assert obs and all("finding_kind" in o for o in obs.values())


def test_a_use_read_as_another_metric_says_so(dataset):
    from touchstone.assess.runner import observations_from_monitor

    item = {"id": "probe-02", "metric": "net_revenue", "label": "identity_mismatch",
            "rationale": "computes gross revenue",
            "sql": "SELECT SUM(amount) AS gross_revenue FROM main.finance.orders "
                   "WHERE status = 'completed'"}
    ds = {**dataset, "families": [{"family": "probe", "split": "test", "items": [item]}]}
    (reason,) = observations_from_monitor(ds)["probe-02"]["reasons"]
    assert reason["code"] == "other_metric" and "gross_revenue" in reason["detail"]


def test_exported_session_events_are_bounded_on_a_session_less_history():
    """A session-less history put every earlier SET beside every query: each
    record grew with the whole history. Only what can change the statement's
    names is exported from other executions, at most 20, with a count."""
    from types import SimpleNamespace

    import sqlglot

    from touchstone.services.monitor import _run_record
    from touchstone.services.session import SessionEvent

    events = [SessionEvent(row=i, index=0, session=None, ts=f"2026-10-02T10:{i:02d}:00Z",
                           what="SET", names=None, text=f"SET x.y = {i}") for i in range(60)]
    ex = SimpleNamespace(statement_id="q", start_time="2026-10-02T11:00:00Z", session_id=None,
                         executed_by="ana", client_application=None, asset=None)
    qualified = sqlglot.parse_one("SELECT SUM(amount) FROM main.finance.orders",
                                  read="databricks")
    record = _run_record(ex, qualified, "databricks", events, 99, 0, None, None,
                         "2026-10-02T11:00:00Z", False, 3)
    assert record["session_evidence"]["events"] == []      # a SET cannot change a full name
    short = sqlglot.parse_one("SELECT SUM(amount) FROM orders", read="databricks")
    listed = _run_record(ex, short, "databricks", events, 99, 0, None, None,
                         "2026-10-02T11:00:00Z", False, 3)["session_evidence"]["events"]
    # Sixty SETs of one key are one setting: its last value (not exported:
    # x.y is not a known evaluation setting).
    assert len(listed) == 1 and listed[0].startswith("SET x.y = [value not exported]")
    # Past the cap, what decides a name's relation outlives configuration.
    use = SessionEvent(row=0, index=0, session=None, ts="2026-10-02T09:00:00Z", what="USE",
                       names=None, use=("dev", None, False), text="USE CATALOG dev")
    noise = [SessionEvent(row=i + 1, index=0, session=None, ts=f"2026-10-02T10:{i:02d}:00Z",
                          what="SET", names=None, text=f"SET spark.sql.k{i} = {i}")
             for i in range(30)]
    capped = _run_record(ex, short, "databricks", [use, *noise], 99, 0, None, None,
                         "2026-10-02T11:00:00Z", False, 3)["session_evidence"]["events"]
    assert len(capped) == 21 and capped[0].startswith("(11 earlier statement(s)")
    assert any(e.startswith("USE CATALOG dev") for e in capped)



def test_cte_items_list_only_the_tables_they_read(dataset):
    for fam, item in items(dataset):
        if fam == "staging_ctes":
            record = build_record(dataset, item)
            assert all(len(r.split(".")) == 3 for r in record["schemas"]), (item["id"],
                                                                          record["schemas"])


def test_session_statements_never_export_credentials(tmp_path):
    from types import SimpleNamespace

    import sqlglot

    from touchstone.services.monitor import _run_record
    from touchstone.services.session import SessionEvent

    secret = "FAKEKEY0000notarealsecret0000"
    texts = [f"SET fs.azure.account.key.acct.dfs.core.windows.net = {secret}",
             f"SET spark.hadoop.fs.s3a.secret.key = '{secret}'",
             f"CREATE TEMPORARY VIEW jdbc_v USING JDBC OPTIONS (url = 'jdbc:x', user = 'svc', "
             f"password = '{secret}')",
             f"CREATE USER bob IDENTIFIED BY '{secret}'",
             "SET spark.sql.ansi.enabled = false"]
    events = [SessionEvent(row=0, index=i, session=None, ts=None, what="SET", names=None,
                           text=t) for i, t in enumerate(texts)]
    ex = SimpleNamespace(statement_id="q", start_time=None, session_id=None, executed_by=None,
                         client_application=None, asset=None)
    stmt = sqlglot.parse_one("SELECT SUM(amount) FROM orders", read="databricks")
    listed = _run_record(ex, stmt, "databricks", events, 0, 9, None, None, None, False,
                         3)["session_evidence"]["events"]
    assert len(listed) == 5 and not any(secret in e for e in listed)
    assert "SET spark.sql.ansi.enabled = false" in listed     # evaluation settings stay
    assert "CREATE TEMPORARY VIEW jdbc_v USING JDBC OPTIONS (url = '[not exported]'" in listed[2]


def test_the_baseline_json_never_calls_an_experimental_match_confirmed():
    from touchstone.assess.runner import observations_from_monitor

    ds = load_dataset()
    report = run_benchmark(get_provider(ProviderConfig("fake")), dataset=ds, split="test",
                           observations=observations_from_monitor(ds, "test"))
    alone = report["baselines"]["touchstone_observations_alone"]
    assert "confirmed" not in alone["permitted"]
    assert alone["permitted"]["labeled_permitted"]["count"] == 9
    assert "experimental" in alone["positive_readings"]
    assert all("confirmed" not in fam["permitted"] for fam in alone["by_family"].values())
    # The model's own arms keep their name: a model's answer is its answer.
    assert "confirmed" in report["arms"]["without_observations"]["overall"]["permitted"]


def test_the_benchmark_dataset_refuses_a_key_written_twice(tmp_path):
    import shutil

    from touchstone.assess.runner import DATASET_DIR, load_dataset

    copy = tmp_path / "ds"
    shutil.copytree(DATASET_DIR, copy)
    family = copy / "dimension_joins.yaml"
    text = family.read_text()
    first = text.index("    label: ")
    end = text.index("\n", first)
    family.write_text(text[:end + 1] + "    label: permitted_variation\n" + text[end + 1:])
    with pytest.raises(ValueError, match="appears twice"):
        load_dataset(copy)
