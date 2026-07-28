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

-- Per-scan fate of every artifact the scan KNEW ABOUT, which is what makes
-- absence interpretable. A site may only be retired when a scan enumerated
-- its source and the artifact was genuinely gone; an artifact that was listed
-- but unreadable (uncompiled, unparseable) carries NO information about
-- whether its definitions still exist.
CREATE TABLE IF NOT EXISTS scan_artifact_state (
    scan_id      TEXT NOT NULL,
    artifact_key TEXT NOT NULL,       -- "<system>:<object_id>"
    readable     INTEGER NOT NULL,    -- 1 = extracted from, 0 = known but unreadable
    PRIMARY KEY (scan_id, artifact_key)
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
    """Now, to MILLISECONDS.

    Whole seconds could not represent a change that happens inside one, so two
    scans in the same second produced a ZERO-LENGTH valid interval: a
    definition that genuinely existed was live at no instant, and an `asof`
    query at the only moment it was ever observed returned its replacement.
    Millisecond precision sorts identically as text (fixed width, zero-padded)
    and stays comparable with the whole-second timestamps already in a store —
    `normalize_ts` renders both to this shape.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


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
        # A scan of a real estate holds its write transaction for minutes, so
        # two CI jobs on two PRs meant one died with a bare
        # `sqlite3.OperationalError: database is locked`. WAL lets readers
        # (concepts, attest, the MCP server) run against a store mid-scan, and
        # busy_timeout makes a competing WRITER wait its turn instead of
        # failing instantly.
        self._conn = sqlite3.connect(self.path, timeout=120.0)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass  # some filesystems (network mounts) refuse WAL; not fatal
        self._conn.execute("PRAGMA busy_timeout=120000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ scans

    def begin_scan(self, label: str | None, dialect: str) -> tuple[str, str]:
        scan_id = "scan_" + uuid.uuid4().hex[:12]
        # Normalized at the WRITE boundary, not only on read. Valid time is
        # compared as TEXT, so a store holding two representations of the same
        # instant ('...00Z' and '...00.000Z') orders them wrongly and an as-of
        # query at exactly that instant returns the previous definition.
        started_at = normalize_ts(utcnow())
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
        """The newest scan that actually FINISHED.

        A scan that died partway wrote no stats and describes nothing, but it
        is still the newest row — so every "current health" interface
        (`concepts`, `attest`, `publish`, the MCP tools) answered from it and
        reported a CLEAN ESTATE. The store held a recorded CONTRADICTION at
        that moment; `concepts` printed an empty verdict list, which reads as
        "nothing is wrong". `completed_scans` already applied this filter and
        `diff` already used it; the rest of the read paths did not.
        """
        # rowid = insertion order: two scans inside the same second must not
        # tie-break on the random scan_id (recent_scans already does this).
        row = self._conn.execute(
            "SELECT * FROM scans WHERE stats_json IS NOT NULL ORDER BY rowid DESC LIMIT 1"
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

    def snapshot_digest(self, scan_id: str) -> str:
        """Content fingerprint of the estate a scan observed. Two scans of an
        unchanged estate share a digest, which is what lets `diff` tell a
        re-run apart from a genuine change."""
        shas = sorted(
            f"{r['system']}:{r['object_id']}:{r['raw_sha']}"
            for r in self.scan_snapshot(scan_id)
        )
        return hashlib.sha256("|".join(shas).encode()).hexdigest() if shas else ""

    def completed_scans(self, limit: int = 50) -> list[dict]:
        """Scans that finished, newest first. A scan that died partway wrote
        no stats and describes nothing, so it must never serve as a baseline."""
        rows = self._conn.execute(
            "SELECT * FROM scans WHERE stats_json IS NOT NULL ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

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
            (scan_id, normalize_ts(observed_at), kind, object_key, ref_id),
        )

    # ------------------------------------------------------ concepts/verdicts

    def save_results(self, scan_id: str, concepts: list[Concept], verdicts: list[Verdict]) -> None:
        for concept in concepts:
            members = {
                "fingerprints": [fp.fingerprint_id for fp in concept.fingerprints],
                "claims": [cl.claim_id for cl in concept.claims],
                # Every NAME this concept goes by. The label is only one of
                # them and is chosen from the members, so it moves when the
                # ontology gains an alias — after which `attest <the old name>`
                # returned nothing with no explanation. Recorded so a receipt
                # can be found by any name the estate actually uses.
                "names": sorted({fp.concept_candidate for fp in concept.fingerprints}
                                | {cl.name for cl in concept.claims}),
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

    def concepts_touching_artifact(self, system: str, object_id: str) -> set[str]:
        """Concept labels this artifact has previously contributed a definition
        to. Used to decide whether an unreadable artifact is a RELEVANT gap:
        if it once defined `net_revenue`, a scan that cannot read it must not
        certify `net_revenue`."""
        refs = {
            r["ref_id"]
            # substr, not LIKE: `_` is a LIKE wildcard and dbt object ids are
            # full of them, so `model.acme.a_b` also matched `model.acme.axb`.
            for r in self._conn.execute(
                "SELECT ref_id FROM observations WHERE kind = 'fingerprint' "
                "AND substr(object_key, 1, ?) = ?",
                (len(f"{system}:{object_id}:"), f"{system}:{object_id}:"),
            )
        }
        if not refs:
            return set()
        labels = set()
        for r in self._conn.execute("SELECT label, members_json FROM concepts"):
            members = json.loads(r["members_json"] or "{}")
            if refs & set(members.get("fingerprints", [])):
                labels.add(r["label"])
        return labels

    def record_artifact_state(self, scan_id: str, system: str, object_id: str,
                              readable: bool) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO scan_artifact_state VALUES (?, ?, ?)",
            (scan_id, f"{system}:{object_id}", 1 if readable else 0),
        )

    def artifact_states(self) -> dict[str, dict[str, bool]]:
        """scan_id -> {artifact_key: readable}."""
        out: dict[str, dict[str, bool]] = {}
        for r in self._conn.execute("SELECT * FROM scan_artifact_state"):
            out.setdefault(r["scan_id"], {})[r["artifact_key"]] = bool(r["readable"])
        return out

    def scan_coverage(self) -> list[tuple[str, str, set[str]]]:
        """[(scan_id, started_at, OBJECT KEYS this scan actually read)], oldest
        first.

        Absence is only concludable for an artifact the scan ENUMERATED. This
        used to answer "which SYSTEMS did it touch", which is not evidence
        about an individual artifact: pointing a second scan at an unrelated
        dbt project — or at a subdirectory, or running per-team CI jobs against
        one store — retired every definition the other project had, because
        both are `system == "dbt"`. The models were still on disk. Re-scanning
        then produced a second interval, so the store permanently recorded a
        fictitious death-and-rebirth cycle.
        """
        covered: dict[str, set[str]] = {}
        for r in self._conn.execute(
            """SELECT sa.scan_id, a.system, a.object_id FROM scan_artifacts sa
               JOIN artifacts a ON a.artifact_id = sa.artifact_id"""
        ):
            covered.setdefault(r["scan_id"], set()).add(
                f"{r['system']}:{r['object_id']}")
        # started_at, not rowid: intervals are stamped from observed_at, so a
        # walk in insertion order produced intervals that do not contain the
        # instants the definition was observed when a clock moved backwards.
        return [
            (r["scan_id"], r["started_at"], covered.get(r["scan_id"], set()))
            for r in self._conn.execute(
                "SELECT scan_id, started_at FROM scans ORDER BY started_at, rowid")
        ]

    def concept_labels_containing(self, name: str, scan_id: str) -> list[str]:
        """Concept labels in this scan whose MEMBERS include `name`.

        Concept labels are derived from member names, so an ontology addition
        that changes nothing about a concept's meaning can rename it. Without
        this, the documented `attest <metric>` flow simply stopped returning
        anything the day someone declared an alias.
        """
        target = normalize_name(name)
        out = []
        for r in self._conn.execute(
            "SELECT label, members_json FROM concepts WHERE scan_id = ?", (scan_id,)
        ):
            members = json.loads(r["members_json"] or "{}")
            names = {normalize_name(n) for n in members.get("names", [])}
            if target in names:
                out.append(r["label"])
        return out

    def _relation_for(self, scan_id: str, object_key: str) -> str | None:
        """The relation the ARTIFACT at this definition site reported.

        `fingerprints.relation` is on a content-addressed row shared by every
        site computing the same thing (record_fingerprint is INSERT OR IGNORE),
        so the first site to be recorded won and history, asof, the HTML report
        and the signed receipt all attributed the other sites' definitions to
        its table — two teams' models rendering as one duplicated row.
        """
        system, _, rest = object_key.partition(":")
        object_id = rest.rsplit(":", 1)[0] if ":" in rest else rest
        row = self._conn.execute(
            """SELECT a.relation FROM scan_artifacts sa
               JOIN artifacts a ON a.artifact_id = sa.artifact_id
               WHERE sa.scan_id = ? AND a.system = ? AND a.object_id = ?
               LIMIT 1""",
            (scan_id, system, object_id),
        ).fetchone()
        return row["relation"] if row else None

    def concept_members(self, concept_label: str) -> tuple[set[str], set[str]]:
        """(fingerprint ids, claim ids) ever clustered under this concept.

        Concept resolution runs at scan time and records its clustering in
        `concepts.members_json`. Re-deriving membership by matching the
        concept LABEL against member names throws that work away and fails on
        exactly the case clustering exists for: an aliased concept whose
        members are all spelled differently from the label (`gmv` clustering
        `total_gmv` and `gross_merchandise_value`). A label also resolves via
        any of its members, so asking about a name you saw in a report finds
        the whole concept rather than a fragment.
        """
        target = normalize_name(concept_label)
        fingerprints: set[str] = set()
        claims: set[str] = set()
        rows = self._conn.execute("SELECT label, members_json FROM concepts").fetchall()

        matched_labels = {
            r["label"] for r in rows if normalize_name(r["label"]) == target
        }
        if not matched_labels:
            # Fall back to membership: the caller may have named a member.
            for r in rows:
                members = json.loads(r["members_json"] or "{}")
                names = self._names_for(members.get("fingerprints", []))
                if any(normalize_name(n) == target for n in names):
                    matched_labels.add(r["label"])

        for r in rows:
            if r["label"] in matched_labels:
                members = json.loads(r["members_json"] or "{}")
                fingerprints.update(members.get("fingerprints", []))
                claims.update(members.get("claims", []))
        return fingerprints, claims

    def _names_for(self, fingerprint_ids: list[str]) -> list[str]:
        if not fingerprint_ids:
            return []
        marks = ",".join("?" * len(fingerprint_ids))
        return [
            r["concept_candidate"]
            for r in self._conn.execute(
                f"SELECT concept_candidate FROM fingerprints WHERE fingerprint_id IN ({marks})",
                fingerprint_ids,
            )
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
        member_ids, _ = self.concept_members(concept_label)
        # Membership recorded PER SCAN. Judging every observation against the
        # union across all scans meant a fingerprint that left a concept kept
        # being attributed to it, so a receipt could name definitions the
        # current scan no longer clusters together.
        per_scan: dict[str, set[str]] = {}
        for r in self._conn.execute("SELECT scan_id, label, members_json FROM concepts"):
            if normalize_name(r["label"]) == normalize_name(concept_label):
                members = json.loads(r["members_json"] or "{}")
                per_scan.setdefault(r["scan_id"], set()).update(
                    members.get("fingerprints", [])
                )
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
            # Membership recorded at scan time is authoritative. The name
            # match is only a fallback for stores written before concepts
            # were persisted.
            scan_members = per_scan.get(r["scan_id"])
            if scan_members is not None:
                belongs = r["ref_id"] in scan_members
            elif member_ids:
                belongs = r["ref_id"] in member_ids
            else:
                belongs = normalize_name(r["concept_candidate"]) == target
            if not belongs:
                continue
            seen.setdefault(r["object_key"], {})[r["scan_id"]] = r

        coverage = self.scan_coverage()
        covered_by = {scan_id: keys for scan_id, _, keys in coverage}
        states = self.artifact_states()
        intervals: list[ValidInterval] = []
        for object_key, by_scan in seen.items():
            system, _, rest = object_key.partition(":")
            artifact_key = f"{system}:{rest.rsplit(':', 1)[0]}" if ":" in rest else object_key
            runs: list[ValidInterval] = []
            last_seen_scan: str | None = None
            for scan_id, started_at, enumerated in coverage:
                observed = by_scan.get(scan_id)
                if observed is None:
                    # Absence is only evidence when the scan (a) covered the
                    # SAME ESTATE and (b) either found the artifact gone, or
                    # read it and found this measure gone. An artifact the scan
                    # LISTED but could not read — an uncompiled dbt model after
                    # `--select`, an unparseable file — says nothing at all,
                    # and must not fabricate a retirement date.
                    #
                    # "Same estate" cannot be "same system": pointing a second
                    # scan at an unrelated dbt project retired every definition
                    # of the first, because both are system == "dbt", while the
                    # models sat untouched on disk. A deleted model and a model
                    # in some other project look identical in isolation — both
                    # are simply absent — so the discriminator is OVERLAP: this
                    # scan is speaking about the same estate only if it re-read
                    # something the scan that last saw this definition also
                    # read.
                    #
                    # Scoped to the artifact's OWN system: a rescan that read
                    # only the metrics YAML overlaps on the claims side while
                    # having examined no dbt model at all, which is precisely
                    # the `dbt --select` case that must say nothing.
                    prefix = f"{system}:"
                    def _same_system(keys, _p=prefix):
                        return {k for k in keys if k.startswith(_p)}
                    covers_same_estate = bool(
                        last_seen_scan
                        and (_same_system(enumerated)
                             & _same_system(covered_by.get(last_seen_scan, set())))
                    )
                    if not (covers_same_estate and runs and runs[-1].valid_to is None):
                        continue
                    known = states.get(scan_id, {})
                    if known and artifact_key in known and not known[artifact_key]:
                        continue  # listed but unreadable -> no information
                    runs[-1].valid_to = started_at
                    continue
                last_seen_scan = scan_id
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
                        # The relation the ARTIFACT reported, not whatever the
                        # content-addressed fingerprints row happens to hold:
                        # two sites computing the same thing share one
                        # fingerprint_id (INSERT OR IGNORE), so the first
                        # site's relation won and a signed receipt attributed
                        # one team's definition to the other team's table.
                        relation=self._relation_for(observed["scan_id"], object_key)
                        or observed["relation"],
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
        all_verdicts = self.verdicts_for_scan(scan["scan_id"])
        verdicts = [
            v
            for v in all_verdicts
            if normalize_name(v["concept_label"]) == normalize_name(concept_label)
        ]
        if not verdicts:
            # A concept's LABEL is chosen from its members, so declaring a new
            # alias renames the concept — after which `attest revenue` returned
            # nothing at all, with no error explaining why, though the ontology
            # still declares `revenue`. Fall back to membership: the concept
            # this name belongs to is the concept to attest.
            for label in self.concept_labels_containing(concept_label,
                                                        scan["scan_id"]):
                verdicts = [v for v in all_verdicts
                            if normalize_name(v["concept_label"]) == normalize_name(label)]
                if verdicts:
                    break
        if not verdicts:
            return None

        # The status answers "can I trust this number", so precedence — not
        # raw severity — is the right order: CONTRADICTION always wins, then
        # the verification result. Reconciliation makes ORPHAN_CLAIM (claims
        # and no fingerprints) and SHADOW_METRIC (fingerprints and no claims)
        # mutually exclusive with CERTIFIED, so the only real co-occurrence is
        # CERTIFIED+ALIASING — where CERTIFIED is genuinely the headline and
        # aliasing is housekeeping. The defect was never the choice of primary;
        # it was that every OTHER verdict was silently dropped. They are all
        # carried in `verdicts` below.
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

        # An `as_of` in the FUTURE is not a question we can answer. `attest X
        # --as-of 2099-01-01` — and the MCP `attest` tool, whose schema invites
        # an agent to pass "the timestamp the answer refers to" — produced a
        # signed CERTIFIED for 73 years of unobserved time, with no divergence
        # noted. Evidence cannot describe a moment nobody has looked at.
        now = utcnow()
        ts = as_of or now
        future = normalize_ts(ts) > normalize_ts(now)
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

        # Scope limitation. A scan that could not read part of the estate
        # cannot issue a clean opinion over it. Two levels, deliberately:
        # every receipt discloses what the scan could not read, and a receipt
        # is DOWNGRADED only when an unreadable artifact is one this very
        # concept has previously been defined in — there the gap is known to
        # be relevant, rather than merely possible.
        unreadable = sorted(
            key for key, ok in self.artifact_states().get(scan["scan_id"], {}).items()
            if not ok
        )
        concept_sites = {_artifact_key(iv.object_key) for iv in self.history(concept_label)}
        known_relevant = sorted(concept_sites & set(unreadable))
        scope = {
            "complete": not unreadable,
            "unreadable": unreadable,
            # Named separately from `known_relevant` because an EMPTY list of
            # known-relevant sites is not proof of irrelevance — on a first
            # scan there is no history to consult, and reporting `[]` there
            # asserted "no relevant gap" when the truth was "we cannot say".
            "known_relevant": known_relevant,
            "relevance_determinable": bool(concept_sites),
        }

        # A receipt asserting verification must name what it verified. If the
        # evidence is empty, the assertion is unsupported no matter why —
        # degrade rather than sign it. Statuses that legitimately have no
        # computed definition (ORPHAN_CLAIM: declared, never computed) are
        # unaffected; the incoherent combination is "certified, but of what?"
        status = primary["verdict"]
        # Any unread estate blocks a signed certification, whether or not we
        # can show the gap is relevant to THIS concept. Requiring proof of
        # relevance inverted the burden: it certified by default and objected
        # only when history happened to contain a counterexample.
        if unreadable and status in (VerdictType.CERTIFIED.value, VerdictType.ALIASING.value):
            status = VerdictType.UNRESOLVED.value
            detail = (", ".join(known_relevant) + " (known to define this concept)"
                      if known_relevant else ", ".join(unreadable[:3]))
            divergences = divergences + [
                f"cannot certify: {len(unreadable)} artifact(s) in this estate could "
                f"not be read — {detail}"
            ]
        # What counts as evidence depends on what the verdict ASSERTS. A
        # contradiction between two DECLARED definitions is fully substantiated
        # by the claims themselves — requiring computed definitions there
        # silenced a real disagreement down to "unresolved".
        _, claim_ids = self.concept_members(concept_label)
        has_evidence = bool(definition_version) or (
            status == VerdictType.CONTRADICTION.value and bool(claim_ids)
        )
        # SHADOW_METRIC asserts "computed at N sites in the estate" — an
        # EXISTENCE claim about a computed definition. Omitting it from this
        # set let a receipt make that claim while showing definition_version:
        # [] and lineage: [], a signed statement its own evidence contradicts.
        evidence_required = status in (
            VerdictType.CERTIFIED.value,
            VerdictType.ALIASING.value,
            VerdictType.CONTRADICTION.value,
            VerdictType.SHADOW_METRIC.value,
        )
        if evidence_required and not has_evidence:
            status = VerdictType.UNRESOLVED.value
            divergences = divergences + [
                f"cannot substantiate {primary['verdict']}: no definition was live "
                f"at {ts} for this concept — refusing to certify without evidence"
            ]

        if future and status in (VerdictType.CERTIFIED.value,
                                 VerdictType.ALIASING.value):
            status = VerdictType.UNRESOLVED.value
            divergences = divergences + [
                f"cannot certify for {ts}: that instant is in the future and no "
                f"evidence describes it (last observation {now})"
            ]

        # A receipt whose OWN freshness block reports a definition removed, or
        # the estate drifted, cannot also assert a clean certification. One
        # signed document said "this definition is live at issue time" and
        # "this definition no longer exists in the estate" in the same breath;
        # stale freshness reached neither `status`, `known_divergences`, nor
        # `scope`.
        fresh_status = (freshness or {}).get("status")
        if fresh_status in ("stale", "partial", "unknown"):
            detail = []
            for key in ("removed", "changed", "added"):
                if (freshness or {}).get(key):
                    detail.append(f"{key}: {', '.join(sorted((freshness or {})[key]))}")
            note = f"freshness is {fresh_status}" + (
                f" — {'; '.join(detail)}" if detail else "")
            divergences = divergences + [note]
            scope = {**scope, "freshness": fresh_status}
            if status in (VerdictType.CERTIFIED.value, VerdictType.ALIASING.value):
                status = VerdictType.UNRESOLVED.value

        # The emitted verdict list must agree with the status. Leaving a
        # CERTIFIED entry in `verdicts` while the status was downgraded made
        # the signed receipt contradict itself, and a reader taking the more
        # favourable of the two would be back where they started.
        emitted = []
        for v in sorted(verdicts, key=lambda v: -v["severity"]):
            entry = {"verdict": v["verdict"], "severity": v["severity"],
                     "summary": v["summary"]}
            if status != primary["verdict"] and v["verdict"] in (
                    VerdictType.CERTIFIED.value, VerdictType.ALIASING.value):
                entry = {**entry, "verdict": status,
                         "superseded": v["verdict"],
                         "summary": f"[downgraded] {v['summary']}"}
            emitted.append(entry)

        receipt = Receipt(
            receipt_id="rcpt_" + uuid.uuid4().hex[:12],
            concept=concept_label,
            status=status,
            definition_version=definition_version,
            lineage=sorted(lineage),
            known_divergences=divergences,
            scope=scope,
            verdicts=emitted,
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


def _artifact_key(object_key: str) -> str:
    """`<system>:<object_id>:<measure>` -> `<system>:<object_id>`."""
    parts = object_key.split(":")
    return ":".join(parts[:-1]) if len(parts) > 2 else object_key


def normalize_ts(ts: str) -> str:
    """Caller timestamps -> the store's canonical '%Y-%m-%dT%H:%M:%SZ'.

    Valid time is compared as TEXT (SQLite has no date type here), which is
    only correct when every timestamp shares one representation. An RFC-3339
    offset like '2026-03-01T00:00:00+00:00' sorts before the 'Z' form of the
    same instant, so an as-of query would silently return the previous,
    superseded definition.

    Unparseable input RAISES. Passing it through was the wrong call: text
    comparison against a value like '03/01/2026' does not fail, it silently
    answers with the wrong definition — and then signs it. An error the
    caller must handle is strictly better than a confident wrong answer.
    """
    try:
        parsed = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError) as e:
        raise ValueError(
            f"unparseable timestamp {ts!r}: expected an ISO-8601 instant such as "
            "2026-03-01T00:00:00Z"
        ) from e
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc)
    # Millisecond precision, matching utcnow. Fixed width and zero-padded, so
    # text comparison still orders correctly against whole-second timestamps
    # already written by earlier versions ('...:00Z' < '...:00.000Z' is the
    # only ordering subtlety, and it puts the older form first, which is the
    # correct reading of an interval that began before sub-second stamping).
    return parsed.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
