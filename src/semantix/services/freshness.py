"""Receipt freshness: verification decays honestly.

A verdict is a statement about the estate *at scan time*; `attest` can be
called long after. Without a check, a CERTIFIED receipt keeps certifying an
estate that has since changed — precisely the "context that may not be true
anymore" failure this product exists to catch, applied to itself.

Given the estate's current sources, this service re-hashes them with the
same conventions the scan used and compares against the snapshot the scan
recorded (`scan_artifacts`). Four statuses, in honesty order:

  fresh    every artifact in every re-checked system matches the snapshot
  partial  re-checked systems are clean, but some scanned systems were not
           re-checked (never claim more than was verified)
  stale    at least one artifact changed, appeared, or disappeared
  unknown  nothing to compare — no sources passed, or the scan predates
           snapshot recording

Only systems present in the re-load are diffed: passing --dbt alone never
counts un-re-checked metrics claims as "removed". The resulting block is
embedded in the receipt BEFORE signing, so "fresh as of T" is itself
tamper-evident.
"""

from __future__ import annotations

import hashlib
import json

from semantix.adapters.agents_schema import load_agents_schema
from semantix.adapters.base import spec_content_text
from semantix.adapters.catalog import catalog_sha
from semantix.adapters.dbt import load_dbt_detailed
from semantix.adapters.metrics_yaml import load_metrics
from semantix.adapters.sql_dir import load_sql_dir, load_views_file
from semantix.extraction.proof import POLICY_ID
from semantix.store import Store, scan_policy, utcnow


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# Which artifact `system` values each estate source produces — how a passed
# source declares "I re-checked these systems", including when the source now
# yields zero artifacts (an emptied file is drift, not an unchecked system).
SOURCE_SYSTEMS: dict[str, frozenset[str]] = {
    # dbt_metrics too: the manifest carries BOTH the models and the Semantic
    # Layer declarations. Mapping dbt_path to {"dbt"} alone meant declaration
    # drift in the manifest was PERMANENTLY invisible — no flag existed that
    # could ever re-check `dbt_metrics`, so a receipt kept verifying after the
    # declared metric's meaning changed, "partial" forever, from a file the
    # checker itself had just re-opened.
    "dbt_path": frozenset({"dbt", "dbt_metrics"}),
    "sql_dir": frozenset({"sql"}),
    "views_path": frozenset({"views"}),
    "metrics_path": frozenset({"metrics_yaml"}),
    "agents_schema_path": frozenset({"agents_osi", "agents_lookml"}),
    "osi_path": frozenset({"ossie"}),
    "uc_metrics_path": frozenset({"uc_metrics"}),
    "catalog_path": frozenset({"catalog"}),
}

# A source file the loaders enumerated but could not read. Freshness re-hashes
# with the scan's own loaders, so a file that turns unreadable after the scan
# (non-UTF-8, a malformed directive) is drift — it was silently ignored, and a
# receipt stayed "fresh" while a rescan refused to certify.
UNREADABLE = "<unreadable>"


def current_estate_shas(
    dbt_path: str | None = None,
    sql_dir: str | None = None,
    views_path: str | None = None,
    metrics_path: str | None = None,
    agents_schema_path: str | None = None,
    osi_path: str | None = None,
    uc_metrics_path: str | None = None,
    dialect: str = "duckdb",
) -> dict[tuple[str, str], set[str]]:
    """(system, object_id) -> content shas for the estate as it is right now.

    Values are sets because one identity can legitimately carry several
    contents (e.g. two declared metrics with the same name). Hash conventions
    MUST mirror engine.run_scan's record_artifact calls: computed artifacts
    hash their SQL, declared specs hash spec_content_text().
    """
    shas: dict[tuple[str, str], set[str]] = {}

    computed = []
    unreadable: list[tuple[str, dict]] = []
    if dbt_path:
        arts, skipped = load_dbt_detailed(dbt_path)
        computed += arts
        unreadable += [("dbt", s) for s in skipped]
    if sql_dir:
        arts, skipped = load_sql_dir(sql_dir, dialect)
        computed += arts
        unreadable += [("sql", s) for s in skipped]
    if views_path:
        arts, skipped = load_views_file(views_path, dialect)
        computed += arts
        unreadable += [("views", s) for s in skipped]
    for artifact in computed:
        shas.setdefault((artifact.system, artifact.object_id), set()).add(_sha(artifact.sql))

    specs = []
    if dbt_path:
        from semantix.adapters.dbt import load_dbt_metrics
        specs += load_dbt_metrics(dbt_path)
    if metrics_path:
        specs += load_metrics(metrics_path)
    if agents_schema_path:
        specs += load_agents_schema(agents_schema_path)
    if osi_path:
        from semantix.adapters.ossie import load_ossie
        loaded, skipped = load_ossie(osi_path, dialect)
        specs += loaded
        unreadable += [("ossie", s) for s in skipped]
    if uc_metrics_path:
        from semantix.adapters.uc_metrics import load_uc_metrics
        loaded, skipped = load_uc_metrics(uc_metrics_path, dialect)
        specs += loaded
        unreadable += [("uc_metrics", s) for s in skipped]
    for spec in specs:
        shas.setdefault((spec.system, spec.name), set()).add(_sha(spec_content_text(spec)))
    for system, s in unreadable:
        if not s.get("informational"):
            shas.setdefault((system, s["object_id"]), set()).add(UNREADABLE)

    return shas


def check_freshness(
    store: Store,
    dbt_path: str | None = None,
    sql_dir: str | None = None,
    views_path: str | None = None,
    metrics_path: str | None = None,
    agents_schema_path: str | None = None,
    osi_path: str | None = None,
    uc_metrics_path: str | None = None,
    catalog_path: str | None = None,
    dialect: str | None = None,
    scan_id: str | None = None,
) -> dict:
    """Freshness block: does the estate, as it is now, match a scan's snapshot?

    Against the store's latest scan by default (a receipt being ISSUED), or
    against `scan_id` — the scan a receipt being VERIFIED was issued from, so
    current reliance is bound to that receipt's own evidence rather than to
    whatever was scanned since.

    dialect=None re-checks with the scan's own recorded dialect — re-hashing a
    views estate under a different dialect would report spurious drift.
    """
    checked_at = utcnow()
    if scan_id is not None:
        scan = store.finished_scan(scan_id)
        if scan is None:
            return {"status": "unknown", "checked_at": checked_at, "scan_id": scan_id,
                    "note": f"scan {scan_id} is not in this store — the receipt's "
                            f"evidence cannot be re-checked here"}
    else:
        scan = store.latest_scan()
    if scan is None:
        return {"status": "unknown", "checked_at": checked_at,
                "note": "no scans recorded in this store"}
    dialect = dialect or scan.get("dialect") or "duckdb"

    base = {
        "scan_id": scan["scan_id"],
        "verified_as_of": scan["started_at"],
        "checked_at": checked_at,
    }

    # Unchanged sources do not make old verdicts current when the proof
    # policy that produced them is no longer in force: matching file hashes
    # say the INPUT is the same, not that this policy reaches the same
    # verdicts from it.
    policy = scan_policy(scan)
    if policy != POLICY_ID:
        return {**base, "status": "stale",
                "proof_policy": {"scan": policy, "current": POLICY_ID},
                "estate_drift": {"changed": [], "added": [], "removed": []},
                "unchecked_systems": [],
                "note": f"the scan's verdicts were computed under proof policy "
                        f"{policy or 'before semantix-proof/2'}; the policy in force "
                        f"is {POLICY_ID} — rescan to recertify"}

    snapshot = store.scan_snapshot(scan["scan_id"])
    if not snapshot:
        return {**base, "status": "unknown",
                "note": "this scan predates estate snapshots — rescan once "
                        "to enable freshness checks"}

    sources = {
        "dbt_path": dbt_path,
        "sql_dir": sql_dir,
        "views_path": views_path,
        "metrics_path": metrics_path,
        "agents_schema_path": agents_schema_path,
        "osi_path": osi_path,
        "uc_metrics_path": uc_metrics_path,
        "catalog_path": catalog_path,
    }
    checked_systems: set[str] = set()
    for source, value in sources.items():
        if value:
            checked_systems |= SOURCE_SYSTEMS[source]
    if not checked_systems:
        return {**base, "status": "unknown",
                "note": "no estate sources passed — freshness not re-checked"}

    try:
        current = current_estate_shas(dialect=dialect, **{
            k: v for k, v in sources.items() if k != "catalog_path"})
        if catalog_path:
            current[("catalog", "catalog")] = {catalog_sha(catalog_path)}
    except Exception as e:  # noqa: BLE001 — degrade, never crash an attest
        return {**base, "status": "unknown",
                "note": f"estate re-check failed ({type(e).__name__}: {e}) — "
                        "freshness could not be determined"}

    snapshot_shas: dict[tuple[str, str], set[str]] = {}
    for r in snapshot:
        snapshot_shas.setdefault((r["system"], r["object_id"]), set()).add(r["raw_sha"])
    # What the scan enumerated but could not read, as the loaders report it.
    for system, object_id in store.unreadable_artifacts(scan["scan_id"]):
        if (system, object_id) not in snapshot_shas:
            snapshot_shas[(system, object_id)] = {UNREADABLE}
    recorded_catalog = (json.loads(scan.get("stats_json") or "{}") or {}).get("catalog_sha")
    if recorded_catalog:
        snapshot_shas[("catalog", "catalog")] = {recorded_catalog}

    changed: list[str] = []
    removed: list[str] = []
    for (system, object_id), sha_set in sorted(snapshot_shas.items()):
        if system not in checked_systems:
            continue
        if (system, object_id) not in current:
            removed.append(f"{system}:{object_id}")
        elif current[(system, object_id)] != sha_set:
            changed.append(f"{system}:{object_id}")
    added = [
        f"{system}:{object_id}"
        for (system, object_id) in sorted(current)
        if (system, object_id) not in snapshot_shas
    ]
    unchecked = sorted({system for system, _ in snapshot_shas} - checked_systems)

    drift = {"changed": changed, "added": added, "removed": removed}
    if changed or added or removed:
        status = "stale"
        note = (f"estate has drifted since the scan: {len(changed)} changed, "
                f"{len(added)} added, {len(removed)} removed — rescan to re-verify")
    elif unchecked:
        status = "partial"
        note = ("re-checked systems match the snapshot; not re-checked: "
                + ", ".join(unchecked))
    else:
        status = "fresh"
        note = "estate matches the scanned snapshot"

    return {**base, "status": status, "estate_drift": drift,
            "unchecked_systems": unchecked, "note": note}
