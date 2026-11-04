"""touchstone CLI.

    touchstone scan (--dbt <dir> | --sql-dir <dir> | --views <dump.sql>)
                  [--metrics m.yml] [--agents-schema export.json|agents.duckdb]
                  [--osi osi/|osi_document.json] [--uc-metrics views/]
                  [--catalog c.yml]
                  [--execute seed.sql]
                  [--db touchstone.db] [--report out.html] [--json out.json]
                  [--dialect duckdb] [--label "..."] [--fail-on contradiction]
    touchstone publish   --db touchstone.db --warehouse agents.duckdb
    touchstone diff      --db touchstone.db [--markdown pr.md] [--fail-on-new contradiction]
    touchstone concepts  --db touchstone.db
    touchstone history   --db touchstone.db <concept>
    touchstone asof      --db touchstone.db <concept> --at 2026-03-01T00:00:00Z
    touchstone attest    --db touchstone.db <concept> [--as-of TS]
                       [--dbt <dir> | --sql-dir <dir> | --views <dump.sql>]
                       [--metrics m.yml] [--agents-schema ...] [--osi ...]   # freshness check
    touchstone bench     [--dialects duckdb,databricks,snowflake,bigquery]
    touchstone oracle    [--full]          # execute every pair the experimental rules equate
    touchstone catalog   --warehouse duckdb:///wh.db --out catalog.yml
    touchstone propose   --db touchstone.db --out proposals/ [--format metrics|ossie|uc-metrics]
    touchstone monitor   --db touchstone.db --history-file history.sql
                       [--dbt <dir>] [--catalog c.yml] [--min-runs 2]
                       [--fail-on-alt]        # alt definitions in the query stream
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from touchstone import __version__
from touchstone.engine import run_scan
from touchstone.models import VerdictType
from touchstone.report.html import render_report, report_json
from touchstone.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="touchstone", description=__doc__)
    parser.add_argument("--version", action="version", version=f"touchstone {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="scan an estate and reconcile it against declared semantics")
    p_scan.add_argument("--dbt", help="dbt project dir (contains [target/]manifest.json)")
    p_scan.add_argument("--sql-dir", dest="sql_dir",
                        help="directory of .sql files (no dbt required)")
    p_scan.add_argument("--views", dest="views_path",
                        help="file of CREATE VIEW statements (information_schema dump)")
    p_scan.add_argument("--metrics", help="declared metrics YAML")
    p_scan.add_argument(
        "--agents-schema",
        dest="agents_schema_path",
        help="AGENTS.* claims source: a .json export or a DuckDB file with the "
             "AGENTS schema (OSI metrics, LookML measures)",
    )
    p_scan.add_argument(
        "--osi", dest="osi_path",
        help="Apache Ossie documents: an osi/ directory, one document, or "
             "dbt's target/osi_document.json")
    p_scan.add_argument(
        "--uc-metrics", dest="uc_metrics_path",
        help="Unity Catalog metric view YAML (one file or a directory)")
    p_scan.add_argument("--catalog", help="physical catalog YAML (schema.table.column: type)")
    p_scan.add_argument(
        "--default-database", dest="default_database",
        help="the session's default database: fills an omitted leading "
             "qualifier. Without it, a reference shorter than the estate's "
             "names is never assumed to mean any particular table")
    p_scan.add_argument(
        "--default-schema", dest="default_schema",
        help="the session's default schema for one-part table names")

    p_scan.add_argument("--db", default="touchstone.db")
    p_scan.add_argument("--dialect", default="duckdb")
    p_scan.add_argument("--label")
    p_scan.add_argument("--report", help="write HTML report here")
    p_scan.add_argument("--json", dest="json_out", help="write JSON verdicts here")
    p_scan.add_argument(
        "--fail-on",
        choices=["contradiction", "any"],
        help="non-zero exit if such verdicts exist (CI wedge). Also fails when "
             "the scan read no SQL at all or produced no measure, since a gate "
             "that passes on an unread estate is not a gate",
    )
    p_scan.add_argument(
        "--min-severity", type=int, metavar="N",
        help="only PRINT verdicts at severity >= N (all are still stored, "
             "reported and written to --json)",
    )
    p_scan.add_argument(
        "--top", type=int, metavar="N",
        help="only PRINT the N most severe verdicts",
    )
    p_scan.add_argument(
        "--min-coverage",
        type=float,
        metavar="PCT",
        help="non-zero exit if proof-grade canonicalization coverage is below "
             "PCT — the estate was read but mostly not understood",
    )
    p_scan.add_argument(
        "--execute",
        dest="execute_seed",
        help="SQL seed script: materialize the estate in DuckDB and attach "
             "measured divergence to contradictions",
    )

    p_diff = sub.add_parser(
        "diff",
        help="newly introduced / resolved verdicts vs a baseline scan (the CI comment)",
    )
    p_diff.add_argument("--db", default="touchstone.db")
    p_diff.add_argument("--scan", help="scan id to evaluate (default: most recent)")
    p_diff.add_argument("--baseline", help="baseline scan id (default: previous scan)")
    p_diff.add_argument("--markdown", help="write a PR-ready markdown comment here")
    p_diff.add_argument(
        "--fail-on-new",
        choices=["contradiction", "any"],
        help="non-zero exit only for *newly introduced* verdicts",
    )

    p_concepts = sub.add_parser("concepts", help="list concepts and verdicts from the latest scan")
    p_concepts.add_argument("--db", default="touchstone.db")

    p_history = sub.add_parser("history", help="derived valid-time history of a concept")
    p_history.add_argument("concept")
    p_history.add_argument("--db", default="touchstone.db")

    p_asof = sub.add_parser("asof", help="what did this concept mean at a point in time")
    p_asof.add_argument("concept")
    p_asof.add_argument("--at", required=True, help="ISO timestamp, e.g. 2026-03-01T00:00:00Z")
    p_asof.add_argument("--db", default="touchstone.db")

    p_attest = sub.add_parser("attest", help="issue a signed semantic receipt for a concept")
    p_attest.add_argument("concept")
    p_attest.add_argument("--as-of", dest="as_of")
    p_attest.add_argument("--db", default="touchstone.db")
    # Estate sources for the freshness check: same flags as scan. When given,
    # the estate is re-hashed and the receipt says whether the verdicts still
    # describe what is deployed (fresh/partial/stale); omitted → unknown.
    _add_estate_args(p_attest)

    p_verify = sub.add_parser(
        "verify-receipt",
        help="verify a receipt's Ed25519 signature offline (no store needed); "
             "with --db and the estate sources, also re-check the estate NOW "
             "against the receipt's scan — the only way to establish current use",
    )
    p_verify.add_argument("receipt", help="path to a receipt JSON file, or - for stdin")
    p_verify.add_argument(
        "--require-current", dest="require_current", action="store_true",
        help="exit 1 unless the receipt is valid for CURRENT use: a live re-check "
             "(--db plus estate sources) must find the estate unchanged since the "
             "receipt's scan")
    p_verify.add_argument("--db", default="touchstone.db",
                          help="store holding the receipt's scan (for the live re-check)")
    _add_estate_args(p_verify)
    p_verify.add_argument(
        "--public-key",
        help="pin the expected signer (base64url Ed25519); omit to check "
             "tamper-evidence against the embedded key",
    )

    p_pubkey = sub.add_parser("pubkey", help="print this store's receipt-signing public key")
    p_pubkey.add_argument("--db", default="touchstone.db")

    p_mcp = sub.add_parser(
        "mcp",
        help="serve attest/asof/history/concepts as MCP tools over stdio "
             "(agents check receipts in the answer path)",
    )
    p_mcp.add_argument("--db", default="touchstone.db")
    # Optional estate sources: when configured, every attest re-checks
    # freshness against the live estate before answering.
    _add_estate_args(p_mcp)

    p_bench = sub.add_parser(
        "bench",
        help="development benchmark of the experimental equality rules across a "
             "SQL-dialect matrix: detection rate, known gaps, and the zero-false-"
             "equality gate",
    )
    p_bench.add_argument("--corpus", help="corpus YAML (default: shipped corpus)")
    p_bench.add_argument(
        "--dialects",
        help="comma-separated dialect matrix (default: corpus setting, else "
             "duckdb,databricks,snowflake,bigquery)",
    )
    p_bench.add_argument("--json", dest="json_out", help="write full results JSON here")

    p_publish = sub.add_parser(
        "publish",
        help="write signed receipts, their policy assessments and the verdicts "
             "projection into a warehouse (AGENTS.ROOT + touchstone.verdicts)",
    )
    p_publish.add_argument("--db", default="touchstone.db")
    p_publish.add_argument(
        "--refresh-policy", dest="refresh_policy", action="store_true",
        help="do not issue anything: bring rows published EARLIER under the policy "
             "in force (verdicts mapped, usable_for_reliance = false; receipts left "
             "unchanged with an assessment row beside each)")
    # Estate sources: when given, every published receipt embeds a LIVE
    # freshness check instead of `unchecked`.
    _add_estate_args(p_publish)
    p_publish.add_argument("--warehouse", required=True,
                           help="DuckDB file to publish AGENTS.ROOT rows into")

    p_mine = sub.add_parser(
        "mine-relationships",
        help="mine join edges from executed SQL (BI query history) and record "
             "them as candidate relationships — used is not valid",
    )
    p_mine.add_argument("--history-file", dest="history_file",
                        help="file of executed SQL statements")
    p_mine.add_argument("--warehouse", dest="warehouse_url",
                        help="warehouse URL to pull history from (with --history-query)")
    p_mine.add_argument("--history-query", dest="history_query",
                        help="query returning executed SQL text in the first column")
    p_mine.add_argument("--catalog", help="catalog YAML (qualification + declared keys)")
    p_mine.add_argument("--db", default="touchstone.db")
    p_mine.add_argument("--dialect", default="duckdb")

    p_verify_rel = sub.add_parser(
        "verify-relationships",
        help="run cardinality checks (uniqueness, orphans) for known edges "
             "against a warehouse — the step that upgrades used to valid",
    )
    p_verify_rel.add_argument("--warehouse", dest="warehouse_url",
                              help="warehouse URL (e.g. path.duckdb)")
    p_verify_rel.add_argument("--seed", dest="seed_path",
                              help="build an ephemeral DuckDB from this seed instead")
    p_verify_rel.add_argument("--dbt", help="estate to materialize as views over the seed")
    p_verify_rel.add_argument("--top", type=int, help="check only the N most-used edges")
    p_verify_rel.add_argument("--catalog", help="catalog YAML — declared primary keys "
                                                "are checked against reality too")
    p_verify_rel.add_argument("--db", default="touchstone.db")

    p_rel = sub.add_parser("relationships",
                           help="the relationship graph: checks passed on observed "
                                "data / failed / unverified, by usage (declared+used / "
                                "shadow / orphan)")
    p_rel.add_argument("--table", help="only edges touching this relation")
    p_rel.add_argument("--json", dest="json_out", help="write full graph JSON here")
    p_rel.add_argument("--db", default="touchstone.db")

    p_oracle = sub.add_parser(
        "oracle",
        help="development check: execute every query pair the experimental rules "
             "equate against adversarial DuckDB datasets; exits 1 on any disagreement")
    p_oracle.add_argument("--full", action="store_true",
                          help="full cross product (~15k queries, ~40s) instead "
                               "of the one-dimension-at-a-time default")

    p_cat = sub.add_parser(
        "catalog",
        help="generate catalog YAML from a warehouse's information_schema")
    p_cat.add_argument("--warehouse", dest="warehouse_url", required=True,
                       help="warehouse URL (duckdb:///path.db)")
    p_cat.add_argument("--out", dest="out_path", required=True,
                       help="catalog YAML to write")

    p_prop = sub.add_parser(
        "propose",
        help="draft resolutions for contradictions and declarations for "
             "shadow metrics — a PR body plus declaration files, nothing applied")
    p_prop.add_argument("--out", dest="out_dir", required=True,
                        help="directory to write RESOLUTIONS.md + declarations/")
    p_prop.add_argument("--format", dest="fmt", default="metrics",
                        choices=["metrics", "ossie", "uc-metrics"],
                        help="declaration format to emit (default: metrics YAML)")
    p_prop.add_argument("--concept", help="only this concept")
    p_prop.add_argument("--dbt", help="dbt project dir — rebuilds the scan's registry "
                                      "so emitted declarations are round-trip checked")
    p_prop.add_argument("--sql-dir", dest="sql_dir", help="directory of .sql files")
    p_prop.add_argument("--views", dest="views_path", help="file of CREATE VIEW statements")
    p_prop.add_argument("--catalog", help="catalog YAML (column resolution)")
    p_prop.add_argument(
        "--default-database", dest="default_database",
        help="the session's default database: fills an omitted leading "
             "qualifier. Without it, a reference shorter than the estate's "
             "names is never assumed to mean any particular table")
    p_prop.add_argument(
        "--default-schema", dest="default_schema",
        help="the session's default schema for one-part table names")

    p_prop.add_argument("--db", default="touchstone.db")

    p_mon = sub.add_parser(
        "monitor",
        help="compare executed SQL with authoritative metric definitions: suspected "
             "differences, candidates, and what could not be judged (matches are "
             "experimental comparisons while positive verdicts are suspended)")
    p_mon.add_argument("--pilot", dest="pilot_dir",
                       help="pilot package directory (pilot.yml): approved metric "
                            "views with definition snapshots and windows, the "
                            "history export, and any asserted session context")
    p_mon.add_argument("--history-file", dest="history_file",
                       help="executed SQL: .jsonl / .csv (one execution per row, "
                            "with statement_id, start_time, executed_by, catalog, "
                            "schema, …) or .sql (one statement per execution)")
    p_mon.add_argument("--warehouse", dest="warehouse_url",
                       help="warehouse URL to pull history from")
    p_mon.add_argument("--history-query", dest="history_query",
                       help="query returning one execution per row (columns named "
                            "as in --history-file; else the first column is the SQL)")
    p_mon.add_argument("--authority", dest="authority",
                       help="comma-separated systems whose definitions are "
                            "authoritative (e.g. uc_metrics,dbt_metrics; a computed "
                            "system such as dbt or sql only when named). Default: "
                            "every declared system the scan read")
    p_mon.add_argument("--asset-map", dest="asset_map",
                       help="YAML mapping of dashboard:<id> / genie:<id> to the "
                            "metric a single-measure query from that asset computes")
    p_mon.add_argument("--assert-context-for-all-executions", dest="assert_context",
                       action="store_true",
                       help="state that --default-database/--default-schema (or "
                            "the scan's context) was the session context of EVERY "
                            "exported execution. Without it, a history row's short "
                            "table names resolve only from its own catalog/schema")
    p_mon.add_argument("--dbt", help="dbt project dir — rebuilds the scan's "
                                     "registry so observed columns resolve "
                                     "to the same terminals")
    p_mon.add_argument("--sql-dir", dest="sql_dir", help="directory of .sql files")
    p_mon.add_argument("--views", dest="views_path",
                       help="file of CREATE VIEW statements")
    p_mon.add_argument("--catalog", help="catalog YAML (column resolution)")
    p_mon.add_argument(
        "--default-database", dest="default_database",
        help="the estate's default database (as for scan): resolves the "
             "estate's models. Applies to history rows only with "
             "--assert-context-for-all-executions")
    p_mon.add_argument(
        "--default-schema", dest="default_schema",
        help="the estate's default schema for one-part table names (history "
             "rows: only with --assert-context-for-all-executions)")

    p_mon.add_argument("--dialect", help="history dialect (default: the scan's)")
    p_mon.add_argument("--min-runs", dest="min_runs", type=int, default=2,
                       help="occurrences before a recurring measure counts as "
                            "a definition in use (default 2)")
    p_mon.add_argument("--json", dest="json_out", help="write full findings JSON here")
    p_mon.add_argument("--export-comparisons", dest="export_comparisons",
                       help="write one touchstone-comparison/1 evidence record per metric use "
                            "here (JSON lines) for the advisory classifier: the statement "
                            "and the definition first, touchstone's reading as unverified "
                            "observations")
    p_mon.add_argument("--fail-on-alt", dest="fail_on_alt", action="store_true",
                       help="exit 1 when a suspected difference from an authoritative "
                            "definition is found (a triage gate: the difference is "
                            "suspected, not verified; candidates and unverifiable "
                            "uses never fail it)")
    p_mon.add_argument("--db", default="touchstone.db")

    p_assess = sub.add_parser(
        "assess",
        help="development: evaluate an ADVISORY drift classifier against labeled metric "
             "uses (model-authored and model-reviewed; never changes a finding or an exit "
             "code)")
    assess_sub = p_assess.add_subparsers(dest="assess_command", required=True)
    p_ab = assess_sub.add_parser(
        "benchmark",
        help="score a provider on the metric-use dataset: drift flagged / labeled "
             "permitted / undecided, permitted uses confirmed / flagged / undecided, "
             "calibration, cost and latency (the fake provider measures nothing — the "
             "report says so)")
    def provider_args(p) -> None:
        p.add_argument("--provider", default="fake",
                       choices=["fake", "api", "databricks", "snowflake"],
                       help="assessment backend: fake (measures nothing) or databricks "
                            "(ai_query; the token is read from DATABRICKS_TOKEN, or from a "
                            "`databricks auth login` profile)")
        p.add_argument("--model", help="databricks: the serving endpoint ai_query calls, "
                                       "e.g. system.ai.<model>")
        p.add_argument("--endpoint", help="databricks: the SQL warehouse id (or "
                                          "DATABRICKS_WAREHOUSE_ID) — never a secret")
        p.add_argument("--host", help="databricks: the workspace URL (or DATABRICKS_HOST, or "
                                      "the profile's host)")
        p.add_argument("--profile", help="databricks: a `databricks auth login` profile to "
                                         "take the host and a short-lived OAuth token from "
                                         "when DATABRICKS_TOKEN is unset (or "
                                         "DATABRICKS_CONFIG_PROFILE)")
        p.add_argument("--batch-size", dest="batch_size", type=int, default=1,
                       help="databricks: records per ai_query statement (default 1)")
        p.add_argument("--max-tokens", dest="max_tokens", type=int,
                       help="databricks: max_tokens per answer, reasoning included "
                            "(default 8192)")
        p.add_argument("--temperature", type=float,
                       help="databricks: sampling temperature (default: not sent — "
                            "reasoning endpoints reject it)")
        p.add_argument("--seed", default="", help="seed for the fake provider")

    provider_args(p_ab)
    p_ab.add_argument("--split", default="test", choices=["dev", "test", "all"],
                      help="split by query family; tune on dev, report test")
    p_ab.add_argument("--with-observations", dest="with_observations", action="store_true",
                      help="run the monitor over the dataset and add touchstone's unverified "
                           "observations: enables the ablation and the touchstone-alone "
                           "baseline")
    p_ab.add_argument("--dataset", help="dataset directory (default: the shipped one)")
    p_ab.add_argument("--json", dest="json_out", help="write the full report JSON here")
    p_ar = assess_sub.add_parser(
        "run",
        help="advisory verdicts for exported comparison records (monitor "
             "--export-comparisons): one call per distinct judgment, cached by judgment, "
             "prompt, model and criteria version; never a finding or an exit code")
    provider_args(p_ar)
    p_ar.add_argument("--comparisons", required=True,
                      help="the JSON-lines file monitor --export-comparisons wrote")
    p_ar.add_argument("--out", required=True, help="write one verdict per record here "
                                                   "(JSON lines)")
    p_ar.add_argument("--cache", help="verdict cache (JSON lines): answered judgments are "
                                      "reused, new ones appended")
    p_ar.add_argument("--without-observations", dest="without_observations",
                      action="store_true",
                      help="send the records without touchstone's unverified observations")
    p_rs = assess_sub.add_parser(
        "review-sheet",
        help="write a BLIND review sheet (CSV, no labels or rationales) for a human labeler, "
             "with the policy and definitions beside it")
    p_rs.add_argument("--out", required=True,
                      help="CSV file to write (the guide is written beside it as "
                           "<name>.guide.md)")
    p_rs.add_argument("--force", action="store_true",
                      help="overwrite an existing sheet or guide")
    p_rs.add_argument("--dataset", help="dataset directory (default: the shipped one)")
    p_cl = assess_sub.add_parser(
        "compare-labels",
        help="compare a filled review sheet with the dataset's labels: agreement and every "
             "disagreement (changes nothing)")
    p_cl.add_argument("sheet", help="the filled CSV")
    p_cl.add_argument("--dataset", help="dataset directory (default: the shipped one)")

    args = parser.parse_args(argv)
    handler = {
        "scan": _cmd_scan,
        "diff": _cmd_diff,
        "concepts": _cmd_concepts,
        "history": _cmd_history,
        "asof": _cmd_asof,
        "attest": _cmd_attest,
        "verify-receipt": _cmd_verify_receipt,
        "pubkey": _cmd_pubkey,
        "mcp": _cmd_mcp,
        "bench": _cmd_bench,
        "publish": _cmd_publish,
        "mine-relationships": _cmd_mine_relationships,
        "verify-relationships": _cmd_verify_relationships,
        "relationships": _cmd_relationships,
        "monitor": _cmd_monitor,
        "catalog": _cmd_catalog,
        "oracle": _cmd_oracle,
        "propose": _cmd_propose,
        "assess": _cmd_assess,
    }[args.command]
    try:
        import duckdb
        duckdb_errors: tuple = (duckdb.Error,)
    except ImportError:  # pragma: no cover — duckdb is optional
        duckdb_errors = ()
    try:
        return handler(args)
    except (ValueError, FileNotFoundError) as e:
        # Intake problems the adapters have already described precisely — a
        # missing manifest, a truncated JSON file, a YAML of the wrong shape,
        # an unsupported warehouse URL. They arrived as raw tracebacks, which
        # is the least actionable way to tell an operator their path is wrong.
        print(f"touchstone: {e}", file=sys.stderr)
        return 2
    except duckdb_errors as e:
        # A nonexistent, locked, or non-DuckDB warehouse file, or the SQLite
        # store passed as a warehouse — the driver's own message is precise;
        # a traceback around it is not.
        print(f"touchstone: warehouse error — {e}", file=sys.stderr)
        return 2


def _add_estate_args(parser: argparse.ArgumentParser) -> None:
    """Estate-source flags shared by attest/mcp for the freshness check —
    deliberately identical to scan's source flags."""
    parser.add_argument("--dbt", help="dbt project dir to re-check freshness against")
    parser.add_argument("--sql-dir", dest="sql_dir", help="directory of .sql files")
    parser.add_argument("--views", dest="views_path", help="file of CREATE VIEW statements")
    parser.add_argument("--metrics", help="declared metrics YAML")
    parser.add_argument("--agents-schema", dest="agents_schema_path",
                        help="AGENTS.* claims source (.json or DuckDB file)")
    parser.add_argument("--osi", dest="osi_path",
                        help="Apache Ossie documents (osi/ dir, one document, "
                             "or dbt's target/osi_document.json)")
    parser.add_argument("--uc-metrics", dest="uc_metrics_path",
                        help="Unity Catalog metric view YAML (file or directory)")
    parser.add_argument("--catalog", dest="catalog_path",
                        help="the catalog YAML the scan used — verdicts depend on its "
                             "columns and types, so current reliance re-checks it")
    # None: re-hash with the scan's own recorded dialect. A default of duckdb
    # re-read a Databricks estate's backticks as drift.
    parser.add_argument("--dialect", default=None)


def _estate_kwargs(args: argparse.Namespace) -> dict | None:
    """Estate sources from parsed args, or None when none were passed."""
    estate = {
        "dbt_path": args.dbt,
        "sql_dir": args.sql_dir,
        "views_path": args.views_path,
        "metrics_path": args.metrics,
        "agents_schema_path": args.agents_schema_path,
        "osi_path": args.osi_path,
        "uc_metrics_path": args.uc_metrics_path,
        "catalog_path": getattr(args, "catalog_path", None),
    }
    if not any(estate.values()):
        return None
    if args.dialect:
        estate["dialect"] = args.dialect
    return estate


def _cmd_scan(args: argparse.Namespace) -> int:
    if not (args.dbt or args.sql_dir or args.views_path):
        print("scan needs a computed-SQL source: --dbt, --sql-dir, or --views",
              file=sys.stderr)
        return 2
    result, store = run_scan(
        dbt_path=args.dbt,
        metrics_path=args.metrics,
        catalog_path=args.catalog,
        db_path=args.db,
        dialect=args.dialect,
        label=args.label,
        execute_seed=args.execute_seed,
        sql_dir=args.sql_dir,
        views_path=args.views_path,
        agents_schema_path=args.agents_schema_path,
        osi_path=args.osi_path,
        uc_metrics_path=args.uc_metrics_path,
        default_database=args.default_database,
        default_schema=args.default_schema,
    )
    stats = result.stats()
    print(f"scan {result.scan_id} · {result.observed_at}")
    print(
        f"  artifacts {stats['artifacts']} · fingerprints {stats['fingerprints']} · "
        f"claims {stats['claims']} · concepts {stats['concepts']} · "
        f"parsed {stats['parse_coverage_pct']}% · verifiable measures "
        f"{stats['verifiable_measures'][0]}/{stats['verifiable_measures'][1]} "
        f"({stats['coverage_pct']}%)"
        + (f", counting {result.unread_artifacts} unreadable artifact(s) as one each"
           if result.unread_artifacts else "")
    )
    # What the scan could NOT read, printed before the verdicts so coverage is
    # never mistaken for completeness.
    for s in result.skipped:
        print(f"  ! SKIPPED {s['system']}:{s['object_id']} — {s['reason']}")
    if result.unparseable:
        print(f"  ! {len(result.unparseable)} artifact(s) COULD NOT BE PARSED: "
              f"{', '.join(result.unparseable[:5])}"
              + (" …" if len(result.unparseable) > 5 else ""))
    if result.unrecognized:
        print(f"  ! {len(result.unrecognized)} measure(s) computed by functions touchstone "
              f"does not recognize — counted as unverifiable: "
              f"{', '.join(result.unrecognized[:5])}"
              + (" …" if len(result.unrecognized) > 5 else ""))
    if result.unmeasured:
        print(f"  ! {len(result.unmeasured)} artifact(s) yielded no measure: "
              f"{', '.join(result.unmeasured[:5])}"
              + (" …" if len(result.unmeasured) > 5 else ""))
    for relation, ids in result.duplicate_relations.items():
        print(f"  ! {relation} is produced by {len(ids)} artifacts: {', '.join(ids)}")
    print()
    # TRIAGE. A first scan of a real estate emits one verdict per undeclared
    # measure — 764 SHADOW_METRICs from 800 models in one measured run — and
    # an unfiltered wall of them is indistinguishable from noise. The verdicts
    # are all still recorded and still in the report and JSON; this only bounds
    # what the terminal prints.
    shown = [v for v in result.verdicts if v.severity >= (args.min_severity or 0)]
    hidden_by_severity = len(result.verdicts) - len(shown)
    if args.top:
        truncated = len(shown) - args.top
        shown = shown[:args.top]
    else:
        truncated = 0
    for v in shown:
        print(f"  [{v.verdict_type.value:<13}] sev {v.severity:>3}  {v.concept_label}: {v.summary}")
    if hidden_by_severity or truncated:
        parts = []
        if truncated > 0:
            parts.append(f"{truncated} more above the --top limit")
        if hidden_by_severity:
            parts.append(f"{hidden_by_severity} below --min-severity {args.min_severity}")
        print(f"  … {', '.join(parts)} — all {len(result.verdicts)} are in the "
              f"store, the report and --json")
    if result.execution:
        ex = result.execution
        print(f"\n  execution: {ex['measured']} concept(s) measured "
              f"against {ex['seed']} ({ex['engine']})")
        if ex["collisions"]:
            print(f"    !! {ex['collisions']} experimental equality(ies) FALSIFIED by "
                  "the data — an observed discrepancy on the seed data")
        for declined in ex["declined"]:
            print(f"    - not measured: {declined}")
        for err in ex["errors"]:
            print(f"    ! {err}")

    scan_meta = {
        "scan_id": result.scan_id,
        "label": result.label,
        "observed_at": result.observed_at,
        "dialect": result.dialect,
        **stats,
    }
    if args.report:
        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_report(result.concepts, result.verdicts, store, scan_meta))
        print(f"\nreport → {out}")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(report_json(result.verdicts, scan_meta))
        print(f"json   → {out}")

    # A GATE THAT PASSES ON AN UNREAD ESTATE IS NOT A GATE. `--fail-on` looked
    # only at verdicts, so an empty project, a project whose every model failed
    # to parse, and a manifest with no compiled SQL all exited 0 — a wrong
    # `--dbt` path or a forgotten `dbt compile` turned CI green. Nothing was
    # verified, so nothing objected.
    if args.min_coverage is not None and stats["coverage_pct"] < args.min_coverage:
        print(f"\nFAILED: verifiable-measure coverage {stats['coverage_pct']}% is "
              f"below the required {args.min_coverage}% — most measures in this "
              f"estate are outside the supported grammar, so its results do not "
              f"describe it",
              file=sys.stderr)
        return 1
    if args.fail_on:
        if not result.artifacts:
            print("\nFAILED: the scan found no computed SQL at all — check the "
                  "--dbt / --sql-dir / --views path", file=sys.stderr)
            return 1
        if not result.fingerprints:
            print(f"\nFAILED: {result.artifacts} artifact(s) were read and NONE "
                  "yielded a measure — nothing in this estate was verified",
                  file=sys.stderr)
            return 1
        unread = len([s for s in result.skipped if not s.get("informational")])
        if unread and args.fail_on == "any":
            print(f"\nFAILED: {unread} artifact(s) could not be read, so this scan "
                  "cannot speak for the whole estate", file=sys.stderr)
            return 1
        bad = {VerdictType.CONTRADICTION, VerdictType.SUSPECTED_DIVERGENCE}
        if args.fail_on == "any":
            bad |= {VerdictType.ORPHAN_CLAIM, VerdictType.SHADOW_METRIC, VerdictType.UNRESOLVED}
        failing = [v for v in result.verdicts if v.verdict_type in bad]
        if failing:
            # Exiting 1 with no closing line left a CI log that just ends —
            # the reason must be the last thing printed.
            kinds = sorted({v.verdict_type.value for v in failing})
            print(f"\nFAILED --fail-on {args.fail_on}: {len(failing)} verdict(s) "
                  f"of kind {', '.join(kinds)}", file=sys.stderr)
            return 1
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    from touchstone.services.diff import diff_verdicts, markdown_comment

    store = _open_existing(args.db)
    diff = diff_verdicts(store, scan_id=args.scan, baseline_scan_id=args.baseline)
    if diff is None:
        print("need two scans in the store to diff (this looks like the first)",
              file=sys.stderr)
        return 2

    for v in diff["new"]:
        print(f"  NEW      [{v['verdict']:<13}] sev {v['severity']:>3}  "
              f"{v['concept_label']}: {v['summary']}")
    for v in diff["resolved"]:
        # A vanished CERTIFIED/ALIASING is a certification LOST, not a
        # problem resolved — one word for both congratulated a typo'd path
        # that read nothing.
        # A problem the experimental rules now find equal is REPLACED by an
        # experimental equality — not resolved.
        replaced = v.get("replaced_by_experimental")
        tag = ("LOST    " if v.get("engine_verdict", v["verdict"]) in ("CERTIFIED", "ALIASING")
               else "REPLACED" if replaced else "GONE    ")
        print(f"  {tag} [{v['verdict']:<13}]          {v['concept_label']}"
              + (f" (now {'/'.join(replaced)} under the experimental rules — not a "
                 f"conclusion)" if replaced else ""))
    if not diff["new"] and not diff["resolved"]:
        print("  no verdict changes vs baseline")
    print(f"  ({diff['unchanged']} pre-existing verdict(s) unchanged)")

    if args.markdown:
        out = Path(args.markdown)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown_comment(diff))
        print(f"comment → {out}")

    if args.fail_on_new:
        bad = {VerdictType.CONTRADICTION.value, VerdictType.SUSPECTED_DIVERGENCE.value}
        if args.fail_on_new == "any":
            bad |= {VerdictType.ORPHAN_CLAIM.value, VerdictType.SHADOW_METRIC.value,
                    VerdictType.UNRESOLVED.value}
        if any(v["verdict"] in bad for v in diff["new"]):
            return 1
    return 0


def _open_existing(db: str) -> Store:
    """Open a store that must already exist.

    Read commands used to CREATE an empty .db on a typo'd --db path and then
    report "no scans recorded" — leaving a stray store behind and making the
    error look like a data problem rather than a path problem.
    """
    if not Path(db).exists():
        from touchstone.store import former_store

        raise ValueError(former_store(db) or f"no store at {db} — this command reads "
                         f"an existing store and will not create one")
    return Store(db)


def _cmd_concepts(args: argparse.Namespace) -> int:
    store = _open_existing(args.db)
    scan = store.latest_scan()
    if scan is None:
        print("no scans recorded", file=sys.stderr)
        return 2
    print(f"latest scan {scan['scan_id']} · {scan['started_at']}")
    for v in store.verdicts_for_scan(scan["scan_id"]):
        print(f"  [{v['verdict']:<13}] sev {v['severity']:>3}  {v['concept_label']}: {v['summary']}")
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    store = _open_existing(args.db)
    intervals = store.history(args.concept)
    if not intervals:
        print(f"no recorded definitions for '{args.concept}'", file=sys.stderr)
        return 2
    for iv in sorted(intervals, key=lambda i: (i.object_key, i.valid_from)):
        until = iv.valid_to or "now"
        flag = "  ← superseded" if iv.valid_to else ""
        print(f"  {iv.relation or iv.object_key}")
        print(f"    {iv.semantic_hash[:12]}  valid {iv.valid_from} → {until}{flag}")
    changes = [iv for iv in intervals if iv.valid_to is not None]
    if changes:
        print(
            f"\n  ⚠ {len(changes)} definition change(s) recorded — historical comparisons "
            "spanning these boundaries are semantic restatements."
        )
    return 0


def _cmd_asof(args: argparse.Namespace) -> int:
    store = _open_existing(args.db)
    try:
        live = store.as_of(args.concept, args.at)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if not live:
        print(f"'{args.concept}' had no live definition at {args.at}", file=sys.stderr)
        return 2
    print(f"'{args.concept}' as of {args.at}:")
    for iv in live:
        row = store.fingerprint_row(iv.ref_id)
        expr = row["measure_expr"] if row else "?"
        print(f"  {iv.relation or iv.object_key}: {expr}   [{iv.semantic_hash[:12]}]")
    return 0


def _cmd_attest(args: argparse.Namespace) -> int:
    store = _open_existing(args.db)
    freshness = None
    estate = _estate_kwargs(args)
    if estate is not None:
        from touchstone.services.freshness import check_freshness

        freshness = check_freshness(store, **estate)
    try:
        receipt = store.issue_receipt(args.concept, args.as_of, freshness=freshness)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if receipt is None:
        print(f"cannot attest '{args.concept}': no verdicts recorded", file=sys.stderr)
        return 2
    # stdout stays pure receipt JSON (pipelines do `attest > r.json`); the
    # human-facing freshness line goes to stderr.
    print(json.dumps(receipt.as_dict(), indent=2))
    fr = receipt.freshness or {}
    if fr.get("status"):
        print(f"freshness: {fr['status']} — {fr.get('note', '')}", file=sys.stderr)
    from touchstone.signing import assess_receipt
    # The freshness block attest just computed IS a live check, made now
    # against the scan this receipt was issued from.
    print("reliance: " + assess_receipt(receipt.as_dict(), receipt.freshness).reliance(),
          file=sys.stderr)
    return 0


def _cmd_verify_receipt(args: argparse.Namespace) -> int:
    from touchstone.signing import verify_receipt_dict

    if args.receipt == "-":
        raw = sys.stdin.read()
    else:
        p = Path(args.receipt)
        if not p.is_file():
            raise ValueError(f"{p}: receipt file not found")
        raw = p.read_text()
    try:
        receipt = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{args.receipt}: is not valid JSON — {e}") from e

    from touchstone.signing import assess_receipt

    ok, message = verify_receipt_dict(receipt, args.public_key)
    print(message)
    if not ok:
        return 1

    # A valid SIGNATURE is not a valid STATUS — decay lives in receipt_trust,
    # shared with the MCP tool so both surfaces refuse a stale certificate.
    verified_as_of = receipt.get("verified_as_of")
    freshness = receipt.get("freshness")
    fresh_status = freshness.get("status") if isinstance(freshness, dict) else None
    if verified_as_of:
        from touchstone.conclusions import signed_status_label

        print(f"  signed status {signed_status_label(receipt.get('status'))} · scan of "
              f"{verified_as_of}"
              + (f" · self-reported freshness {fresh_status}" if fresh_status else ""))
    live = None
    # Authentic = what the store signed: a pinned key establishes the signer,
    # the store itself establishes the exact payload. The embedded key alone
    # is tamper-evidence, not authenticity.
    authentic = True if args.public_key else None
    estate = _estate_kwargs(args)
    from touchstone.store import former_store

    # A missing default store beside the former one is refused, not skipped:
    # skipping it dropped the issued-by-this-store check without a word.
    store = _open_existing(args.db) if (estate is not None or Path(args.db).exists()
                                        or former_store(args.db)) else None
    if store is not None:
        authentic = store.issued(receipt)
        if not authentic:
            print("  this store did not issue this receipt (payload not found as signed)")
    if estate is not None and isinstance(receipt.get("scan_id"), str):
        from touchstone.services.freshness import check_freshness

        estate.pop("dialect", None)   # the receipt's scan records its own
        if authentic:
            live = check_freshness(store, scan_id=receipt["scan_id"], **estate)
        else:
            live = {"status": "unknown", "scan_id": receipt["scan_id"],
                    "note": "this store did not issue this receipt — its estate "
                            "says nothing about it"}
        print(f"  live re-check against scan {receipt['scan_id']}: {live.get('status')}"
              + (f" — {live['note']}" if live.get("note") else ""))
    if store is not None:
        store.close()
    a = assess_receipt(receipt, live, signature_authentic=authentic)
    print(f"  reliance: {a.reliance()}")
    print("  policy assessment (made now; the signed receipt is unchanged): "
          + json.dumps(a.policy_assessment))
    for warning in a.warnings:
        print(f"  WARNING: {warning}", file=sys.stderr)
    if getattr(args, "require_current", False):
        # Pipelines gating CURRENT reliance must not pass on a receipt that is
        # only valid for a past instant.
        return 0 if a.current_use else 1
    return 0 if a.valid_for_stated_use else 1


def _cmd_mcp(args: argparse.Namespace) -> int:
    from touchstone.mcp import serve

    return serve(args.db, estate=_estate_kwargs(args))


def _cmd_publish(args: argparse.Namespace) -> int:
    from touchstone.services.writeback import publish_receipts, refresh_published_policy

    if args.refresh_policy:
        from touchstone.store import former_store

        store = (_open_existing(args.db) if Path(args.db).exists() or former_store(args.db)
                 else None)
        try:
            out = refresh_published_policy(args.warehouse, store)
        finally:
            if store is not None:
                store.close()
        tables = " + ".join(out["verdict_tables"]) or "no verdicts table"
        print(f"refreshed {out['warehouse']}: {out['verdict_rows']} {tables} "
              f"row(s) mapped (usable_for_reliance = false), {out['assessments']} "
              f"assessment row(s) beside unchanged signed receipts")
        if out["former_provider_rows"]:
            print(f"  {out['former_provider_rows']} agents.root row(s) published under the "
                  f"former name `semantix` now sit under `touchstone`")
        return 0
    store = _open_existing(args.db)
    freshness = None
    estate = _estate_kwargs(args)
    if estate:
        from touchstone.services.freshness import check_freshness
        freshness = check_freshness(store, **estate)
    try:
        summary = publish_receipts(store, args.warehouse, freshness=freshness)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"published {summary['receipts']} receipt(s) + skill/verify-metric "
          f"→ agents.root in {summary['warehouse']} (scan {summary['scan_id']})"
          + (f" · freshness {freshness.get('status')}" if freshness else
             " · freshness unchecked — pass estate sources (--dbt/--sql-dir/…) "
             "to embed a live re-check in every receipt"))
    for key in summary["keys"]:
        print(f"  touchstone · {key}")
    print(f"  touchstone.verdicts · {summary['verdict_rows']} row(s) — gate on "
          f"them via dbt tests (integrations/dbt/touchstone), warehouse alerts, "
          f"or asset checks (docs/action-rails.md)")
    if summary["former_verdict_rows"] is not None:
        print(f"  semantix.verdicts (the former name) · {summary['former_verdict_rows']} "
              f"row(s), the same projection, kept in step for gates that still read it "
              f"— point them at touchstone.verdicts, then drop it")
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    from touchstone.bench import run_bench

    dialects = None
    if args.dialects is not None:
        parts = [p.strip() for p in args.dialects.split(",")]
        dialects = list(dict.fromkeys(p for p in parts if p))
        if not dialects:
            print("--dialects was given but names no dialects", file=sys.stderr)
            return 2
        # A typo'd dialect silently parses as sqlglot's generic dialect and
        # produces a run that proves nothing — reject unknown names up front.
        from sqlglot.dialects import DIALECTS

        known = {d.lower() for d in DIALECTS}
        unknown = [d for d in dialects if d.lower() not in known]
        if unknown:
            print(f"unknown dialect(s): {', '.join(unknown)} — "
                  f"known: {', '.join(sorted(known))}", file=sys.stderr)
            return 2
    result = run_bench(args.corpus, dialects=dialects)
    det = result["equivalence_detection"]
    print(f"equivalence corpus: {result['cases']} pairs · "
          f"dialects: {', '.join(result['dialects'])} · "
          f"proof-grade coverage {result['proof_grade_coverage_pct']}%")
    for d, r in result["by_dialect"].items():
        dd = r["equivalence_detection"]
        unproven = len(r["near_misses"]["unproven"])
        print(f"    {d:<11} {dd['detected']}/{dd['of']} detected · "
              f"near-misses {r['near_misses']['distinguished']}/{r['near_misses']['of']} · "
              f"coverage {r['proof_grade_coverage_pct']}% · "
              f"false certs {len(r['false_certifications'])}"
              + (f" · UNPROVEN {unproven}" if unproven else ""))
    rate = f"{det['rate']:.0%}" if det["rate"] is not None else "n/a"
    print(f"  equivalence detection: {det['detected']}/{det['of']} ({rate})"
          + (f" — missed: {', '.join(det['missed'])}" if det["missed"] else ""))
    gaps = result["known_gaps"]
    if gaps["confirmed"] or gaps["closed"]:
        print(f"  known gaps: {len(gaps['confirmed'])} confirmed"
              + (f" · CLOSED (promote to scored): {', '.join(gaps['closed'])}"
                 if gaps["closed"] else ""))
    nm = result["near_misses"]
    print(f"  near-misses distinguished: {nm['distinguished']}/{nm['of']}"
          + (f" — UNPROVEN (nothing demonstrated): {', '.join(nm['unproven'])}"
             if nm["unproven"] else ""))
    if result["refused"]:
        # The scope's cost: pairs outside the proof grammar are refused by
        # design — counted and named, never scored as detected or distinguished.
        print(f"  refused by the proof policy ({result['proof_policy']}): "
              f"{len(det['refused'])} equivalence / {len(nm['refused'])} near-miss run(s)")
        for name, why in result["refused"].items():
            print(f"    {name}: {why.split(': ', 1)[-1]}")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
        print(f"  json → {out}")
    if result["false_certifications"]:
        print(f"  FALSE EQUALITIES: {', '.join(result['false_certifications'])} "
              f"— a verifier cannot ship this", file=sys.stderr)
        return 1
    # "Zero false certifications" over a run that proved nothing is not a
    # pass. A missed equivalence or an unproven near-miss means the prover
    # did not demonstrate what the corpus asks of it.
    if det["missed"] or nm["unproven"]:
        print("  gate FAILED: the corpus was not fully demonstrated "
              f"({len(det['missed'])} missed, {len(nm['unproven'])} unproven)",
              file=sys.stderr)
        return 1
    print("  false equalities: 0 ✓ (experimental rules; not a certification)")
    return 0


def _cmd_mine_relationships(args: argparse.Namespace) -> int:
    from touchstone.adapters.catalog import load_catalog, load_declared_relationships
    from touchstone.extraction.registry import SchemaRegistry
    from touchstone.services.relationships import (
        graph_with_status,
        mine_sql_text,
        record_declared,
        record_mined,
    )

    if not args.history_file and not (args.warehouse_url and args.history_query):
        print("mine-relationships needs --history-file, or --warehouse with "
              "--history-query", file=sys.stderr)
        return 2

    if args.history_file:
        text = Path(args.history_file).read_text()
        source = args.history_file
    else:
        from touchstone.warehouse import connect_warehouse

        wh = connect_warehouse(args.warehouse_url)
        try:
            rows = wh.query(args.history_query)
        finally:
            wh.close()
        text = ";\n".join(str(r[0]) for r in rows if r and r[0])
        source = f"{args.warehouse_url} [{args.history_query[:60]}]"

    catalog = load_catalog(args.catalog) if args.catalog else {}
    registry = SchemaRegistry(catalog)
    store = Store(args.db)

    mined, meta = mine_sql_text(text, registry, args.dialect)
    record_mined(store, mined, source)
    if args.catalog:
        record_declared(store, load_declared_relationships(args.catalog), args.catalog)

    print(f"mined {meta['edges']} edge(s) from {meta['statements']} statement(s)"
          + (f" ({meta['skipped_statements']} unparseable, skipped)"
             if meta["skipped_statements"] else ""))
    _print_graph(graph_with_status(store))
    return 0


def _cmd_verify_relationships(args: argparse.Namespace) -> int:
    from touchstone.services.relationships import verify_edges
    from touchstone.warehouse import DuckDBWarehouse, connect_warehouse

    if not args.warehouse_url and not args.seed_path:
        print("verify-relationships needs --warehouse URL or --seed seed.sql",
              file=sys.stderr)
        return 2

    if args.warehouse_url:
        warehouse = connect_warehouse(args.warehouse_url)
    else:
        artifacts = []
        if args.dbt:
            from touchstone.adapters.dbt import load_dbt

            artifacts = load_dbt(args.dbt)
        warehouse = DuckDBWarehouse.ephemeral(
            Path(args.seed_path).read_text(), artifacts,
            label=f"ephemeral:{args.seed_path}",
        )

    declared_pks = None
    if args.catalog:
        from touchstone.adapters.catalog import load_primary_keys

        declared_pks = load_primary_keys(args.catalog)

    store = _open_existing(args.db)
    try:
        results = verify_edges(store, warehouse, top=args.top, declared_pks=declared_pks)
    finally:
        warehouse.close()
    if not results:
        print("no edges recorded — run mine-relationships first", file=sys.stderr)
        return 2
    _print_graph(results)
    # Count by actual status. Deriving "passed" as everything-not-failed
    # counted every edge the warehouse could not answer for as a success.
    from touchstone.services.relationships import CHECKS_PASSED, LIMITATION

    passed = [r for r in results if r["status"] == CHECKS_PASSED]
    failed = [r for r in results if r["status"] == "failed"]
    unverified = [r for r in results if r["status"] not in (CHECKS_PASSED, "failed")]
    print(f"\n  checked {len(results)} edge(s): {len(passed)} checks passed on observed "
          f"data, {len(failed)} failed, {len(unverified)} unverified")
    if passed:
        print(f"  ({LIMITATION})")
    return 0


def _cmd_relationships(args: argparse.Namespace) -> int:
    from touchstone.services.relationships import graph_with_status

    store = _open_existing(args.db)
    rows = graph_with_status(store, table=args.table)
    if not rows:
        print("no relationships recorded — run mine-relationships first",
              file=sys.stderr)
        return 2
    _print_graph(rows)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rows, indent=2))
        print(f"\njson → {out}")
    return 0


def _print_graph(rows: list[dict]) -> None:
    """Two axes, two columns: validity (what the warehouse proved) and usage
    (declared vs. practice). Never collapse them into one word."""
    for r in rows:
        check = r.get("check")
        detail = ""
        if r.get("unrunnable"):
            detail = f"\n      ? could not check — {r['unrunnable'].get('error', '')}"
        elif check:
            ev = check["evidence"]
            if ev.get("error"):
                detail = f"\n      ! {ev['error']}"
            elif r.get("status") == "unverified":
                # An inconclusive check's cardinality and parent key were
                # computed from 0/1-value trivia — the agent view already
                # nulls them; printing "1:1, parent key ..." here presented
                # the same two fabricated measurements as facts.
                detail = f"\n      — {ev.get('reason', 'nothing was demonstrated')}"
            else:
                detail = f"\n      {ev.get('cardinality', '?')}"
                if ev.get("parent"):
                    detail += f", parent key {ev['parent']}"
                if ev.get("reason"):
                    detail += f" — {ev['reason']}"
                scope = ev.get("scope") or {}
                detail += (f"\n      as of {check.get('checked_at', '?')} · "
                           f"{scope.get('method', 'full table scan')}"
                           + (" (sampled)" if scope.get("sampled") else ""))
        queries = r["mined_queries"]
        used = f"{queries} quer{'y' if queries == 1 else 'ies'}" if queries else "never used"
        tag = {"checks_passed_on_observed_data": "CHECKS PASSED"}.get(
            r["status"], r["status"].upper())
        print(f"  [{tag:<13}] {r['usage']:<14} {r['label']}  · {used}"
              + ("  · competing keys" if r.get("competing_keys") else "")
              + detail)


def _assess_run(args: argparse.Namespace, provider) -> int:
    from touchstone.assess.providers.databricks import DatabricksError
    from touchstone.assess.record import validate
    from touchstone.assess.runner import ADVISORY_NOTE, assess_records

    records, invalid = [], 0
    try:
        with open(args.comparisons, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    record = json.loads(line)
                    if validate(record):
                        invalid += 1      # never hand a classifier a malformed record
                    else:
                        records.append(record)
    except (OSError, ValueError) as e:
        print(f"touchstone assess: cannot read {args.comparisons}: {e}", file=sys.stderr)
        return 2
    cache: dict = {}
    if args.cache and Path(args.cache).exists():
        with open(args.cache, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    entry = json.loads(line)
                    cache[entry["key"]] = entry["assessment"]
    known = set(cache)
    try:
        verdicts, stats = assess_records(provider, records,
                                         with_observations=not args.without_observations,
                                         cache=cache)
    except DatabricksError as e:
        print(f"touchstone assess: {e}", file=sys.stderr)
        return 2
    with open(args.out, "w", encoding="utf-8") as f:
        for v in verdicts:
            f.write(json.dumps(v) + "\n")
    if args.cache:
        with open(args.cache, "a", encoding="utf-8") as f:
            for key in cache.keys() - known:
                f.write(json.dumps({"key": key, "assessment": cache[key]}) + "\n")
    print(f"assess run · provider {provider.name}"
          + (f" ({provider.model})" if provider.model else "")
          + f" · {stats['records']} judgment(s) covering {stats['executions']} execution(s)"
          + (f" · {invalid} invalid record(s) skipped" if invalid else ""))
    if not stats["measures_performance"]:
        print("  NOTE: this provider measures nothing — its labels are not readings")
    print(f"  calls {stats['calls']} · from cache {stats['from_cache']} · "
          f"{stats['without_rules_reading']} judgment(s) the rules did not read")
    if stats["held"]:
        print(f"  HELD for a person (not sent — steering text in comments or strings): "
              f"{stats['held']}")
    print("  labels: " + (", ".join(f"{k} {n}" for k, n in sorted(stats["labels"].items()))
                          or "none"))
    if stats["failed"]:
        print("  failed: " + ", ".join(f"{k} {n}" for k, n in sorted(stats["failed"].items())))
    print(f"  {ADVISORY_NOTE}")
    print(f"  verdicts → {args.out}")
    return 1 if records and not stats["labels"] and not stats["held"] else 0


def _cmd_assess(args: argparse.Namespace) -> int:
    from touchstone.assess.providers import ProviderConfig, get_provider
    from touchstone.assess.runner import load_dataset, observations_from_monitor, run_benchmark

    if args.assess_command == "review-sheet":
        return _assess_review_sheet(args)
    if args.assess_command == "compare-labels":
        return _assess_compare_labels(args)

    try:
        options = {"seed": args.seed, "batch_size": args.batch_size}
        for name in ("host", "profile", "max_tokens", "temperature"):
            if getattr(args, name) is not None:
                options[name] = getattr(args, name)
        provider = get_provider(ProviderConfig(args.provider, model=args.model,
                                               endpoint=args.endpoint, options=options))
    except (NotImplementedError, ValueError) as e:
        print(f"touchstone assess: {e}", file=sys.stderr)
        return 2
    if args.assess_command == "run":
        return _assess_run(args, provider)
    dataset = load_dataset(args.dataset)
    observations = (observations_from_monitor(dataset, args.split)
                    if args.with_observations else None)
    from touchstone.assess.providers.databricks import DatabricksError

    try:
        report = run_benchmark(provider, dataset=dataset, split=args.split,
                               observations=observations)
    except DatabricksError as e:
        print(f"touchstone assess: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # Outside a provider call (building records): nothing was reported.
        print("touchstone assess: interrupted before a report was produced", file=sys.stderr)
        return 130
    labels = report["labels"]
    print(f"assess benchmark · {report['dataset']} · split {report['split']} · "
          f"{report['items']} item(s) over {len(report['families'])} famil"
          f"{'y' if len(report['families']) == 1 else 'ies'} · provider {report['provider']}"
          + (f" ({report['model']})" if report["model"] else ""))
    print(f"  labels: authored by {labels['authored_by']}, reviewed by "
          f"{labels['reviewed_by']}; human review: {labels['human_review']}")
    for note in report.get("notes", []):
        print(f"  NOTE: {note}")
    print(f"  {report['advisory_only']}")

    def c(x):
        return f"{x['count']}/{x['of']}"

    def line(label: str, m: dict, confirmed: str = "confirmed") -> None:
        d, ok, ie = m["drift"], m["permitted"], m["insufficient_evidence"]
        print(f"  {label}")
        print(f"      drift (n={d['of']}): flagged {c(d['flagged'])} · labeled permitted "
              f"{c(d['labeled_permitted'])} · undecided {c(d['undecided'])} · failed "
              f"{c(d['failed'])}")
        print(f"      permitted (n={ok['of']}): {confirmed} "
              f"{c(ok.get('confirmed') or ok['labeled_permitted'])} · flagged as "
              f"drift {c(ok['flagged_as_drift'])} · undecided {c(ok['undecided'])} · failed "
              f"{c(ok['failed'])}")
        print(f"      insufficient evidence (n={ie['of']}): abstained {c(ie['abstained'])} · "
              f"flagged {c(ie['flagged'])} · labeled permitted {c(ie['labeled_permitted'])} · "
              f"failed {c(ie['failed'])}")
        cal = m["calibration"]
        agreement = m["agreement_with_labels"]
        print(f"      answered {m['answered']}/{m['n']} · abstained {c(m['abstention'])}"
              + (f" · agreement with labels {agreement:.0%}" if agreement is not None else "")
              + (f" · ECE {cal['ece']:.3f} over {cal['scored']}/{cal['of']} answered with a "
                 f"confidence ({report.get('confidence_source') or 'source not stated'})"
                 if cal["ece"] is not None else " · calibration: no confidence to score")
              + (f" · {cal['out_of_range']} confidence(s) outside [0, 1] not scored"
                 if cal.get("out_of_range") else ""))
        latency = m["latency_ms"]
        print("      cost: " + (f"${m['cost_usd']}" if m["cost_reported"] else
                                "not reported")
              + " · latency: " + (f"mean {latency['mean']} ms, p95 {latency['p95']} ms"
                                  if latency else "not reported"))
        if m["errors"]["count"]:
            kinds = ", ".join(f"{k} ×{v}" for k, v in m["errors"]["by_kind"].items())
            print(f"      errors: {m['errors']['count']} ({kinds})")
    for arm, result in report["arms"].items():
        line(arm.replace("_", " ")
             + ("" if result["performance_measured"] else " [not a measurement]"),
             result["overall"])
    for name, value in report["baselines"].items():
        if isinstance(value, dict):
            # touchstone's "permitted" readings are experimental matches, never
            # confirmations: positive verdicts are suspended.
            line(f"baseline: {name.replace('_', ' ')}", value,
                 confirmed="labeled permitted (experimental matches)")
        else:
            print(f"  baseline: {name.replace('_', ' ')} — {value}")
    if args.json_out:
        from touchstone.assess.runner import to_json

        out = Path(args.json_out)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(to_json(report))
            print(f"  report JSON: {out}")
        except OSError as e:
            print(f"touchstone assess: could not write the report JSON to {out}: "
                  f"{e.strerror or e}", file=sys.stderr)
            if not report.get("stopped"):
                return 2
    if report.get("stopped"):
        # The report above keeps what was answered before the run stopped;
        # the exit code is the stop's KIND, never the server's wording.
        print(f"touchstone assess: the run stopped: {report['stopped']}", file=sys.stderr)
        return 130 if report.get("stop_kind") == "interrupted" else 2
    # A run in which no call answered measured nothing: never exit 0 on it.
    if any(a["overall"]["answered"] == 0 for a in report["arms"].values()):
        print("touchstone assess: no call answered in at least one arm — nothing was measured",
              file=sys.stderr)
        return 1
    return 0


def _assess_review_sheet(args: argparse.Namespace) -> int:
    import csv

    from touchstone.assess.runner import LABELS, REVIEW_COLUMNS, load_dataset, review_sheet

    dataset = load_dataset(args.dataset)
    rows = review_sheet(dataset)
    out = Path(args.out)
    guide = out.with_name(f"{out.stem}.guide.md")
    if guide.resolve() == out.resolve():
        print("touchstone assess: --out must not be the guide's own name", file=sys.stderr)
        return 2
    existing = [p for p in (out, guide) if p.exists()]
    if existing and not args.force:
        print(f"touchstone assess: {', '.join(map(str, existing))} already exist(s) — a filled "
              f"sheet would be lost; pass --force to overwrite", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    d = dataset["definitions"]
    catalog = "\n".join(f"- `{rel}`: " + ", ".join(f"`{c}` {t}" for c, t in cols.items())
                         for rel, cols in d["catalog"].items())
    facts = "\n".join(f"- {f}" for f in d.get("stated_facts") or [])
    guide.write_text(
        "# Blind review of the metric-use labels\n\n"
        "Label each row from the definitions and the policy below and the row itself "
        "(`sql`, and `session_statements` — statements that ran earlier in the same "
        "session and can change what its names resolve to). `metric` is the governed metric the "
        "row is compared with. Fill `label` with one of: "
        + ", ".join(f"`{x}`" for x in LABELS)
        + ". Add a one-sentence `rationale` and your name in `reviewer`. Rows marked in "
        "`priority` need the most care; the mark says nothing about their labels.\n\n"
        "Until you have finished, do not open: the dataset's family files "
        "(`benchmark/metric_uses/*.yaml` other than this sheet), `adjudication.yaml`, "
        "`blind_labels.yaml`, the dataset README's label sections, `docs/assess.md` below "
        "its review instructions (it reports results by family), or any touchstone output."
        f"\n\n## The definition: metric view `{d['view_name']}` over "
        f"`{dataset['view']['source']}`\n\nIt is THE governed view: a query that reads "
        f"`{d['view_name']}` reads the governed source through it.\n\n```yaml\n" + d["view"]
        + "```\n\n## The data\n\nColumns and declared types:\n\n" + catalog
        + "\n\nStated facts:\n\n" + facts
        + "\n\n## Policy\n\n" + d["policy"] + "\n",
        encoding="utf-8")
    flagged = sum(1 for r in rows if r["priority"])
    print(f"review sheet: {len(rows)} item(s), {flagged} marked priority → {out}")
    print(f"  instructions, definitions and policy → {guide}")
    print("  then: touchstone assess compare-labels " + str(out))
    return 0


def _assess_compare_labels(args: argparse.Namespace) -> int:
    import csv

    from touchstone.assess.runner import compare_labels, load_dataset

    # utf-8-sig: a sheet saved by Excel starts with a byte-order mark.
    with open(args.sheet, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    try:
        result = compare_labels(load_dataset(args.dataset), rows)
    except ValueError as e:
        print(f"touchstone assess: {e}", file=sys.stderr)
        return 2
    print(f"human review: {result['reviewed']}/{result['of']} item(s) labeled · agreed "
          f"{result['agreed']} · disagreed {len(result['disagreements'])}")
    for d in result["disagreements"]:
        print(f"  {d['id']}: dataset {d['dataset']} · reviewer {d['reviewer_label']}"
              + (" (changes the advisory class)" if d["advisory_class_changes"] else "")
              + (f" — {d['reviewer_rationale']}" if d["reviewer_rationale"] else ""))
    problems = False
    for key, label in (("not_reviewed", "not reviewed"), ("unknown_ids", "unknown ids"),
                       ("invalid_labels", "invalid labels"),
                       ("duplicate_ids", "labeled more than once"),
                       ("conflicting_ids", "labeled twice with different labels (not scored)")):
        if result[key]:
            print(f"  {label}: {', '.join(result[key])}")
            problems = problems or key != "not_reviewed"
    if result["reviewed"] == 0:
        labeled = result["unknown_ids"] or result["invalid_labels"] or result["conflicting_ids"]
        print("touchstone assess: " + ("no item could be scored — every labeled row has one of "
                                     "the problems listed above" if labeled
                                     else "no row of the sheet carries a label"),
              file=sys.stderr)
        return 1
    print("  nothing was changed: decide each disagreement and record it in "
          "adjudication.yaml")
    return 1 if problems else 0


def _cmd_monitor(args: argparse.Namespace) -> int:
    from touchstone.adapters.catalog import load_catalog
    from touchstone.services.history import from_records, read_history
    from touchstone.services.monitor import build_registry, monitor_history

    sources = [bool(args.pilot_dir), bool(args.history_file),
               bool(args.warehouse_url and args.history_query)]
    if sum(sources) != 1:
        print("monitor needs exactly one history source: --pilot DIR, "
              "--history-file, or --warehouse with --history-query",
              file=sys.stderr)
        return 2
    if args.pilot_dir and args.authority:
        print("monitor: --authority does not apply with --pilot — the pilot "
              "package names its own authority", file=sys.stderr)
        return 2
    store = _open_existing(args.db)
    scan = store.latest_scan()
    if scan is None:
        print("touchstone: no scans recorded — monitor compares the query "
              "stream against a scan's reference set; run `touchstone scan` "
              "first", file=sys.stderr)
        return 2
    dialect = args.dialect or scan.get("dialect") or "duckdb"

    pilot = None
    if args.pilot_dir:
        from touchstone.services.pilot import load_pilot

        pilot = load_pilot(args.pilot_dir, dialect)
        intake = read_history(pilot.history)
    elif args.history_file:
        intake = read_history(args.history_file)
    else:
        from touchstone.warehouse import connect_warehouse

        wh = connect_warehouse(args.warehouse_url)
        try:
            records = wh.query_records(args.history_query)
        finally:
            wh.close()
        if records and "statement_text" not in {k.lower() for k in records[0]} \
                and "query_text" not in {k.lower() for k in records[0]}:
            # A bare one-column pull: the first column is the SQL.
            records = [{"statement_text": next(iter(r.values()), None)} for r in records]
        intake = from_records(records, source=args.warehouse_url, fmt="warehouse")

    # The observed side must resolve through the SAME registry shape the scan
    # used — models registered, columns resolved to raw terminals — or its
    # cores could never equal the reference cores and every conforming use
    # would misreport as unregistered.
    artifacts = []
    if args.dbt:
        from touchstone.adapters.dbt import load_dbt
        artifacts += load_dbt(args.dbt)
    if args.sql_dir:
        from touchstone.adapters.sql_dir import load_sql_dir
        artifacts += load_sql_dir(args.sql_dir, dialect)[0]
    if args.views_path:
        from touchstone.adapters.sql_dir import load_views_file
        artifacts += load_views_file(args.views_path, dialect)[0]
    scan_read_models = any(r["kind"] == "computed"
                           for r in store.scan_snapshot(scan["scan_id"]))
    if not artifacts and scan_read_models:
        # Without the scan's models, observed lineage resolves to different
        # terminals than the reference cores' and byte-exact reruns of the
        # estate's own marts were accused. A registry the scan did not have
        # is not a degraded mode; it is a different comparison.
        print("touchstone: monitor needs the estate the scan read (--dbt, "
              "--sql-dir or --views) so observed queries resolve through the "
              "same models the reference definitions did", file=sys.stderr)
        return 2
    catalog_path = args.catalog or (pilot.catalog if pilot and pilot.catalog else None)
    catalog = load_catalog(catalog_path) if catalog_path else {}
    context = _scan_context(scan, args)
    registry = build_registry(catalog, artifacts, dialect, context)
    asset_map = {}
    if args.asset_map:
        from touchstone.adapters.loadutil import load_yaml

        asset_map = {str(k): str(v) for k, v in load_yaml(args.asset_map).items()}
    authority = ({s.strip() for s in args.authority.split(",") if s.strip()}
                 if args.authority else None)

    result = monitor_history(store, intake, registry, dialect, min_runs=args.min_runs,
                             authority_systems=authority, pilot=pilot,
                             context=context, assert_context=args.assert_context,
                             asset_map=asset_map,
                             export=bool(getattr(args, "export_comparisons", None)))
    _print_monitor(result)
    if getattr(args, "export_comparisons", None):
        records = result.pop("comparisons")
        out = Path(args.export_comparisons)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        executions = sum((r.get("occurrences") or {}).get("executions", 1) for r in records)
        unread = sum(1 for r in records if "unverified_observations" not in r)
        print(f"  comparisons: {len(records)} touchstone-comparison/1 record(s), one per "
              f"distinct judgment, covering {executions} execution(s); {unread} the rules did "
              f"not read → {out}")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
        print(f"  findings JSON: {args.json_out}")
    # Only SUSPECTED DIFFERENCES from a named authoritative definition fail
    # the gate: a candidate is a hypothesis by construction, and failing CI on
    # an unnamed hypothesis is the alert-fatigue path.
    suspected = [f for f in result["findings"]
                 if f["kind"] in ("suspected_difference", "definition_violation")]
    if args.fail_on_alt and suspected:
        print(f"\ntouchstone monitor: FAILED — {len(suspected)} suspected "
              f"difference(s) from an authoritative definition (suspected, not "
              f"verified)", file=sys.stderr)
        return 1
    if args.fail_on_alt:
        # Passing says nothing suspected was FOUND — never that uses conform:
        # a use that matched under the experimental rules is unverifiable.
        print("\ntouchstone monitor: no suspected difference found — not a statement "
              "that any use conforms (positive verdicts are suspended)", file=sys.stderr)
    return 0


def _print_monitor(result: dict) -> None:
    ref = result["reference"]
    print(f"monitor · against scan {result['scan_id']} · authority "
          f"{', '.join(ref['authority_systems']) or 'none'}: {ref['definitions']} "
          f"definition(s) across {ref['concepts']} concept(s)"
          + (f" ({ref['excluded_below_proof']} outside the supported grammar excluded)"
             if ref["excluded_below_proof"] else "")
          + (f" · authorities suspected to disagree: "
             f"{', '.join(ref['contested_concepts'])}"
             if ref["contested_concepts"] else ""))
    print(f"comparison: {result['comparison']}")
    if result["toolchain"]["positive_verdicts_withheld"]:
        print("toolchain: positive verdicts WITHHELD — the parser is not a qualified "
              "version (see `touchstone/toolchain.py`)")
    ctx = result["context"]
    print("context: "
          + ("per-row catalog/schema present" if ctx["per_row"] else "no per-row context")
          + " · asserted for all executions: "
          + (".".join(p for p in ctx["asserted"] if p) if ctx["asserted"] else "none"))
    h = result["history"]
    parts = []
    for key, label in (("non_query_statements", "non-query"),
                       ("unreadable_statements", "unreadable"),
                       ("statements_without_measures", "no measure"),
                       ("statements_with_nested_measures",
                        "measure in a nested scope — not read"),
                       ("statements_mixing_raw_aggregates",
                        "raw aggregates beside governed measures — raw ones not read")):
        if result[key]:
            parts.append(f"{result[key]} {label}")
    print(f"history: {h['executions']} execution(s)"
          + (f" ({h['duplicate_executions']} duplicate id(s) removed)"
             if h["duplicate_executions"] else "")
          + f" · {result['statements']} statement(s): {result['parsed_statements']} parsed"
          + (f" ({', '.join(parts)})" if parts else "")
          + (f", {result['skipped_statements']} unparseable"
             if result["skipped_statements"] else ""))
    if h["ignored_columns"]:
        print(f"  columns not read: {', '.join(h['ignored_columns'])}")
    if h["executions_without_time"]:
        print(f"  {h['executions_without_time']} execution(s) carry no start time — "
              f"no definition can be evidenced for them")
    if h.get("executions_with_unreadable_time"):
        print(f"  {h['executions_with_unreadable_time']} execution(s) carry an unreadable "
              f"start time — no definition can be evidenced for them")
    if h.get("executions_outside_window"):
        print(f"  {h['executions_outside_window']} execution(s) fall outside the pilot "
              f"window — not judged")
    if h.get("session_events"):
        print(f"  session: {h['session_events']} statement(s) that can change name "
              f"resolution (USE/SET/CREATE …) · {h['statements_after_session_change']} "
              f"statement(s) whose relation identity therefore depends on the session · "
              f"{h['statements_with_reconstructed_context']} resolved from an earlier USE "
              f"in the same execution")
    f = result["funnel"]
    n = f["measure_uses"]

    def line(label: str, count: int, extra: str = "") -> None:
        pct = f" ({100 * count / n:.0f}%)" if n else ""
        print(f"  {label:<50} {count:>6}/{n}{pct}{extra}")

    print(f"measure uses: {n} — every use counted once below; governed references "
          f"counted alongside")
    line("governed references (MEASURE/AGG, approved view)", f["governed_references"])
    print(f"  positive verdicts are {f['positive_verdicts']}: a match is an "
          f"experimental comparison, a difference is suspected — nothing below is "
          f"a conclusion")
    line("suspected differences (named metric)", f["suspected_differences"])
    line("candidates (unnamed near misses)", f["candidates"])
    line("unregistered", f["unregistered"])
    line("unverifiable", f["unverifiable"],
         "  " + " · ".join(f"{k} {v}" for k, v in f["unverifiable_by_reason"].items())
         if f["unverifiable_by_reason"] else "")
    line(f"below recurrence threshold (≥{result['min_runs']} runs; not judged)",
         f["below_threshold"])
    if f["experimental_contested"]:
        print(f"  (of the unverifiable: {f['experimental_contested']} matched one side "
              f"of authoritative definitions suspected to disagree)")
    if f["experimental_matches"]:
        shapes = f["experimental_matches_by_shape"]
        print(f"  (of the unverifiable: {f['experimental_matches']} matched under the "
              f"experimental rules — exact {shapes['exact']} · raw-table slice "
              f"{shapes['A']} · metric view {shapes['B']}; not conclusions)")
    print(f"rule set: {result['rule_set']} — not covered: "
          f"{'; '.join(result['excluded_semantics'])}")
    print()
    tags = {"conforming_use": "CONFORMING", "contested_use": "CONTESTED",
            "definition_violation": "VIOLATION", "suspected_difference": "SUSPECTED",
            "candidate_alt_definition": "CANDIDATE",
            "unregistered_recurring_measure": "UNREGISTERED",
            "unverifiable": "UNVERIFIABLE"}
    for f in result["findings"]:
        sev = f" sev {f['severity']:>3}" if f["severity"] else "        "
        names = ", ".join(f["names_used"]) or "(unnamed)"
        governed = " [governed ref]" if f.get("governed_reference") else ""
        print(f"  [{tags[f['kind']]:<12}]{sev}  uses {f['uses']:>4}  "
              f"{names}{governed}: {f['summary']}")
        for component, sides in (f.get("diff") or {}).items():
            # "reference", not "certified": the reference is the authoritative
            # definition named for this run, whatever the scan concluded.
            print(f"      {component}: reference {sides['reference']} · "
                  f"observed {sides['observed']}")
        if f.get("calculation_matches"):
            print(f"      its calculation: {f['calculation_matches']}")
        for note in f.get("suspected") or []:
            print(f"      suspected (not a verdict): {note}")
        who = [x for x in (", ".join(f.get("executed_by") or []),
                           ", ".join(f.get("assets") or [])) if x]
        if who:
            print(f"      run by: {' · '.join(who)}")
    if not result["findings"]:
        print("  no recurring measures met the threshold")
    print("\n  note: monitor is read-only — findings are reported, not "
          "recorded in the store")


def _cmd_propose(args: argparse.Namespace) -> int:
    from touchstone.services.propose import build_proposals, write_proposals

    store = _open_existing(args.db)
    scan = store.latest_scan()
    dialect = (scan or {}).get("dialect") or "duckdb"
    registry = None
    if args.dbt or args.sql_dir or args.views_path:
        from touchstone.adapters.catalog import load_catalog
        from touchstone.services.monitor import build_registry

        artifacts = []
        if args.dbt:
            from touchstone.adapters.dbt import load_dbt
            artifacts += load_dbt(args.dbt)
        if args.sql_dir:
            from touchstone.adapters.sql_dir import load_sql_dir
            artifacts += load_sql_dir(args.sql_dir, dialect)[0]
        if args.views_path:
            from touchstone.adapters.sql_dir import load_views_file
            artifacts += load_views_file(args.views_path, dialect)[0]
        catalog = load_catalog(args.catalog) if args.catalog else {}
        registry = build_registry(catalog, artifacts, dialect,
                              _scan_context(scan, args))
    elif args.fmt != "metrics":
        # Ossie and metric-view emission TRANSPILE (generic / Databricks
        # writers); without the registry there is no self-check, and an
        # unverified transpile has already been shown to change meaning.
        print(f"touchstone: --format {args.fmt} needs the estate sources (--dbt, "
              f"--sql-dir or --views, plus --catalog) so each emitted declaration "
              f"is round-trip checked before it is offered", file=sys.stderr)
        return 2
    result = build_proposals(store, fmt=args.fmt, concept=args.concept,
                             registry=registry, dialect=dialect)
    written = write_proposals(result, args.out_dir)
    kinds = {"resolution": 0, "promotion": 0}
    refused = matched = 0
    for p in result["proposals"]:
        kinds[p["kind"]] += 1
        refused += sum(1 for c in p["candidates"]
                       if c["declaration"] is None and not c["matches_declaration"])
        matched += sum(1 for c in p["candidates"] if c["matches_declaration"])
    print(f"proposals against scan {result['scan_id']}: "
          f"{kinds['resolution']} suspected-divergence resolution(s), "
          f"{kinds['promotion']} shadow promotion(s)"
          + (f" · {matched} site(s) appear to match a declaration (experimental "
             f"comparison, not a conclusion)" if matched else "")
          + (f" · {refused} candidate(s) could not be emitted (reasons in memo)"
             if refused else "")
          + (" · round trips re-scanned (consistency check, not verification)"
             if registry is not None else
             " · round trips NOT checked (no estate sources)"))
    for path in written["declarations"]:
        print(f"  {path}")
    print(f"  memo: {written['memo']}")
    print("  nothing applied — review the memo, pick a direction per concept, "
          "and open it as a PR (e.g. gh pr create --body-file RESOLUTIONS.md)")
    return 0


def _cmd_oracle(args: argparse.Namespace) -> int:
    from touchstone.oracle import run_oracle
    from touchstone.toolchain import sqlglot_version

    report = run_oracle(full=args.full)
    print(f"execution oracle · sqlglot {sqlglot_version()} · {report.queries} queries · "
          f"{report.proof_grade} at proof grade · {report.buckets} shared-hash bucket(s) · "
          f"{report.pairs_executed} pair executions over {report.datasets} dataset(s)")
    for f in report.self_test_failures:
        print(f"  SELF-TEST FAILED ({f['why']}): {f['a']}  ||  {f['b']}")
    seen = set()
    for m in report.mismatches:
        if (m["a"], m["b"]) in seen:
            continue
        seen.add((m["a"], m["b"]))
        print(f"  FALSE EQUALITY [{m['estate']}/{m['dataset']}]\n    {m['a']}\n    {m['b']}\n"
              f"    -> {m['result_a']}  vs  {m['result_b']}")
    print(f"  proof forms ({report.proof_grade} queries): {report.transforms_executed} "
          f"executions of each query's trusted transformed SQL against the query as written")
    shown_t = set()
    for m in report.transform_mismatches:
        if m["original"] in shown_t:
            continue
        shown_t.add(m["original"])
        print(f"  PROOF FORM DIFFERS [{m['estate']}/{m['dataset']}]\n    {m['original']}\n"
              f"    {m['transformed']}\n"
              f"    -> {m['result_original']}  vs  {m['result_transformed']}")
    checks = report.parse_checks
    print(f"  parser-disagreement detector (DuckDB {report.queries} originals): "
          + " · ".join(f"{k} {v}" for k, v in sorted(checks.items()))
          + " — detects disagreement only; no disagreement found qualifies nothing")
    for m in report.parse_disagreements:
        print(f"  PARSER DISAGREEMENT AT PROOF GRADE [{m['estate']}]\n    {m['sql']}\n"
              f"    {m['detail']}")
    if report.ok:
        print("  no false equalities: every pair the experimental rules equate executed identically, "
              "and every proof form executed as its original")

    from touchstone.oracle import run_conformance_oracle

    conf = run_conformance_oracle(full=args.full)
    print(f"conformance challenger · {conf.queries} monitor cases · {conf.conforming} "
          f"judged conforming, each executed against its definition + variation "
          f"({conf.executions} executions) · {conf.near_misses} near misses")
    for f in conf.self_test_failures:
        print(f"  SELF-TEST FAILED ({f['why']}): {f['sql']}")
    for m in conf.near_miss_conformed:
        print(f"  NEAR MISS CONFORMED [{m['authority']}]: {m['sql']}")
    for m in conf.false_accusations:
        print(f"  FALSE ACCUSATION [{m['authority']}]: {m['sql']}")
    shown = set()
    for m in conf.mismatches:
        if m["observed"] in shown:
            continue
        shown.add(m["observed"])
        print(f"  CONFORMING BUT DIFFERENT [{m['authority']}/{m['dataset']}]\n"
              f"    {m['observed']}\n    {m['expected']}\n"
              f"    -> {m['result_observed']}  vs  {m['result_expected']}")
    if conf.ok:
        print("  every conforming verdict executed identically to its definition + "
              "variation; no near miss conformed. This challenges the rules on "
              "these datasets — it does not prove them")
    if report.ok and conf.ok:
        return 0
    print(f"\nFAILED: {len(seen)} false certification(s), "
          f"{len(report.self_test_failures) + len(conf.self_test_failures)} self-test "
          f"failure(s), {len(shown)} conforming-but-different, "
          f"{len(conf.near_miss_conformed)} near miss(es) conformed, "
          f"{len(conf.false_accusations)} false accusation(s)", file=sys.stderr)
    return 1


def _scan_context(scan: dict | None, args: argparse.Namespace) -> dict:
    """Execution context for a registry rebuilt after the scan: the scan's
    recorded context, overridden only by flags passed explicitly."""
    recorded = {}
    if scan and scan.get("stats_json"):
        recorded = (json.loads(scan["stats_json"]) or {}).get("execution_context") or {}
    return {
        "default_database": getattr(args, "default_database", None) or recorded.get("default_database"),
        "default_schema": getattr(args, "default_schema", None) or recorded.get("default_schema"),
    }


def _cmd_catalog(args: argparse.Namespace) -> int:
    from touchstone.adapters.infoschema import generate_catalog, write_catalog_yaml
    from touchstone.warehouse import connect_warehouse

    wh = connect_warehouse(args.warehouse_url)
    try:
        catalog = generate_catalog(wh)
    finally:
        wh.close()
    tables, columns = write_catalog_yaml(catalog, args.out_path)
    print(f"catalog: {tables} table(s), {columns} column(s) -> {args.out_path}")
    return 0


def _cmd_pubkey(args: argparse.Namespace) -> int:
    from touchstone.signing import ensure_keypair

    # A typo'd --db silently CREATED a store and minted a fresh key, which the
    # operator then published as the pin — after which every real receipt fails
    # verification, and the failure looks like tampering rather than a typo.
    if not Path(args.db).exists():
        from touchstone.store import former_store

        print("touchstone: " + (former_store(args.db) or f"no store at {args.db} — pubkey "
              f"reads an existing store's signing key and will not create one"),
              file=sys.stderr)
        return 2
    keypair = ensure_keypair(Store(args.db))
    if keypair is None:
        print("no crypto backend (pip install cryptography)", file=sys.stderr)
        return 2
    key_id, _, pub = keypair
    print(f"{key_id} {pub}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
