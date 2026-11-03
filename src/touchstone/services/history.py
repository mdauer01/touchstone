"""Query-history intake: executions, not SQL text.

The monitor used to receive one string — every statement joined with `;` —
which threw away everything that says WHO ran a query, WHEN, from WHICH
dashboard, and under WHICH session defaults. Each of those is evidence the
monitor needs: the time decides which definition applied, the source
decides whose finding it is, and the session context decides which table
a short name read.

Formats: `.jsonl` (one object per execution), `.csv` (header row), or
`.sql` (statements only, no metadata). Only `statement_text` is required.
Columns this module does not read are named in the result, never dropped
silently — a context column under an unexpected name would otherwise look
like "no context" and every short name would read as unresolved for a
reason nobody could see.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlglot import exp

FIELDS = ("statement_id", "statement_text", "start_time", "executed_by",
          "client_application", "catalog", "schema", "query_tag",
          "dashboard_id", "genie_space_id", "session_id", "statement_type")
# Spellings the warehouses' own history tables use for the same facts.
_ALIASES = {
    "query_id": "statement_id", "query_text": "statement_text",
    "user_name": "executed_by", "executed_as": "executed_by",
    "database_name": "catalog", "schema_name": "schema",
    "current_catalog": "catalog", "current_schema": "schema",
    "session_key": "session_id", "query_type": "statement_type",
}


@dataclass
class Execution:
    statement_text: str
    statement_id: str | None = None
    start_time: str | None = None
    executed_by: str | None = None
    client_application: str | None = None
    catalog: str | None = None
    schema: str | None = None
    query_tag: str | None = None
    dashboard_id: str | None = None
    genie_space_id: str | None = None
    # The session that ran it: session state (USE, SET, temp views) from
    # another execution applies only within one (services.session).
    session_id: str | None = None
    statement_type: str | None = None

    @property
    def asset(self) -> str | None:
        if self.dashboard_id:
            return f"dashboard:{self.dashboard_id}"
        if self.genie_space_id:
            return f"genie:{self.genie_space_id}"
        return None


@dataclass
class HistoryIntake:
    executions: list[Execution] = field(default_factory=list)
    source: str = ""
    format: str = "sql"
    rows: int = 0
    duplicate_executions: int = 0       # same statement_id seen again
    empty_rows: int = 0
    ignored_columns: list[str] = field(default_factory=list)


def read_history(path: str | Path) -> HistoryIntake:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".jsonl":
        records = []
        for n, line in enumerate(p.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{p}:{n}: not a JSON object ({e.msg})") from None
            if not isinstance(obj, dict):
                raise ValueError(f"{p}:{n}: not a JSON object")
            records.append(obj)
        return from_records(records, source=str(p), fmt="jsonl")
    if suffix == ".csv":
        with p.open(newline="", encoding="utf-8-sig") as fh:
            return from_records(list(csv.DictReader(fh)), source=str(p), fmt="csv")
    return from_sql_text(p.read_text(encoding="utf-8-sig"), source=str(p))


def from_sql_text(text: str, source: str = "") -> HistoryIntake:
    """A bare SQL log: one execution per statement, no metadata. Splitting
    happens in the monitor (one tokenizer-based splitter for every history
    consumer), so the whole text travels as one pseudo-row."""
    intake = HistoryIntake(source=source, format="sql")
    if text.strip():
        intake.executions.append(Execution(statement_text=text))
        intake.rows = 1
    return intake


def from_records(records: list[dict], source: str = "", fmt: str = "records") -> HistoryIntake:
    intake = HistoryIntake(source=source, format=fmt)
    seen: set[str] = set()
    ignored: set[str] = set()
    for raw in records:
        intake.rows += 1
        row: dict[str, object] = {}
        for key, value in raw.items():
            k = str(key).strip().lower()
            if k == "query_source" and isinstance(value, dict):
                # Databricks system.query.history carries the entity ids in a
                # struct; exported as JSON it arrives nested.
                for sub in ("dashboard_id", "genie_space_id"):
                    if value.get(sub) is not None:
                        row[sub] = value[sub]
                continue
            k = _ALIASES.get(k, k)
            if k in FIELDS:
                row[k] = value
            else:
                ignored.add(str(key))
        text = row.get("statement_text")
        if not isinstance(text, str) or not text.strip():
            intake.empty_rows += 1
            continue
        sid = _text(row.get("statement_id"))
        if sid is not None:
            if sid in seen:
                intake.duplicate_executions += 1
                continue
            seen.add(sid)
        intake.executions.append(Execution(
            statement_text=text, statement_id=sid,
            start_time=_text(row.get("start_time")),
            executed_by=_text(row.get("executed_by")),
            client_application=_text(row.get("client_application")),
            catalog=_text(row.get("catalog")), schema=_text(row.get("schema")),
            query_tag=_text(row.get("query_tag")),
            dashboard_id=_text(row.get("dashboard_id")),
            genie_space_id=_text(row.get("genie_space_id")),
            session_id=_text(row.get("session_id")),
            statement_type=_text(row.get("statement_type"))))
    intake.ignored_columns = sorted(ignored)
    return intake


def _text(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def qualify_tables(ast: exp.Expression, catalog: str | None, schema: str | None,
                   depth: int) -> None:
    """Fill omitted LEADING qualifiers of every table reference from one
    execution's own session context — exactly what the warehouse did when it
    ran the statement. CTE references are never touched, nothing is dropped,
    and a qualifier the context does not supply stays missing (so the
    under-qualified gap still names it)."""
    if depth < 2:
        return
    ctes = {c.alias_or_name.lower() for c in ast.find_all(exp.CTE)}
    for table in ast.find_all(exp.Table):
        if not table.name or table.args.get("catalog"):
            continue
        if not table.args.get("db") and table.name.lower() in ctes:
            continue
        if not table.args.get("db"):
            if not schema:
                continue
            table.set("db", exp.to_identifier(schema))
        if depth >= 3 and catalog and not table.args.get("catalog"):
            table.set("catalog", exp.to_identifier(catalog))
