"""Write-back certification into Agents Schema (spec §7 Phase 2, landed early).

Agents Schema is where agents already look for what a metric means; this
module puts *whether that meaning is true* in the same place. After a scan,
every concept's signed receipt is published into `AGENTS.ROOT` under the
`semantix` provider:

    (semantix, receipt/<concept>, <signed receipt JSON>)

plus a `skill/verify-metric` row teaching any SQL-capable agent the check
(per the schema's skill convention: Markdown with YAML frontmatter), and a
documentation row under the unprefixed key `receipt` (per the schema's
doc-row convention).

Alongside ROOT, publish projects the scan's verdicts into a structured
`semantix.verdicts` table (one row per verdict per affected relation), so
findings gate through rails the estate already triages — dbt tests
(integrations/dbt/semantix), warehouse alerts, orchestrator asset checks —
rather than through a new alert channel. See docs/action-rails.md.

Replacement semantics follow the standard's own model — provider rows are
regenerated wholesale on each publish. That is safe here because ROOT is a
*projection*: the append-only semantix store keeps every receipt and the
full observation history; the warehouse rows are just the current answer.

Target is a DuckDB file (zero-infra). A Snowflake target is the same three
statements behind a different connector.
"""

from __future__ import annotations

import json
from typing import Any

from semantix.store import Store

PROVIDER = "semantix"

SKILL_KEY = "skill/verify-metric"
SKILL_CONTENT = """\
---
uses:
  schemas: [AGENTS]
---
# Verify a metric before quoting it

Before answering a business-metric question with a number, check whether the
metric's definition is certified:

```sql
SELECT content FROM "{catalog}".agents.root
WHERE provider = 'semantix' AND key = 'receipt/<metric_name>';
```

The row is a signed semantic receipt. `status` is the verdict on the
definition itself: CERTIFIED means every computed definition matches the
declared one; CONTRADICTION means competing definitions disagree — read
`known_divergences` before answering, and say which definition you used.
`definition_version` pins the exact content-addressed definitions that were
live. Verify the receipt offline with `semantix verify-receipt` against the
publisher's pinned public key. Quote the `receipt_id` alongside the number.
"""

DOC_KEY = "receipt"
DOC_CONTENT = (
    "Rows under receipt/<concept> are signed semantic receipts issued by "
    "semantix: the certification status of a metric's definition (CERTIFIED / "
    "CONTRADICTION / ...), the content-addressed definition versions live at "
    "issue time, physical lineage, and known divergences. Signatures are "
    "Ed25519 over the canonical payload; see skill/verify-metric."
)


def publish_receipts(store: Store, warehouse_path: str,
                     freshness: dict | None = None) -> dict[str, Any]:
    """Publish signed receipts for every concept in the latest scan into the
    warehouse's AGENTS.ROOT. Returns a summary of what was written.

    `freshness` is the estate re-check to embed in every receipt; without
    it each published receipt carries `freshness.status = "unchecked"`, and
    the docs' "decay honestly" claim rests on verified_as_of alone."""
    # Every other warehouse entry point refuses an unsupported scheme by name;
    # publish treated any string as a DuckDB path, so `--warehouse
    # postgres://host/db` died in a raw duckdb IOException.
    if "://" in warehouse_path and not warehouse_path.startswith("duckdb://"):
        scheme = warehouse_path.split("://", 1)[0]
        raise ValueError(
            f"no {scheme}:// driver shipped yet — publish writes to a DuckDB "
            f"file (pass a path, or duckdb://path)")
    warehouse_path = warehouse_path.removeprefix("duckdb://")
    if not warehouse_path.strip():
        # A bare `duckdb://` connected to an IN-MEMORY database and reported
        # success while writing nothing anywhere.
        raise ValueError("duckdb:// carries no path — pass duckdb://<file> or "
                         "a plain file path")
    try:
        import duckdb
    except ImportError as e:
        raise ValueError("publishing needs `pip install duckdb`") from e

    scan = store.latest_scan()
    if scan is None:
        raise ValueError("no scans in this store — run `semantix scan` first")

    labels = sorted({v["concept_label"] for v in store.verdicts_for_scan(scan["scan_id"])})
    rows: list[tuple[str, str, str]] = []
    for label in labels:
        receipt = store.issue_receipt(label, freshness=freshness)
        if receipt is not None:
            rows.append((PROVIDER, f"receipt/{label}", json.dumps(receipt.as_dict())))

    con = duckdb.connect(warehouse_path)
    # Fully qualify with the catalog: a warehouse file named agents.duckdb —
    # the natural name — makes bare `agents.root` ambiguous in DuckDB. The
    # skill text agents read must carry the same qualification, or the SQL
    # it teaches fails in exactly that file.
    catalog = con.execute("SELECT current_database()").fetchone()[0]
    rows.append((PROVIDER, SKILL_KEY, SKILL_CONTENT.replace("{catalog}", catalog)))
    rows.append((PROVIDER, DOC_KEY, DOC_CONTENT))
    root = f'"{catalog}".agents.root'
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{catalog}".agents')
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {root} "
        "(provider VARCHAR, key VARCHAR, content VARCHAR)"
    )
    # Name the columns. A positional INSERT writes into whatever the first
    # three columns happen to be, and this table belongs to the CUSTOMER: if
    # theirs carries an id, a timestamp, or a different column order, every
    # value lands in the wrong field and their existing rows are corrupted by
    # a tool whose entire pitch is not corrupting things.
    existing = {
        r[0].lower()
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE lower(table_schema) = 'agents' AND lower(table_name) = 'root'"
        ).fetchall()
    }
    required = {"provider", "key", "content"}
    missing = sorted(required - existing)
    if existing and missing:
        con.close()
        raise ValueError(
            f"{warehouse_path}: agents.root exists but is missing column(s) "
            f"{', '.join(missing)} — refusing to write into a table whose shape "
            "does not match the Agents Schema convention"
        )

    try:
        _refuse_unwritable_columns(con, "agents", "root", {"provider", "key", "content"},
                                   warehouse_path)
    except ValueError:
        con.close()
        raise
    # One transaction for both tables: DuckDB autocommits each statement, so
    # a DELETE that succeeded before an INSERT failed (a customer column the
    # shape guard did not know about) left agents.root without our rows and
    # semantix.verdicts EMPTY — after which every dbt gate passed cleanly.
    con.begin()
    try:
        con.execute(f"DELETE FROM {root} WHERE provider = ?", (PROVIDER,))
        con.executemany(
            f"INSERT INTO {root} (provider, key, content) VALUES (?, ?, ?)", rows
        )
        verdict_rows = _publish_verdicts(store, scan, con, catalog, warehouse_path)
        con.commit()
    except Exception:
        con.rollback()
        con.close()
        raise
    con.close()

    return {
        "warehouse": warehouse_path,
        "scan_id": scan["scan_id"],
        "receipts": len(rows) - 2,
        "verdict_rows": verdict_rows,
        "keys": [key for _, key, _ in rows],
    }


# Columns the verdicts projection carries. The table exists so the estate's
# OWN immune system can gate on findings — dbt tests, warehouse alerts,
# orchestrator asset checks are all "run this SELECT, fail on rows" — instead
# of semantix growing one more alert channel nobody triages.
_VERDICT_COLUMNS = ("scan_id", "scanned_at", "published_at", "concept",
                    "verdict", "severity", "summary", "relation")


def _refuse_unwritable_columns(con, schema: str, table: str, ours: set[str],
                               warehouse_path: str) -> None:
    """A customer-created table whose EXTRA columns are NOT NULL without a
    default cannot take our rows; the name-only shape guard let the DELETE
    run and the INSERT fail. Refuse before touching anything."""
    rows = con.execute(
        "SELECT column_name, is_nullable, column_default FROM information_schema.columns "
        "WHERE lower(table_schema) = ? AND lower(table_name) = ?",
        (schema, table)).fetchall()
    blocking = sorted(name for name, nullable, default in rows
                      if name.lower() not in ours
                      and str(nullable).upper() == "NO" and default is None)
    if blocking:
        # The caller owns the connection (and the transaction, when there is
        # one): closing here left the rollback path with a dead handle.
        raise ValueError(
            f"{warehouse_path}: {schema}.{table} carries NOT NULL column(s) "
            f"{', '.join(blocking)} that semantix does not write — refusing "
            f"to publish into a table shaped for another writer")


def _publish_verdicts(store: Store, scan: dict, con, catalog: str,
                      warehouse_path: str = "") -> int:
    """Project the latest scan's verdicts, one row per affected relation.

    A verdict is concept-level; consumers gate per-relation (`{{ this }}` in
    a dbt test) or per-concept. Each verdict therefore lands once per
    relation its concept touches, plus with relation NULL when the concept
    touches none — so concept-scoped queries always find it.

    Unlike agents.root (a shared convention this tool must not disturb),
    semantix.verdicts is OURS: replaced wholesale each publish, a projection
    of the append-only store, never the record.
    """
    relations: dict[str, set[str]] = {}
    for concept in store.concepts_for_scan(scan["scan_id"]):
        rels: set[str] = set()
        for fp_id in concept["members"].get("fingerprints") or []:
            row = store.fingerprint_row(fp_id)
            if row and row.get("relation"):
                rels.add(row["relation"])
        for claim_id in concept["members"].get("claims") or []:
            row = store.claim_row(claim_id)
            if row and row.get("model"):
                rels.add(row["model"])
        relations[concept["label"]] = rels

    table = f'"{catalog}".semantix.verdicts'
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{catalog}".semantix')
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {table} ("
        "scan_id VARCHAR, scanned_at TIMESTAMPTZ, published_at TIMESTAMPTZ, "
        "concept VARCHAR, verdict VARCHAR, severity INTEGER, "
        "summary VARCHAR, relation VARCHAR)"
    )
    described = {
        r[0].lower(): str(r[1]).upper()
        for r in con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE lower(table_schema) = 'semantix' "
            "AND lower(table_name) = 'verdicts'"
        ).fetchall()
    }
    missing = sorted(set(_VERDICT_COLUMNS) - set(described))
    if described and missing:
        raise ValueError(
            f"semantix.verdicts exists but is missing column(s) "
            f"{', '.join(missing)} — refusing to write a projection tests "
            f"would silently misread")
    # Names are not enough: a VARCHAR scanned_at took our rows and then
    # broke every fresh_scan comparison at test time.
    for col in ("scanned_at", "published_at"):
        if col in described and "TIMESTAMP" not in described[col]:
            raise ValueError(
                f"semantix.verdicts.{col} is {described[col]}, not a timestamp "
                f"— the freshness gate cannot compare against it")
    _refuse_unwritable_columns(con, "semantix", "verdicts", set(_VERDICT_COLUMNS),
                               warehouse_path)

    rows = []
    for v in store.verdicts_for_scan(scan["scan_id"]):
        rels = sorted(relations.get(v["concept_label"], set())) or [None]
        for rel in rels:
            rows.append((scan["scan_id"], scan["started_at"],
                         v["concept_label"], v["verdict"], v["severity"],
                         v["summary"], rel))
    con.execute(f"DELETE FROM {table}")
    con.executemany(
        f"INSERT INTO {table} ({', '.join(_VERDICT_COLUMNS)}) "
        "VALUES (?, CAST(? AS TIMESTAMPTZ), current_timestamp, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)
