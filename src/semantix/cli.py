"""semantix CLI.

    semantix scan (--dbt <dir> | --sql-dir <dir> | --views <dump.sql>)
                  [--metrics m.yml] [--agents-schema export.json|agents.duckdb]
                  [--osi osi/|osi_document.json] [--catalog c.yml]
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
    p_scan.add_argument("--catalog", help="physical catalog YAML (schema.table.column: type)")
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
        help="verify a receipt's Ed25519 signature offline (no store needed)",
    )
    p_verify.add_argument("receipt", help="path to a receipt JSON file, or - for stdin")
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
    }[args.command]
    try:
        return handler(args)
    except (ValueError, FileNotFoundError) as e:
        # Intake problems the adapters have already described precisely — a
        # missing manifest, a truncated JSON file, a YAML of the wrong shape,
        # an unsupported warehouse URL. They arrived as raw tracebacks, which
        # is the least actionable way to tell an operator their path is wrong.
        print(f"semantix: {e}", file=sys.stderr)
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
    )
    stats = result.stats()
    print(f"scan {result.scan_id} · {result.observed_at}")
    print(
        f"  artifacts {stats['artifacts']} · fingerprints {stats['fingerprints']} · "
        f"claims {stats['claims']} · concepts {stats['concepts']} · "
        f"canonicalization coverage {stats['coverage_pct']}%"
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
        print(f"\nFAILED: proof-grade coverage {stats['coverage_pct']}% is below the "
              f"required {args.min_coverage}% — most of this estate could not be "
              f"canonicalized, so its verdicts do not describe it", file=sys.stderr)
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
        print(f"  RESOLVED [{v['verdict']:<13}]          {v['concept_label']}")
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

    from semantix.signing import receipt_trust

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
    trusted, warnings = receipt_trust(receipt)
    for warning in warnings:
        print(f"  WARNING: {warning}", file=sys.stderr)
    if not trusted and not warnings:
        print("  WARNING: receipt failed the trust evaluation", file=sys.stderr)
    return 0 if trusted else 1


def _cmd_mcp(args: argparse.Namespace) -> int:
    from semantix.mcp import serve

    return serve(args.db, estate=_estate_kwargs(args))


def _cmd_publish(args: argparse.Namespace) -> int:
    from semantix.services.writeback import publish_receipts

    try:
        summary = publish_receipts(_open_existing(args.db), args.warehouse)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"published {summary['receipts']} receipt(s) + skill/verify-metric "
          f"→ agents.root in {summary['warehouse']} (scan {summary['scan_id']})")
    for key in summary["keys"]:
        print(f"  semantix · {key}")
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
