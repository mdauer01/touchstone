"""Raw-SQL adapters (§2): estates without dbt — which is most estates.

Two intake shapes, both normalizing to ComputedArtifacts:

* `load_sql_dir` — a directory tree of `.sql` files, one SELECT per file.
  Relation defaults to `<parent-dir>.<stem>` (`analytics/fct_orders.sql` →
  `analytics.fct_orders`); a header directive overrides it:

      -- semantix: relation=analytics.fct_orders

* `load_views_file` — one file of `CREATE [OR REPLACE] VIEW ... AS SELECT`
  statements, i.e. what `SHOW CREATE VIEW` / information_schema dumps
  produce. The view name is the relation; the body is the artifact.

Dependencies (for topological registration) come from the table references
in each statement — no manifest needed. Parsing here is best-effort by
design: a file sqlglot cannot parse still becomes an artifact and degrades
at canonicalization, exactly like any other estate SQL.
"""

from __future__ import annotations

import re
from pathlib import Path

import sqlglot
from sqlglot import exp

from semantix.adapters.base import ComputedArtifact, topo_sort
from semantix.extraction.sqlutil import faithful_render, table_fqname

# Each dotted part may be double-quoted, backtick-quoted, or bare. The old
# character-class regex stopped a quoted name at its first SPACE — so
# `relation="MY DB".analytics.orders` bound the artifact to a relation named
# `MY` (a fabricated identity), and a backtick-quoted name silently fell back
# to the path-derived default. An explicitly declared identity must be read
# whole or not at all.
_DIRECTIVE_PART = r'(?:"[^"]+"|`[^`]+`|[\w$]+)'
_DIRECTIVE_RE = re.compile(
    rf"--\s*semantix:\s*relation=({_DIRECTIVE_PART}(?:\.{_DIRECTIVE_PART})*)",
    re.IGNORECASE,
)


def _referenced_relations(sql: str, dialect: str) -> list[str]:
    try:
        ast = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # noqa: BLE001 — deps are an ordering hint, not a gate
        return []
    return sorted({table_fqname(t) for t in ast.find_all(exp.Table)})


def load_sql_dir(root: str | Path, dialect: str = "duckdb") -> tuple[list[ComputedArtifact],
                                                                    list[dict]]:
    """(artifacts, skipped). A file we ENUMERATED but could not read is
    returned as `skipped`, never dropped.

    Dropping it silently was the worst defect in this file: the estate then
    looked one model smaller than it is, so a concept whose only contradictor
    was that file certified — and the receipt asserted `scope.complete: true`
    over an estate it had not read. A later scan then recorded the file as
    DELETED, manufacturing absence evidence out of an I/O error.
    """
    rootp = Path(root)
    if not rootp.is_dir():
        # A typo'd --sql-dir completed a scan with 0 artifacts and exit 0,
        # became latest_scan, "resolved" every standing contradiction in
        # `diff`, emptied the verdicts projection, and signed an ORPHAN_CLAIM
        # receipt beside the concept's own computed evidence. --dbt and
        # --views already refused; this was the one source that did not.
        raise ValueError(f"{rootp}: not a directory")
    artifacts: list[ComputedArtifact] = []
    skipped: list[dict] = []
    # Case-insensitive: `rglob("*.sql")` silently skipped `.SQL` and `.Sql`
    # files, and a dropped artifact is a missing contradictor.
    candidates = sorted(
        p for p in rootp.rglob("*") if p.is_file() and p.suffix.lower() == ".sql"
    )
    # An EXISTING directory with no .sql files is left to the CLI gate
    # (`--fail-on` fails an empty estate by design, with its own message) and
    # to the narrowed-scan semantics the store encodes; only a path that is
    # not a directory at all is refused here.
    for path in candidates:
        rel_path = path.relative_to(rootp)
        object_id = str(rel_path)
        # Estate files are not guaranteed UTF-8 and may be unreadable; a
        # single mis-encoded byte used to abort the whole scan.
        try:
            # utf-8-sig, not utf-8: a Windows- or SSMS-authored file carries a
            # BOM, which utf-8 keeps as a leading ﻿ and every SQL parser
            # then rejects — the whole model lost to an invisible character.
            sql = path.read_text(encoding="utf-8-sig", errors="strict")
        except UnicodeDecodeError as e:
            # `errors="replace"` was worse than failing: a latin-1 byte inside
            # a string literal became U+FFFD, the file canonicalized cleanly,
            # and we fingerprinted a DIFFERENT predicate than production runs
            # — at proof grade. A file we cannot decode is a file we cannot
            # speak for.
            skipped.append({"object_id": object_id, "path": str(path),
                            "reason": f"not decodable as UTF-8: {e}"})
            continue
        except OSError as e:
            skipped.append({"object_id": object_id, "path": str(path),
                            "reason": f"could not be read: {e}"})
            continue
        directive = _DIRECTIVE_RE.search(sql)
        marker_present = "semantix:" in sql.lower() and "relation=" in sql.lower()
        if directive:
            declared = directive.group(1)
            line = next((ln for ln in sql.splitlines() if "relation=" in ln.lower()), "")
            tail = line.lower().split("relation=", 1)[1].strip() if "relation=" in line.lower() else ""
            if tail and tail != declared.lower():
                # The regex matched a PREFIX of a malformed declared identity
                # (`"A"b`, a trailing dot) — binding a prefix of what the
                # author wrote is neither "whole" nor "not at all".
                skipped.append({"object_id": object_id, "path": str(path),
                                "reason": f"malformed relation directive: {line.strip()!r}"})
                continue
            relation = declared.replace('"', "").replace("`", "")
        elif marker_present:
            skipped.append({"object_id": object_id, "path": str(path),
                            "reason": "malformed `-- semantix: relation=` directive "
                                      "— fix or remove it"})
            continue
        else:
            rel_path = path.relative_to(rootp)
            schema = rel_path.parent.name or "public"
            relation = f"{schema}.{rel_path.stem}"
        artifacts.append(
            ComputedArtifact(
                system="sql",
                object_id=str(path.relative_to(rootp)),
                relation=relation,
                sql=sql,
                path=str(path),
                depends_on=_referenced_relations(sql, dialect),
            )
        )
    return topo_sort(artifacts), skipped


def load_views_file(path: str | Path,
                    dialect: str = "duckdb") -> tuple[list[ComputedArtifact], list[dict]]:
    """(artifacts, skipped). Same contract as `load_sql_dir`: a statement we
    read but could not turn into a view definition is REPORTED, not dropped.

    Dropping them meant a five-view dump could produce four artifacts and say
    nothing about the fifth — silent definition loss, the one thing every
    other part of this codebase treats as the cardinal sin.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8-sig", errors="strict")
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"could not read view dump {p}: {e}") from e
    try:
        statements = sqlglot.parse(text, read=dialect)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"could not parse view dump {p}: {e}") from e

    artifacts: list[ComputedArtifact] = []
    skipped: list[dict] = []
    seen_at: dict[str, int] = {}
    for index, stmt in enumerate(statements):
        if stmt is None:
            continue
        if not isinstance(stmt, exp.Create) or stmt.kind != "VIEW":
            skipped.append({
                "object_id": f"{p.name}#{index}", "path": str(p),
                "reason": f"not a CREATE VIEW statement ({type(stmt).__name__})",
            })
            continue
        target = stmt.this
        relation = table_fqname(target) if isinstance(target, exp.Table) else target.sql()
        body = stmt.expression
        if body is None:
            skipped.append({"object_id": relation, "path": str(p),
                            "reason": "CREATE VIEW carries no body"})
            continue
        if relation in seen_at:
            # Later wins — that is what OR REPLACE means, and a replay-shaped
            # dump's end state is its truth. But the supersession must be
            # REPORTED: both statements share (system, object_id), so
            # topo_sort's keyed dedup collapsed them before
            # duplicate_relations could ever see the collision, and the
            # superseded contradictor vanished without a trace — the one
            # remaining silent-loss channel in the non-dbt intake path.
            skipped.append({
                "object_id": f"{relation}#{seen_at[relation]}", "path": str(p),
                # INFORMATIONAL: the statement WAS read and its content is
                # known — OR REPLACE means the end state is the truth. Routing
                # this through the unreadable-artifact channel permanently
                # un-certified the estate with a false reason ("could not be
                # read") on a routine replay dump.
                "informational": True,
                "reason": (f"definition of {relation} at statement "
                           f"{seen_at[relation]} superseded by a later CREATE "
                           f"VIEW in the same dump — only the last definition "
                           f"was scanned"),
            })
        seen_at[relation] = index
        body_sql = faithful_render(body, dialect)
        if body_sql is None:
            # The dialect's generator would rewrite this body (Spark hoists a
            # derived table's own WITH), so its text is not what the view
            # computes — reported, never scanned as something else.
            skipped.append({"object_id": relation, "path": str(p),
                            "reason": "the view body cannot be re-rendered faithfully in "
                                      f"{dialect} (e.g. a nested WITH the generator "
                                      "hoists) — not scanned"})
            continue
        artifacts.append(
            ComputedArtifact(
                system="views",
                object_id=relation,
                relation=relation,
                sql=body_sql,
                path=str(p),
                depends_on=_referenced_relations(body_sql, dialect),
            )
        )
    return topo_sort(artifacts), skipped
