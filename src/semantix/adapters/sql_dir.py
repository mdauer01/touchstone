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
from semantix.extraction.sqlutil import table_fqname

_DIRECTIVE_RE = re.compile(r"--\s*semantix:\s*relation=([\w.\"]+)", re.IGNORECASE)


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
    artifacts: list[ComputedArtifact] = []
    skipped: list[dict] = []
    for path in sorted(rootp.rglob("*.sql")):
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
        if directive:
            relation = directive.group(1).replace('"', "")
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
        body_sql = body.sql(dialect=dialect)
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
