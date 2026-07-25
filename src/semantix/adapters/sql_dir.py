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


def load_sql_dir(root: str | Path, dialect: str = "duckdb") -> list[ComputedArtifact]:
    rootp = Path(root)
    artifacts: list[ComputedArtifact] = []
    for path in sorted(rootp.rglob("*.sql")):
        sql = path.read_text()
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
    return topo_sort(artifacts)


def load_views_file(path: str | Path, dialect: str = "duckdb") -> list[ComputedArtifact]:
    text = Path(path).read_text()
    try:
        statements = sqlglot.parse(text, read=dialect)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"could not parse view dump {path}: {e}") from e

    artifacts: list[ComputedArtifact] = []
    for stmt in statements:
        if not isinstance(stmt, exp.Create) or stmt.kind != "VIEW":
            continue
        target = stmt.this
        relation = table_fqname(target) if isinstance(target, exp.Table) else target.sql()
        body = stmt.expression
        if body is None:
            continue
        body_sql = body.sql(dialect=dialect)
        artifacts.append(
            ComputedArtifact(
                system="views",
                object_id=relation,
                relation=relation,
                sql=body_sql,
                path=str(path),
                depends_on=_referenced_relations(body_sql, dialect),
            )
        )
    return topo_sort(artifacts)
