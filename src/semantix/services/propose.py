"""Resolution proposals: the loop closes through a human, in the review rail.

Two verdict classes have a mechanical next step, and this service takes it
as far as a machine honestly can:

* CONTRADICTION — the declared definition and a computed one disagree. The
  declaration-side fix is mechanical: emit the definition the code actually
  computes, in the declaration's own format (emit.py). The code-side fix is
  DESCRIBED from the component diff, never patched — rewriting someone's
  model SQL is not a change a verifier should make. Which side is the truth
  is unknowable from outside: the code may have drifted from intent, or the
  declaration may be stale. The memo presents both directions; the human
  picks by merging the declaration or changing the code.

* SHADOW_METRIC — computed, declared nowhere. The declaration is drafted
  from the fingerprint so the owner can accept it into the ontology or
  delete the model. Undeclared is not a stable state.

Output is a proposals directory: one declaration file per candidate and a
RESOLUTIONS.md written to serve as a PR body — the review rail the team
already has. No PR is opened and no file in the estate is edited: a
proposal that silently applied itself would be the verifier deciding
meaning, which is the failure it exists to catch.
"""

from __future__ import annotations

import json
from pathlib import Path

from semantix.models import PROOF_LEVELS, SemanticCore, core_components, core_diff
from semantix.services.emit import Emitted, emit_declaration
from semantix.services.resolution import normalize_name
from semantix.store import Store


def build_proposals(store: Store, fmt: str = "metrics",
                    concept: str | None = None) -> dict:
    scan = store.latest_scan()
    if scan is None:
        raise ValueError("no scans recorded — run `semantix scan` first")
    verdict_types: dict[str, set[str]] = {}
    severity: dict[str, int] = {}
    for v in store.verdicts_for_scan(scan["scan_id"]):
        verdict_types.setdefault(v["concept_label"], set()).add(v["verdict"])
        severity[v["concept_label"]] = max(severity.get(v["concept_label"], 0),
                                           v["severity"])

    proposals: list[dict] = []
    for c in store.concepts_for_scan(scan["scan_id"]):
        label = c["label"]
        if concept and label.lower() != concept.lower():
            continue
        types = verdict_types.get(label, set())
        if "CONTRADICTION" in types:
            kind = "resolution"
        elif "SHADOW_METRIC" in types:
            kind = "promotion"
        else:
            continue

        computed = _computed_definitions(store, c["members"])
        declared = _declared_definitions(store, c["members"])
        candidates = []
        for definition in computed:
            emitted, reasons = emit_declaration(
                label, definition["core"], definition["dialect"], fmt)
            candidates.append({
                "sites": definition["sites"],
                "semantic_hash": definition["hash"][:12],
                "declaration": emitted,
                "refused": reasons,
                # What the CODE would have to change to match each declaration.
                "code_changes": [
                    {"declared_by": d["name"], "system": d["system"],
                     "diff": core_diff(d["components"], definition["components"],
                                       "declared", "computed")}
                    for d in declared
                ],
            })
        proposals.append({
            "concept": label, "kind": kind,
            "severity": severity.get(label, 0),
            "declared": [{"name": d["name"], "system": d["system"],
                          "model": d["model"]} for d in declared],
            "candidates": candidates,
            "excluded_below_proof": (
                len(c["members"].get("fingerprints") or []) - sum(
                    len(d["sites"]) for d in computed)),
        })
    return {"scan_id": scan["scan_id"], "scanned_at": scan["started_at"],
            "format": fmt, "proposals": proposals}


def write_proposals(result: dict, out_dir: str | Path) -> dict:
    """Write declaration files + RESOLUTIONS.md; returns what was written."""
    out = Path(out_dir)
    decl_dir = out / "declarations"
    decl_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for p in result["proposals"]:
        for cand in p["candidates"]:
            emitted: Emitted | None = cand["declaration"]
            if emitted is None:
                continue
            site = cand["sites"][0].split(".")[-1] if cand["sites"] else "computed"
            path = decl_dir / (f"{normalize_name(p['concept'])}__"
                               f"{normalize_name(site)}{emitted.suffix}")
            path.write_text(emitted.text, encoding="utf-8")
            cand["file"] = str(path)
            written.append(str(path))
    memo = out / "RESOLUTIONS.md"
    memo.write_text(render_memo(result), encoding="utf-8")
    return {"memo": str(memo), "declarations": written,
            "proposals": len(result["proposals"])}


def render_memo(result: dict) -> str:
    lines = [f"# semantix resolution proposals — scan `{result['scan_id']}` "
             f"({result['scanned_at']})", "",
             "Generated declarations describe what the code **computes today**. "
             "For each contradiction there are two honest resolutions and only "
             "a human can pick: **A** — the code is right and the ontology "
             "drifted, so replace the declaration; **B** — the ontology is "
             "right and the code drifted, so change the model. Nothing here "
             "has been applied.", ""]
    if not result["proposals"]:
        lines.append("_No contradictions or shadow metrics in the latest scan._")
        return "\n".join(lines) + "\n"
    for p in result["proposals"]:
        head = "CONTRADICTION" if p["kind"] == "resolution" else "SHADOW_METRIC"
        lines += [f"## {p['concept']} — {head} (sev {p['severity']})", ""]
        if p["declared"]:
            lines.append("Declared by: " + ", ".join(
                f"{d['system']}:{d['name']}" + (f" over `{d['model']}`" if d["model"] else "")
                for d in p["declared"]))
        else:
            lines.append("Declared nowhere.")
        if p["excluded_below_proof"]:
            lines.append(f"_{p['excluded_below_proof']} computed definition(s) "
                         f"below proof grade were not proposed from._")
        lines.append("")
        for i, cand in enumerate(p["candidates"], 1):
            sites = ", ".join(f"`{s}`" for s in cand["sites"])
            if p["kind"] == "resolution":
                lines.append(f"### Option A{i if len(p['candidates']) > 1 else ''} · "
                             f"update the declaration to match {sites}")
            else:
                lines.append(f"### Proposed declaration · as computed at {sites}")
            if cand["declaration"] is not None:
                lines.append(f"File: `{cand.get('file', '(not written)')}`")
                lines += ["", "```" + ("json" if cand["declaration"].format == "ossie" else "yaml"),
                          cand["declaration"].text.rstrip(), "```", ""]
            else:
                lines.append("Could not be emitted: " + "; ".join(cand["refused"]))
                lines.append("")
            if p["kind"] == "resolution":
                for change in cand["code_changes"]:
                    lines.append(f"### Option B · change the code at {sites} to match "
                                 f"{change['system']}:{change['declared_by']}")
                    if change["diff"]:
                        for component, sides in change["diff"].items():
                            lines.append(f"- **{component}** — declared "
                                         f"`{sides['declared']}` · computed "
                                         f"`{sides['computed']}`")
                    else:
                        lines.append("- no component difference recorded")
                    lines.append("")
            elif p["kind"] == "promotion":
                lines.append("Accept by adding this declaration to the ontology, "
                             "or delete the model. Undeclared is not a stable state.")
                lines.append("")
    lines.append("---")
    lines.append("Open as a PR with this file as the body, e.g. "
                 "`gh pr create --body-file RESOLUTIONS.md`.")
    return "\n".join(lines) + "\n"


def _computed_definitions(store: Store, members: dict) -> list[dict]:
    """Distinct proof-grade computed cores with the sites that compute them."""
    by_hash: dict[str, dict] = {}
    for fp_id in members.get("fingerprints") or []:
        row = store.fingerprint_row(fp_id)
        if row is None or row["canon_level"] not in PROOF_LEVELS:
            continue
        core = SemanticCore.from_row(row)
        entry = by_hash.setdefault(row["semantic_hash"], {
            "hash": row["semantic_hash"], "core": core,
            "dialect": row.get("dialect") or "duckdb", "sites": [],
            "components": core_components(core)})
        if row.get("relation"):
            entry["sites"].append(row["relation"])
    for entry in by_hash.values():
        entry["sites"] = sorted(set(entry["sites"]))
    return sorted(by_hash.values(), key=lambda e: (-len(e["sites"]), e["hash"]))


def _declared_definitions(store: Store, members: dict) -> list[dict]:
    out = []
    for claim_id in members.get("claims") or []:
        row = store.claim_row(claim_id)
        if row is None or not row["expressible"] or not row["semantic_hash"]:
            continue
        prov = json.loads(row.get("provenance_json") or "{}")
        out.append({"name": row["name"], "system": prov.get("system", "?"),
                    "model": row.get("model"),
                    "components": core_components(SemanticCore.from_row(row))})
    return out
