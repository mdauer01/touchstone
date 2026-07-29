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
SELECT content FROM agents.root
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


def publish_receipts(store: Store, warehouse_path: str) -> dict[str, Any]:
    """Publish signed receipts for every concept in the latest scan into the
    warehouse's AGENTS.ROOT. Returns a summary of what was written."""
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
        receipt = store.issue_receipt(label)
        if receipt is not None:
            rows.append((PROVIDER, f"receipt/{label}", json.dumps(receipt.as_dict())))
    rows.append((PROVIDER, SKILL_KEY, SKILL_CONTENT))
    rows.append((PROVIDER, DOC_KEY, DOC_CONTENT))

    con = duckdb.connect(warehouse_path)
    # Fully qualify with the catalog: a warehouse file named agents.duckdb —
    # the natural name — makes bare `agents.root` ambiguous in DuckDB.
    catalog = con.execute("SELECT current_database()").fetchone()[0]
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

    con.execute(f"DELETE FROM {root} WHERE provider = ?", (PROVIDER,))
    con.executemany(
        f"INSERT INTO {root} (provider, key, content) VALUES (?, ?, ?)", rows
    )
    con.close()

    return {
        "warehouse": warehouse_path,
        "scan_id": scan["scan_id"],
        "receipts": len(rows) - 2,
        "keys": [key for _, key, _ in rows],
    }
