"""semantix CLI.

    semantix scan (--dbt <dir> | --sql-dir <dir> | --views <dump.sql>)
                  [--metrics m.yml] [--agents-schema export.json|agents.duckdb]
                  [--osi osi/|osi_document.json] [--uc-metrics views/]
                  [--catalog c.yml]
                  [--execute seed.sql]
                  [--db semantix.db] [--report out.html] [--json out.json]
                  [--dialect duckdb] [--label "..."] [--fail-on contradiction]
    semantix publish   --db semantix.db --warehouse agents.duckdb
    semantix diff      --db semantix.db [--markdown pr.md] [--fail-on-new contradiction]
    semantix concepts  --db semantix.db
    semantix history   --db semantix.db <concept>
    semantix asof      --db semantix.db <concept> --at 2026-03-01T00:00:00Z
    semantix attest    --db semantix.db <concept> [--as-of TS]
                       [--dbt <dir> | --sql-dir <dir> | --views <dump.sql>]
                       [--metrics m.yml] [--agents-schema ...] [--osi ...]   # freshness check
    semantix bench     [--dialects duckdb,databricks,snowflake,bigquery]
    semantix oracle    [--full]          # execute every pair the engine would certify equal
    semantix catalog   --warehouse duckdb:///wh.db --out catalog.yml
    semantix propose   --db semantix.db --out proposals/ [--format metrics|ossie|uc-metrics]
    semantix monitor   --db semantix.db --history-file history.sql
                       [--dbt <dir>] [--catalog c.yml] [--min-runs 2]
                       [--fail-on-alt]        # alt definitions in the query stream
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from semantix import __version__
from semantix.engine import run_scan
from semantix.models import VerdictType
from semantix.report.html import render_report, report_json
from semantix.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="semantix", description=__doc__)
    parser.add_argument("--version", action="version", version=f"semantix {__version__}")
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

    p_scan.add_argument("--db", default="semantix.db")
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
    p_diff.add_argument("--db", default="semantix.db")
    p_diff.add_argument("--scan", help="scan id to evaluate (default: most recent)")
    p_diff.add_argument("--baseline", help="baseline scan id (default: previous scan)")
    p_diff.add_argument("--markdown", help="write a PR-ready markdown comment here")
    p_diff.add_argument(
        "--fail-on-new",
        choices=["contradiction", "any"],
        help="non-zero exit only for *newly introduced* verdicts",
    )

    p_concepts = sub.add_parser("concepts", help="list concepts and verdicts from the latest scan")
    p_concepts.add_argument("--db", default="semantix.db")

    p_history = sub.add_parser("history", help="derived valid-time history of a concept")
    p_history.add_argument("concept")
    p_history.add_argument("--db", default="semantix.db")

    p_asof = sub.add_parser("asof", help="what did this concept mean at a point in time")
    p_asof.add_argument("concept")
    p_asof.add_argument("--at", required=True, help="ISO timestamp, e.g. 2026-03-01T00:00:00Z")
    p_asof.add_argument("--db", default="semantix.db")

    p_attest = sub.add_parser("attest", help="issue a signed semantic receipt for a concept")
    p_attest.add_argument("concept")
    p_attest.add_argument("--as-of", dest="as_of")
    p_attest.add_argument("--db", default="semantix.db")
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
    p_verify.add_argument("--db", default="semantix.db",
                          help="store holding the receipt's scan (for the live re-check)")
    _add_estate_args(p_verify)
    p_verify.add_argument(
        "--public-key",
        help="pin the expected signer (base64url Ed25519); omit to check "
             "tamper-evidence against the embedded key",
    )

    p_pubkey = sub.add_parser("pubkey", help="print this store's receipt-signing public key")
    p_pubkey.add_argument("--db", default="semantix.db")

    p_mcp = sub.add_parser(
        "mcp",
        help="serve attest/asof/history/concepts as MCP tools over stdio "
             "(agents check receipts in the answer path)",
    )
    p_mcp.add_argument("--db", default="semantix.db")
    # Optional estate sources: when configured, every attest re-checks
    # freshness against the live estate before answering.
    _add_estate_args(p_mcp)

    p_bench = sub.add_parser(
        "bench",
        help="open equivalence benchmark across a SQL-dialect matrix: detection "
             "rate, known gaps, and the zero-false-certification gate",
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
        help="write signed receipts into a warehouse's AGENTS.ROOT "
             "(write-back certification into Agents Schema)",
    )
    p_publish.add_argument("--db", default="semantix.db")
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
    p_mine.add_argument("--db", default="semantix.db")
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
    p_verify_rel.add_argument("--db", default="semantix.db")

    p_rel = sub.add_parser("relationships",
                           help="the relationship graph: certified / failed / "
                                "candidate / shadow / orphan edges")
    p_rel.add_argument("--table", help="only edges touching this relation")
    p_rel.add_argument("--json", dest="json_out", help="write full graph JSON here")
    p_rel.add_argument("--db", default="semantix.db")

    p_oracle = sub.add_parser(
        "oracle",
        help="execute every query pair the engine would certify as equal "
             "against adversarial DuckDB datasets; exits 1 on any disagreement")
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

    p_prop.add_argument("--db", default="semantix.db")

    p_mon = sub.add_parser(
        "monitor",
        help="judge executed SQL against authoritative metric definitions: "
             "conforming uses, definition violations, and what could not be read")
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
    p_mon.add_argument("--fail-on-alt", dest="fail_on_alt", action="store_true",
                       help="exit 1 when a definition violation is found (candidates "
                            "and unverifiable uses never fail the gate)")
    p_mon.add_argument("--db", default="semantix.db")

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
        print(f"semantix: {e}", file=sys.stderr)
        return 2
    except duckdb_errors as e:
        # A nonexistent, locked, or non-DuckDB warehouse file, or the SQLite
        # store passed as a warehouse — the driver's own message is precise;
        # a traceback around it is not.
        print(f"semantix: warehouse error — {e}", file=sys.stderr)
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
    parser.add_argument("--dialect", default="duckdb")


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
    }
    if not any(estate.values()):
        return None
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
            print(f"    !! {ex['collisions']} canonical equality(ies) FALSIFIED by "
                  "the data — a certification was withdrawn")
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
              f"estate are below proof grade, so its verdicts do not describe it",
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
        bad = {VerdictType.CONTRADICTION}
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
    from semantix.services.diff import diff_verdicts, markdown_comment

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
        tag = "LOST    " if v["verdict"] in ("CERTIFIED", "ALIASING") else "RESOLVED"
        print(f"  {tag} [{v['verdict']:<13}]          {v['concept_label']}")
    if not diff["new"] and not diff["resolved"]:
        print("  no verdict changes vs baseline")
    print(f"  ({diff['unchanged']} pre-existing verdict(s) unchanged)")

    if args.markdown:
        out = Path(args.markdown)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown_comment(diff))
        print(f"comment → {out}")

    if args.fail_on_new:
        bad = {VerdictType.CONTRADICTION.value}
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
        raise ValueError(f"no store at {db} — this command reads an existing "
                         f"store and will not create one")
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
        from semantix.services.freshness import check_freshness

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
    from semantix.signing import assess_receipt
    # The freshness block attest just computed IS a live check, made now
    # against the scan this receipt was issued from.
    print("reliance: " + assess_receipt(receipt.as_dict(), receipt.freshness).reliance(),
          file=sys.stderr)
    return 0


def _cmd_verify_receipt(args: argparse.Namespace) -> int:
    from semantix.signing import verify_receipt_dict

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

    from semantix.signing import assess_receipt

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
        print(f"  status {receipt.get('status')} · verified as of {verified_as_of}"
              + (f" · freshness {fresh_status}" if fresh_status else ""))
    live = None
    estate = _estate_kwargs(args)
    if estate is not None and isinstance(receipt.get("scan_id"), str):
        from semantix.services.freshness import check_freshness

        store = _open_existing(args.db)
        estate.pop("dialect", None)   # the receipt's scan records its own
        live = check_freshness(store, scan_id=receipt["scan_id"], **estate)
        store.close()
        print(f"  live re-check against scan {receipt['scan_id']}: {live.get('status')}"
              + (f" — {live['note']}" if live.get("note") else ""))
    a = assess_receipt(receipt, live)
    print(f"  reliance: {a.reliance()}")
    for warning in a.warnings:
        print(f"  WARNING: {warning}", file=sys.stderr)
    if getattr(args, "require_current", False):
        # Pipelines gating CURRENT reliance must not pass on a receipt that is
        # only valid for a past instant.
        return 0 if a.current_use else 1
    return 0 if a.valid_for_stated_use else 1


def _cmd_mcp(args: argparse.Namespace) -> int:
    from semantix.mcp import serve

    return serve(args.db, estate=_estate_kwargs(args))


def _cmd_publish(args: argparse.Namespace) -> int:
    from semantix.services.writeback import publish_receipts

    store = _open_existing(args.db)
    freshness = None
    estate = _estate_kwargs(args)
    if estate:
        from semantix.services.freshness import check_freshness
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
        print(f"  semantix · {key}")
    print(f"  semantix.verdicts · {summary['verdict_rows']} row(s) — gate on "
          f"them via dbt tests (integrations/dbt/semantix), warehouse alerts, "
          f"or asset checks (docs/action-rails.md)")
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    from semantix.bench import run_bench

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
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
        print(f"  json → {out}")
    if result["false_certifications"]:
        print(f"  FALSE CERTIFICATIONS: {', '.join(result['false_certifications'])} "
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
    print("  false certifications: 0 ✓")
    return 0


def _cmd_mine_relationships(args: argparse.Namespace) -> int:
    from semantix.adapters.catalog import load_catalog, load_declared_relationships
    from semantix.extraction.registry import SchemaRegistry
    from semantix.services.relationships import (
        graph_with_status, mine_sql_text, record_declared, record_mined,
    )

    if not args.history_file and not (args.warehouse_url and args.history_query):
        print("mine-relationships needs --history-file, or --warehouse with "
              "--history-query", file=sys.stderr)
        return 2

    if args.history_file:
        text = Path(args.history_file).read_text()
        source = args.history_file
    else:
        from semantix.warehouse import connect_warehouse

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
    from semantix.services.relationships import verify_edges
    from semantix.warehouse import DuckDBWarehouse, connect_warehouse

    if not args.warehouse_url and not args.seed_path:
        print("verify-relationships needs --warehouse URL or --seed seed.sql",
              file=sys.stderr)
        return 2

    if args.warehouse_url:
        warehouse = connect_warehouse(args.warehouse_url)
    else:
        artifacts = []
        if args.dbt:
            from semantix.adapters.dbt import load_dbt

            artifacts = load_dbt(args.dbt)
        warehouse = DuckDBWarehouse.ephemeral(
            Path(args.seed_path).read_text(), artifacts,
            label=f"ephemeral:{args.seed_path}",
        )

    declared_pks = None
    if args.catalog:
        from semantix.adapters.catalog import load_primary_keys

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
    # Count by actual status. Deriving "certified" as everything-not-failed
    # counted every edge the warehouse could not answer for as a success.
    certified = [r for r in results if r["status"] == "certified"]
    failed = [r for r in results if r["status"] == "failed"]
    unverified = [r for r in results if r["status"] not in ("certified", "failed")]
    print(f"\n  checked {len(results)} edge(s): {len(certified)} certified, "
          f"{len(failed)} failed, {len(unverified)} unverified")
    return 0


def _cmd_relationships(args: argparse.Namespace) -> int:
    from semantix.services.relationships import graph_with_status

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
        queries = r["mined_queries"]
        used = f"{queries} quer{'y' if queries == 1 else 'ies'}" if queries else "never used"
        print(f"  [{r['status'].upper():<10}] {r['usage']:<14} {r['label']}  · {used}"
              + ("  · competing keys" if r.get("competing_keys") else "")
              + detail)


def _cmd_monitor(args: argparse.Namespace) -> int:
    from semantix.adapters.catalog import load_catalog
    from semantix.services.history import from_records, read_history
    from semantix.services.monitor import build_registry, monitor_history

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
        print("semantix: no scans recorded — monitor compares the query "
              "stream against a scan's reference set; run `semantix scan` "
              "first", file=sys.stderr)
        return 2
    dialect = args.dialect or scan.get("dialect") or "duckdb"

    pilot = None
    if args.pilot_dir:
        from semantix.services.pilot import load_pilot

        pilot = load_pilot(args.pilot_dir, dialect)
        intake = read_history(pilot.history)
    elif args.history_file:
        intake = read_history(args.history_file)
    else:
        from semantix.warehouse import connect_warehouse

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
        from semantix.adapters.dbt import load_dbt
        artifacts += load_dbt(args.dbt)
    if args.sql_dir:
        from semantix.adapters.sql_dir import load_sql_dir
        artifacts += load_sql_dir(args.sql_dir, dialect)[0]
    if args.views_path:
        from semantix.adapters.sql_dir import load_views_file
        artifacts += load_views_file(args.views_path, dialect)[0]
    scan_read_models = any(r["kind"] == "computed"
                           for r in store.scan_snapshot(scan["scan_id"]))
    if not artifacts and scan_read_models:
        # Without the scan's models, observed lineage resolves to different
        # terminals than the reference cores' and byte-exact reruns of the
        # estate's own marts were accused. A registry the scan did not have
        # is not a degraded mode; it is a different comparison.
        print("semantix: monitor needs the estate the scan read (--dbt, "
              "--sql-dir or --views) so observed queries resolve through the "
              "same models the reference definitions did", file=sys.stderr)
        return 2
    catalog_path = args.catalog or (pilot.catalog if pilot and pilot.catalog else None)
    catalog = load_catalog(catalog_path) if catalog_path else {}
    context = _scan_context(scan, args)
    registry = build_registry(catalog, artifacts, dialect, context)
    asset_map = {}
    if args.asset_map:
        from semantix.adapters.loadutil import load_yaml

        asset_map = {str(k): str(v) for k, v in load_yaml(args.asset_map).items()}
    authority = ({s.strip() for s in args.authority.split(",") if s.strip()}
                 if args.authority else None)

    result = monitor_history(store, intake, registry, dialect, min_runs=args.min_runs,
                             authority_systems=authority, pilot=pilot,
                             context=context, assert_context=args.assert_context,
                             asset_map=asset_map)
    _print_monitor(result)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2))
        print(f"  findings JSON: {args.json_out}")
    # Only VIOLATIONS fail the gate: a candidate is a hypothesis by
    # construction, and failing CI on a hypothesis is the alert-fatigue path.
    violations = [f for f in result["findings"] if f["kind"] == "definition_violation"]
    if args.fail_on_alt and violations:
        print(f"\nsemantix monitor: FAILED — {len(violations)} definition "
              f"violation(s) in use", file=sys.stderr)
        return 1
    return 0


def _print_monitor(result: dict) -> None:
    ref = result["reference"]
    print(f"monitor · against scan {result['scan_id']} · authority "
          f"{', '.join(ref['authority_systems']) or 'none'}: {ref['definitions']} "
          f"definition(s) across {ref['concepts']} concept(s)"
          + (f" ({ref['excluded_below_proof']} below proof grade excluded)"
             if ref["excluded_below_proof"] else "")
          + (f" · contested: {', '.join(ref['contested_concepts'])}"
             if ref["contested_concepts"] else ""))
    print(f"comparison: {result['comparison']}")
    if result["toolchain"]["positive_verdicts_withheld"]:
        print("toolchain: positive verdicts WITHHELD — the parser is not a qualified "
              "version (see `semantix/toolchain.py`)")
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
    f = result["funnel"]
    n = f["measure_uses"]

    def line(label: str, count: int, extra: str = "") -> None:
        pct = f" ({100 * count / n:.0f}%)" if n else ""
        print(f"  {label:<50} {count:>6}/{n}{pct}{extra}")

    print(f"measure uses: {n} — every use counted once below; governed references "
          f"counted alongside")
    line("governed references (MEASURE/AGG, approved view)", f["governed_references"])
    shapes = f["conforming_by_shape"]
    line("conforming", f["conforming"],
         f"  exact {shapes['exact']} · raw-table slice {shapes['A']} · "
         f"metric view {shapes['B']}" if f["conforming"] else "")
    if f["contested"]:
        line("contested (authorities disagree)", f["contested"])
    line("definition violations", f["violations"])
    line("candidates (unnamed near misses)", f["candidates"])
    line("unregistered", f["unregistered"])
    line("unverifiable", f["unverifiable"],
         "  " + " · ".join(f"{k} {v}" for k, v in f["unverifiable_by_reason"].items())
         if f["unverifiable_by_reason"] else "")
    line(f"below recurrence threshold (≥{result['min_runs']} runs; not judged)",
         f["below_threshold"])
    line("checked = conforming + violations", f["checked"])
    print(f"rule set: {result['rule_set']} — not covered: "
          f"{'; '.join(result['excluded_semantics'])}")
    print()
    tags = {"conforming_use": "CONFORMING", "contested_use": "CONTESTED",
            "definition_violation": "VIOLATION",
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
    from semantix.services.propose import build_proposals, write_proposals

    store = _open_existing(args.db)
    scan = store.latest_scan()
    dialect = (scan or {}).get("dialect") or "duckdb"
    registry = None
    if args.dbt or args.sql_dir or args.views_path:
        from semantix.adapters.catalog import load_catalog
        from semantix.services.monitor import build_registry

        artifacts = []
        if args.dbt:
            from semantix.adapters.dbt import load_dbt
            artifacts += load_dbt(args.dbt)
        if args.sql_dir:
            from semantix.adapters.sql_dir import load_sql_dir
            artifacts += load_sql_dir(args.sql_dir, dialect)[0]
        if args.views_path:
            from semantix.adapters.sql_dir import load_views_file
            artifacts += load_views_file(args.views_path, dialect)[0]
        catalog = load_catalog(args.catalog) if args.catalog else {}
        registry = build_registry(catalog, artifacts, dialect,
                              _scan_context(scan, args))
    elif args.fmt != "metrics":
        # Ossie and metric-view emission TRANSPILE (generic / Databricks
        # writers); without the registry there is no self-check, and an
        # unverified transpile has already been shown to change meaning.
        print(f"semantix: --format {args.fmt} needs the estate sources (--dbt, "
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
          f"{kinds['resolution']} contradiction resolution(s), "
          f"{kinds['promotion']} shadow promotion(s)"
          + (f" · {matched} site(s) already match a declaration" if matched else "")
          + (f" · {refused} candidate(s) could not be emitted (reasons in memo)"
             if refused else "")
          + (" · round trips verified" if registry is not None else
             " · round trips NOT verified (no estate sources)"))
    for path in written["declarations"]:
        print(f"  {path}")
    print(f"  memo: {written['memo']}")
    print("  nothing applied — review the memo, pick a direction per concept, "
          "and open it as a PR (e.g. gh pr create --body-file RESOLUTIONS.md)")
    return 0


def _cmd_oracle(args: argparse.Namespace) -> int:
    from semantix.oracle import run_oracle
    from semantix.toolchain import sqlglot_version

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
        print(f"  FALSE CERTIFICATION [{m['estate']}/{m['dataset']}]\n    {m['a']}\n    {m['b']}\n"
              f"    -> {m['result_a']}  vs  {m['result_b']}")
    if report.ok:
        print("  no false certifications: every pair the engine equates executed identically")

    from semantix.oracle import run_conformance_oracle

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
    from semantix.adapters.infoschema import generate_catalog, write_catalog_yaml
    from semantix.warehouse import connect_warehouse

    wh = connect_warehouse(args.warehouse_url)
    try:
        catalog = generate_catalog(wh)
    finally:
        wh.close()
    tables, columns = write_catalog_yaml(catalog, args.out_path)
    print(f"catalog: {tables} table(s), {columns} column(s) -> {args.out_path}")
    return 0


def _cmd_pubkey(args: argparse.Namespace) -> int:
    from semantix.signing import ensure_keypair

    # A typo'd --db silently CREATED a store and minted a fresh key, which the
    # operator then published as the pin — after which every real receipt fails
    # verification, and the failure looks like tampering rather than a typo.
    if not Path(args.db).exists():
        print(f"semantix: no store at {args.db} — pubkey reads an existing "
              f"store's signing key and will not create one", file=sys.stderr)
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
