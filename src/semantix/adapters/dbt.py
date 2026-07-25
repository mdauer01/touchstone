"""dbt adapter: manifest.json + compiled SQL (§2, v0 adapter set).

Accepts a project directory containing `manifest.json` or `target/manifest.json`.
Compiled SQL is resolved per node in order of fidelity: compiled file on disk,
`compiled_code` embedded in the manifest, then `raw_code` only if it contains
no unrendered Jinja.
"""

from __future__ import annotations

import json
from pathlib import Path

from semantix.adapters.base import ComputedArtifact, topo_sort


def load_dbt(project_dir: str | Path) -> list[ComputedArtifact]:
    """Artifacts only. Prefer load_dbt_detailed where the caller can report
    what was skipped — a dropped model is a dropped definition."""
    return load_dbt_detailed(project_dir)[0]


def load_dbt_detailed(
    project_dir: str | Path,
) -> tuple[list[ComputedArtifact], list[dict]]:
    """(artifacts, skipped). A model with no compiled SQL cannot be
    fingerprinted, but it must never vanish silently: a scan that quietly
    ignores the one model contradicting a declared metric reports CERTIFIED,
    which is exactly backwards. Callers surface `skipped` so coverage numbers
    describe the whole estate rather than only the parts we could read.
    """
    root = Path(project_dir)
    manifest_path = _find_manifest(root)
    data = json.loads(manifest_path.read_text())

    nodes = {
        uid: node
        for uid, node in (data.get("nodes") or {}).items()
        if node.get("resource_type") == "model"
    }
    relation_of = {uid: _relation(node) for uid, node in nodes.items()}

    artifacts: list[ComputedArtifact] = []
    skipped: list[dict] = []
    for uid, node in nodes.items():
        sql, path = _compiled_sql(root, node)
        if sql is None:
            raw = node.get("raw_code") or ""
            skipped.append({
                "object_id": uid,
                "relation": relation_of[uid],
                "path": node.get("original_file_path"),
                "reason": ("unrendered Jinja and no compiled SQL — run `dbt compile`"
                           if "{{" in raw else "no compiled SQL in manifest or target/"),
            })
            continue
        depends = sorted(
            relation_of[dep]
            for dep in (node.get("depends_on") or {}).get("nodes", [])
            if dep in relation_of
        )
        artifacts.append(
            ComputedArtifact(
                system="dbt",
                object_id=uid,
                relation=relation_of[uid],
                sql=sql,
                path=path,
                depends_on=depends,
            )
        )
    return topo_sort(artifacts), skipped


def _find_manifest(root: Path) -> Path:
    for candidate in (root / "manifest.json", root / "target" / "manifest.json"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no manifest.json under {root}")


def _relation(node: dict) -> str:
    schema = node.get("schema") or "public"
    name = node.get("alias") or node.get("name")
    return f"{schema}.{name}"


def _compiled_sql(root: Path, node: dict) -> tuple[str | None, str | None]:
    compiled_path = node.get("compiled_path")
    if compiled_path:
        p = root / compiled_path
        if p.is_file():
            return p.read_text(), str(p)
    compiled_code = node.get("compiled_code")
    if compiled_code:
        return compiled_code, node.get("original_file_path")
    raw_code = node.get("raw_code")
    if raw_code and "{{" not in raw_code:
        return raw_code, node.get("original_file_path")
    return None, None


