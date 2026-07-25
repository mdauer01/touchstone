"""Bitemporal, append-only fingerprint store (§4 of the architecture spec).

Two invariants are enforced structurally rather than by convention:

* Append-only — every write path is an INSERT (INSERT OR IGNORE for
  content-addressed rows). Nothing is ever DELETEd, and the single UPDATE in
  the codebase is `finish_scan`'s write-once stats stamp, guarded by
  `stats_json IS NULL` so it can never overwrite. Semantic history — the
  observation log and everything derived from it — is INSERT-only without
  exception.
* Derived valid time — a definition's valid interval is never stored as a
  mutable column. It is computed at read time from the observation log: a
  fingerprint is live at a definition site from its first observation until
  a different fingerprint is first observed at the same site.

SQLite is the Phase 0 engine. The schema is deliberately portable SQL
(TEXT ids, ISO-8601 UTC timestamps, JSON payloads as TEXT) so the spec's
v0 Postgres target is a connection-string change, not a redesign.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from semantix.models import (
    Claim,
    Concept,
    Fingerprint,
    Receipt,
    Verdict,
    VerdictType,
    VERDICT_PRECEDENCE,
)
# One normalization function for the whole system. The store used to carry a
# private copy that disagreed with the resolver's on names containing digits
# or punctuation, so a concept the resolver had clustered could not be found
# again at attest time — yielding a SIGNED receipt with an empty
# definition_version and empty lineage.
from semantix.services.resolution import normalize_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    scan_id     TEXT PRIMARY KEY,
    label       TEXT,
    dialect     TEXT,
    started_at  TEXT NOT NULL,
    stats_json  TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    system      TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    relation    TEXT,
    path        TEXT,
    raw_sha     TEXT,
    canon_level TEXT,
    error       TEXT
);

CREATE TABLE IF NOT EXISTS fingerprints (
    fingerprint_id      TEXT PRIMARY KEY,
    concept_candidate   TEXT NOT NULL,
    semantic_hash       TEXT NOT NULL,
    relation            TEXT,
    dialect             TEXT,
    canon_level         TEXT,
    measure_expr        TEXT,
    filters_json        TEXT,
    grain_json          TEXT,
    join_nodes_json     TEXT,
    join_edges_json     TEXT,
    source_columns_json TEXT,
    canonical_sql       TEXT,
    provenance_json     TEXT
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT,
    owner           TEXT,
    model           TEXT,
    expr_raw        TEXT,
    expressible     INTEGER NOT NULL,
    semantic_hash   TEXT,
    canon_level     TEXT,
    measure_expr    TEXT,
    filters_json    TEXT,
    grain_json      TEXT,
    join_nodes_json TEXT,
    join_edges_json TEXT,
    source_columns_json TEXT,
    canonical_sql   TEXT,
    error           TEXT,
    provenance_json TEXT
);

-- The observation log: the only source of temporal truth.
-- record time = observed_at (when the engine saw it, per scan);
-- valid time  = derived interval between observations at one object_key.
CREATE TABLE IF NOT EXISTS observations (
    scan_id     TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- 'fingerprint' | 'claim'
    object_key  TEXT NOT NULL,          -- definition site
    ref_id      TEXT NOT NULL,
    PRIMARY KEY (scan_id, kind, object_key, ref_id)
);

CREATE TABLE IF NOT EXISTS concepts (
    scan_id      TEXT NOT NULL,
    concept_id   TEXT NOT NULL,
    label        TEXT NOT NULL,
    members_json TEXT NOT NULL,
    PRIMARY KEY (scan_id, concept_id)
);

CREATE TABLE IF NOT EXISTS verdicts (
    verdict_id    TEXT PRIMARY KEY,
    scan_id       TEXT NOT NULL,
    concept_id    TEXT NOT NULL,
    concept_label TEXT NOT NULL,
    verdict       TEXT NOT NULL,
    severity      INTEGER NOT NULL,
    summary       TEXT,
    evidence_json TEXT,
    created_at    TEXT NOT NULL
);

-- Which artifacts (by content hash) each scan observed: the estate snapshot
-- that receipt freshness checks compare against. Append-only.
CREATE TABLE IF NOT EXISTS scan_artifacts (
    scan_id     TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    PRIMARY KEY (scan_id, artifact_id)
);

CREATE TABLE IF NOT EXISTS receipts (
    receipt_id   TEXT PRIMARY KEY,
    concept      TEXT NOT NULL,
    as_of        TEXT,
    issued_at    TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

-- Receipt-signing keys (append-only; newest row signs). Phase 0 keeps the
-- private half here — single-tenant local file; production custody is a KMS.
CREATE TABLE IF NOT EXISTS signing_keys (
    key_id      TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    public_key  TEXT NOT NULL,
    private_key TEXT NOT NULL
);

-- Relationship layer: join edges as first-class, direction-free identities.
-- Same doctrine one level down — mined usage is a claim, declared FKs are
-- claims, and only warehouse checks upgrade either to verified.
CREATE TABLE IF NOT EXISTS relationship_edges (
    edge_id        TEXT PRIMARY KEY,   -- content hash of canonical endpoints
    left_relation  TEXT NOT NULL,
    left_column    TEXT NOT NULL,
    right_relation TEXT NOT NULL,
    right_column   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relationship_observations (
    observed_at TEXT NOT NULL,
    edge_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- 'mined' | 'declared'
    source      TEXT NOT NULL,          -- history file/warehouse | catalog path
    stats_json  TEXT,
    PRIMARY KEY (observed_at, edge_id, kind, source)
);

CREATE TABLE IF NOT EXISTS relationship_checks (
    check_id      TEXT PRIMARY KEY,
    edge_id       TEXT NOT NULL,
    checked_at    TEXT NOT NULL,
    warehouse     TEXT,
    passed        INTEGER NOT NULL,
    evidence_json TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ValidInterval:
    """Derived valid-time interval of one fingerprint at one definition site."""

    object_key: str
    ref_id: str
    concept_candidate: str
    semantic_hash: str
    valid_from: str
    valid_to: str | None  # None = still live
    relation: str | None = None


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ scans

    def begin_scan(self, label: str | None, dialect: str) -> tuple[str, str]:
        scan_id = "scan_" + uuid.uuid4().hex[:12]
        started_at = utcnow()
        self._conn.execute(
            "INSERT INTO scans (scan_id, label, dialect, started_at) VALUES (?, ?, ?, ?)",
            (scan_id, label, dialect, started_at),
        )
        self._conn.commit()
        return scan_id, started_at

    def finish_scan(self, scan_id: str, stats: dict) -> None:
        # stats are set exactly once at scan end; the row is otherwise immutable
        self._conn.execute(
            "UPDATE scans SET stats_json = ? WHERE scan_id = ? AND stats_json IS NULL",
            (json.dumps(stats), scan_id),
        )
        self._conn.commit()

    def latest_scan(self) -> dict | None:
        # rowid = insertion order: two scans inside the same second must not
        # tie-break on the random scan_id (recent_scans already does this).
        row = self._conn.execute(
            "SELECT * FROM scans ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def scans(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM scans ORDER BY started_at")]

    def scan_as_of(self, ts: str) -> dict | None:
        """The scan that was current at `ts` — the one whose verdicts described
        the estate at that instant. None if the store had not yet scanned."""
        row = self._conn.execute(
            "SELECT * FROM scans WHERE started_at <= ? ORDER BY rowid DESC LIMIT 1",
            (normalize_ts(ts),),
        ).fetchone()
        return dict(row) if row else None

    def recent_scans(self, limit: int = 2) -> list[dict]:
        """Most recent scans in insertion order (newest first). rowid breaks
        started_at ties, which matter in CI where two scans share a second."""
        rows = self._conn.execute(
            "SELECT * FROM scans ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- artifacts

    def record_artifact(
        self,
        kind: str,
        system: str,
        object_id: str,
        relation: str | None,
        path: str | None,
        raw_text: str,
        canon_level: str,
        error: str | None,
        scan_id: str | None = None,
    ) -> str:
        raw_sha = hashlib.sha256(raw_text.encode()).hexdigest()
        artifact_id = hashlib.sha256(f"{system}|{object_id}|{raw_sha}".encode()).hexdigest()
        self._conn.execute(
            """INSERT OR IGNORE INTO artifacts
               (artifact_id, kind, system, object_id, relation, path, raw_sha, canon_level, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (artifact_id, kind, system, object_id, relation, path, raw_sha, canon_level, error),
        )
        if scan_id is not None:
            self._conn.execute(
                "INSERT OR IGNORE INTO scan_artifacts VALUES (?, ?)",
                (scan_id, artifact_id),
            )
        return artifact_id

    def scan_snapshot(self, scan_id: str) -> list[dict]:
        """The estate as one scan observed it: every artifact's identity and
        content hash. Empty for scans that predate snapshot recording."""
        rows = self._conn.execute(
            """SELECT a.system, a.object_id, a.kind, a.relation, a.path, a.raw_sha
               FROM scan_artifacts sa
               JOIN artifacts a ON a.artifact_id = sa.artifact_id
               WHERE sa.scan_id = ?
               ORDER BY a.system, a.object_id""",
            (scan_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------- fingerprints

    def record_fingerprint(self, fp: Fingerprint, scan_id: str, observed_at: str) -> None:
        self._conn.execute(
            """INSERT OR IGNORE INTO fingerprints VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fp.fingerprint_id,
                fp.concept_candidate,
                fp.core.hash(),
                fp.relation,
                fp.dialect,
                fp.canon_level,
                fp.core.measure_expr,
                json.dumps(list(fp.core.filters)),
                json.dumps(list(fp.core.grain)),
                json.dumps(list(fp.core.join_nodes)),
                json.dumps(list(fp.core.join_edges)),
                json.dumps(list(fp.core.source_columns)),
                fp.canonical_sql,
                json.dumps(fp.provenance.as_dict()),
            ),
        )
        self._observe(scan_id, observed_at, "fingerprint", fp.object_key, fp.fingerprint_id)

    def record_claim(self, claim: Claim, scan_id: str, observed_at: str) -> None:
        core = claim.core
        self._conn.execute(
            """INSERT OR IGNORE INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                claim.claim_id,
                claim.name,
                claim.description,
                claim.owner,
                claim.model,
                claim.expr_raw,
                1 if claim.expressible else 0,
                core.hash() if core else None,
                claim.canon_level,
                core.measure_expr if core else None,
                json.dumps(list(core.filters)) if core else None,
                json.dumps(list(core.grain)) if core else None,
                json.dumps(list(core.join_nodes)) if core else None,
                json.dumps(list(core.join_edges)) if core else None,
                json.dumps(list(core.source_columns)) if core else None,
                claim.canonical_sql,
                claim.error,
                json.dumps(claim.provenance.as_dict()),
            ),
        )
        self._observe(scan_id, observed_at, "claim", claim.object_key, claim.claim_id)

    def _observe(self, scan_id: str, observed_at: str, kind: str, object_key: str, ref_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO observations VALUES (?, ?, ?, ?, ?)",
            (scan_id, observed_at, kind, object_key, ref_id),
        )

    # ------------------------------------------------------ concepts/verdicts

    def save_results(self, scan_id: str, concepts: list[Concept], verdicts: list[Verdict]) -> None:
        for concept in concepts:
            members = {
                "fingerprints": [fp.fingerprint_id for fp in concept.fingerprints],
                "claims": [cl.claim_id for cl in concept.claims],
            }
            # (scan_id, concept_id) is unique per scan, so this is a pure append;
            # OR IGNORE only makes a crashed-and-retried save idempotent.
            self._conn.execute(
                "INSERT OR IGNORE INTO concepts VALUES (?, ?, ?, ?)",
                (scan_id, concept.concept_id, concept.label, json.dumps(members)),
            )
        created_at = utcnow()
        for verdict in verdicts:
            verdict_id = "v_" + uuid.uuid4().hex[:12]
            self._conn.execute(
                "INSERT INTO verdicts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    verdict_id,
                    scan_id,
                    verdict.concept_id,
                    verdict.concept_label,
                    verdict.verdict_type.value,
                    verdict.severity,
                    verdict.summary,
                    json.dumps(verdict.evidence),
                    created_at,
                ),
            )
        self._conn.commit()

    def verdicts_for_scan(self, scan_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM verdicts WHERE scan_id = ? ORDER BY severity DESC, concept_label",
            (scan_id,),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["evidence"] = json.loads(d.pop("evidence_json") or "{}")
            out.append(d)
        return out

    def fingerprint_row(self, fingerprint_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM fingerprints WHERE fingerprint_id = ?", (fingerprint_id,)
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------ valid time

    def scan_coverage(self) -> list[tuple[str, str, set[str]]]:
        """[(scan_id, started_at, systems this scan actually read)], oldest
        first. A scan that read a system is entitled to assert ABSENCE within
        it; a scan that never looked at dbt says nothing about dbt."""
        covered: dict[str, set[str]] = {}
        for r in self._conn.execute(
            """SELECT sa.scan_id, a.system FROM scan_artifacts sa
               JOIN artifacts a ON a.artifact_id = sa.artifact_id"""
        ):
            covered.setdefault(r["scan_id"], set()).add(r["system"])
        return [
            (r["scan_id"], r["started_at"], covered.get(r["scan_id"], set()))
            for r in self._conn.execute("SELECT scan_id, started_at FROM scans ORDER BY rowid")
        ]

    def history(self, concept_label: str) -> list[ValidInterval]:
        """Derived valid-time intervals for every definition site whose
        measure name matches the concept label.

        Absence is evidence. A scan that read a site's system and did NOT find
        the definition there ends its validity — without that, a deleted or
        renamed model stays `live` forever and keeps appearing in the current
        receipt as though it were still computing. Reading absence from scan
        coverage (rather than a mutable end-date column) keeps the store
        append-only: the observation log remains the only temporal truth.
        """
        target = normalize_name(concept_label)
        rows = self._conn.execute(
            """SELECT o.scan_id, o.object_key, o.observed_at, o.ref_id,
                      f.concept_candidate, f.semantic_hash, f.relation
               FROM observations o
               JOIN fingerprints f ON f.fingerprint_id = o.ref_id
               WHERE o.kind = 'fingerprint'
               ORDER BY o.object_key, o.observed_at, o.ref_id"""
        ).fetchall()

        seen: dict[str, dict[str, sqlite3.Row]] = {}
        for r in rows:
            if normalize_name(r["concept_candidate"]) != target:
                continue
            seen.setdefault(r["object_key"], {})[r["scan_id"]] = r

        coverage = self.scan_coverage()
        intervals: list[ValidInterval] = []
        for object_key, by_scan in seen.items():
            system = object_key.split(":", 1)[0]
            runs: list[ValidInterval] = []
            for scan_id, started_at, systems in coverage:
                observed = by_scan.get(scan_id)
                if observed is None:
                    # Only a scan that READ this system can assert absence.
                    # Legacy scans recorded no coverage, so they stay silent
                    # and behave exactly as before.
                    if system in systems and runs and runs[-1].valid_to is None:
                        runs[-1].valid_to = started_at
                    continue
                if runs and runs[-1].valid_to is None and runs[-1].ref_id == observed["ref_id"]:
                    continue  # unchanged, still live
                runs.append(
                    ValidInterval(
                        object_key=object_key,
                        ref_id=observed["ref_id"],
                        concept_candidate=observed["concept_candidate"],
                        semantic_hash=observed["semantic_hash"],
                        valid_from=observed["observed_at"],
                        valid_to=None,
                        relation=observed["relation"],
                    )
                )
            for i, run in enumerate(runs[:-1]):
                if run.valid_to is None:
                    run.valid_to = runs[i + 1].valid_from
            intervals.extend(runs)
        return intervals

    def as_of(self, concept_label: str, ts: str) -> list[ValidInterval]:  # noqa: D401
        """What did this concept mean at time ts — the live definition at each
        site whose interval contains ts. Caller timestamps are normalized
        first: valid time is compared as text, so an offset-form instant would
        otherwise sort before its own 'Z' form and return the superseded
        definition."""
        ts = normalize_ts(ts)
        live = []
        for interval in self.history(concept_label):
            if interval.valid_from <= ts and (interval.valid_to is None or ts < interval.valid_to):
                live.append(interval)
        return live

    def restatement_check(self, concept_label: str, t0: str, t1: str) -> list[ValidInterval]:
        """Definition *changes* inside (t0, t1] that a comparison over
        [t0, t1] would silently span. A site's first-ever definition is a
        birth, not a restatement, so it never counts."""
        t0, t1 = normalize_ts(t0), normalize_ts(t1)
        history = self.history(concept_label)
        first_seen: dict[str, str] = {}
        for iv in history:
            if iv.object_key not in first_seen or iv.valid_from < first_seen[iv.object_key]:
                first_seen[iv.object_key] = iv.valid_from
        return [
            iv
            for iv in history
            if t0 < iv.valid_from <= t1 and iv.valid_from != first_seen[iv.object_key]
        ]

    # ---------------------------------------------------------- signing keys

    def latest_signing_key(self) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM signing_keys ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def record_signing_key(self, key_id: str, private_key: str, public_key: str) -> None:
        self._conn.execute(
            "INSERT INTO signing_keys VALUES (?, ?, ?, ?)",
            (key_id, utcnow(), public_key, private_key),
        )
        self._conn.commit()

    # --------------------------------------------------------- relationships

    def record_relationship_edge(
        self, edge_id: str, left_relation: str, left_column: str,
        right_relation: str, right_column: str,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO relationship_edges VALUES (?, ?, ?, ?, ?)",
            (edge_id, left_relation, left_column, right_relation, right_column),
        )

    def observe_relationship(
        self, edge_id: str, kind: str, source: str, stats: dict, observed_at: str,
    ) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO relationship_observations VALUES (?, ?, ?, ?, ?)",
            (observed_at, edge_id, kind, source, json.dumps(stats)),
        )
        self._conn.commit()

    def record_relationship_check(
        self, edge_id: str, warehouse: str, passed: bool, evidence: dict,
    ) -> str:
        check_id = "chk_" + uuid.uuid4().hex[:12]
        self._conn.execute(
            "INSERT INTO relationship_checks VALUES (?, ?, ?, ?, ?, ?)",
            (check_id, edge_id, utcnow(), warehouse, 1 if passed else 0,
             json.dumps(evidence)),
        )
        self._conn.commit()
        return check_id

    def relationship_graph(self) -> list[dict]:
        """Read model: every edge with its accumulated mined usage, declared
        flag, and latest verification check."""
        edges = {
            r["edge_id"]: dict(r)
            for r in self._conn.execute("SELECT * FROM relationship_edges")
        }
        for edge in edges.values():
            edge.update(mined_queries=0, mined_sources=[], declared_sources=[],
                        first_mined=None, last_mined=None, check=None)

        obs = self._conn.execute(
            "SELECT * FROM relationship_observations ORDER BY observed_at"
        ).fetchall()
        for r in obs:
            edge = edges.get(r["edge_id"])
            if edge is None:
                continue
            stats = json.loads(r["stats_json"] or "{}")
            if r["kind"] == "mined":
                edge["mined_queries"] += int(stats.get("queries", 0))
                if r["source"] not in edge["mined_sources"]:
                    edge["mined_sources"].append(r["source"])
                edge["first_mined"] = edge["first_mined"] or r["observed_at"]
                edge["last_mined"] = r["observed_at"]
            elif r["kind"] == "declared" and r["source"] not in edge["declared_sources"]:
                edge["declared_sources"].append(r["source"])

        checks = self._conn.execute(
            "SELECT * FROM relationship_checks ORDER BY checked_at"
        ).fetchall()
        for r in checks:  # later rows overwrite: latest check wins the read model
            edge = edges.get(r["edge_id"])
            if edge is not None:
                edge["check"] = {
                    "checked_at": r["checked_at"],
                    "warehouse": r["warehouse"],
                    "passed": bool(r["passed"]),
                    "evidence": json.loads(r["evidence_json"]),
                }
        return sorted(edges.values(), key=lambda e: (-e["mined_queries"], e["edge_id"]))

    # -------------------------------------------------------------- receipts

    def issue_receipt(
        self,
        concept_label: str,
        as_of: str | None = None,
        freshness: dict | None = None,
    ) -> Receipt | None:
        # An as-of receipt must carry the verdicts that were true AT that
        # instant, not today's. Stamping the current status onto a historical
        # definition produces a SIGNED "CERTIFIED" for a moment the store
        # itself records as contradicted — the receipt would be evidence for
        # exactly the wrong claim.
        scan = self.scan_as_of(as_of) if as_of else self.latest_scan()
        if scan is None:
            return None
        verdicts = [
            v
            for v in self.verdicts_for_scan(scan["scan_id"])
            if normalize_name(v["concept_label"]) == normalize_name(concept_label)
        ]
        if not verdicts:
            return None

        primary = min(
            verdicts,
            key=lambda v: VERDICT_PRECEDENCE.index(VerdictType(v["verdict"])),
        )

        # The freshness block was computed against a specific scan; if another
        # scan landed since, its claim no longer describes this receipt's
        # verdicts — degrade rather than attach a stale freshness claim.
        if freshness and freshness.get("scan_id") not in (None, scan["scan_id"]):
            freshness = {
                **freshness,
                "status": "unknown",
                "note": "a new scan landed between the freshness check and "
                        "receipt issuance — re-attest",
            }

        ts = as_of or utcnow()
        live = self.as_of(concept_label, ts)
        definition_version = [
            {
                "fingerprint_id": iv.ref_id,
                "semantic_hash": iv.semantic_hash,
                "site": iv.object_key,
                "relation": iv.relation,
                "valid_from": iv.valid_from,
            }
            for iv in live
        ]

        lineage: set[str] = set()
        for iv in live:
            row = self.fingerprint_row(iv.ref_id)
            if row:
                lineage.update(json.loads(row["source_columns_json"] or "[]"))

        divergences = [
            v["summary"]
            for v in verdicts
            if v["verdict"] in (VerdictType.CONTRADICTION.value, VerdictType.UNRESOLVED.value)
        ]

        receipt = Receipt(
            receipt_id="rcpt_" + uuid.uuid4().hex[:12],
            concept=concept_label,
            status=primary["verdict"],
            definition_version=definition_version,
            lineage=sorted(lineage),
            known_divergences=divergences,
            as_of=as_of,
            issued_at=utcnow(),
            scan_id=scan["scan_id"],
            verified_as_of=scan["started_at"],
            freshness=freshness
            or {
                "status": "unknown",
                "note": "estate not re-checked at attest time — pass estate "
                        "sources to attest to enable the freshness check",
            },
        )
        # Sign before persisting so the stored payload IS the certificate.
        from semantix.signing import sign_receipt

        sign_receipt(self, receipt)
        self._conn.execute(
            "INSERT INTO receipts VALUES (?, ?, ?, ?, ?)",
            (
                receipt.receipt_id,
                receipt.concept,
                receipt.as_of,
                receipt.issued_at,
                json.dumps(receipt.as_dict()),
            ),
        )
        self._conn.commit()
        return receipt


def normalize_ts(ts: str) -> str:
    """Caller timestamps -> the store's canonical '%Y-%m-%dT%H:%M:%SZ'.

    Valid time is compared as TEXT (SQLite has no date type here), which is
    only correct when every timestamp shares one representation. An RFC-3339
    offset like '2026-03-01T00:00:00+00:00' sorts before the 'Z' form of the
    same instant, so an as-of query would silently return the previous,
    superseded definition. Unparseable input is passed through unchanged
    rather than raising — a bad filter must not take down an attestation.
    """
    try:
        parsed = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return ts
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
