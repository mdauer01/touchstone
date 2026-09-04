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
  picks by merging the declaration or changing the code. A computed
  definition that already EQUALS a declaration needs neither, and the memo
  says so instead of proposing a no-op.

* SHADOW_METRIC — computed, declared nowhere. The declaration is drafted
  from the fingerprint so the owner can accept it into the ontology or
  delete the model. Undeclared is not a stable state.

Output is a proposals directory: one declaration file per candidate and a
RESOLUTIONS.md written to serve as a PR body — the review rail the team
already has. No PR is opened and no file in the estate is edited: a
proposal that silently applied itself would be the verifier deciding
meaning, which is the failure it exists to catch. With the estate sources
passed, every emitted declaration is round-tripped through its own adapter
before it is written; without them the memo marks each as unverified.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from semantix.models import PROOF_LEVELS, SemanticCore, core_components, core_diff
from semantix.services.emit import Emitted, emit_declaration
from semantix.services.resolution import normalize_name
from semantix.store import Store


def build_proposals(store: Store, fmt: str = "metrics",
                    concept: str | None = None, registry=None,
                    dialect: str | None = None) -> dict:
    scan = store.latest_scan()
    if scan is None:
        raise ValueError("no scans recorded — run `semantix scan` first")
    dialect = dialect or scan.get("dialect") or "duckdb"
    concepts = store.concepts_for_scan(scan["scan_id"])
    target = normalize_name(concept) if concept else None
    if target is not None and target not in {c["label"] for c in concepts}:
        # A non-matching --concept produced a memo asserting "no contradictions
        # in the latest scan" — false, and exit 0.
        raise ValueError(
            f"no concept named {concept!r} in scan {scan['scan_id']} "
            f"(known: {', '.join(sorted(c['label'] for c in concepts)) or 'none'})")

    verdict_types: dict[str, set[str]] = {}
    severity: dict[str, int] = {}
    for v in store.verdicts_for_scan(scan["scan_id"]):
        verdict_types.setdefault(v["concept_label"], set()).add(v["verdict"])
        severity[v["concept_label"]] = max(severity.get(v["concept_label"], 0),
                                           v["severity"])

    proposals: list[dict] = []
    for c in concepts:
        label = c["label"]
        if target is not None and label != target:
            continue
        types = verdict_types.get(label, set())
        if "CONTRADICTION" in types:
            kind = "resolution"
        elif "SHADOW_METRIC" in types:
            kind = "promotion"
        else:
            continue

        computed, excluded = _computed_definitions(store, c["members"])
        declared = _declared_definitions(store, c["members"])
        expressible = [d for d in declared if d["expressible"]]
        candidates = []
        for definition in computed:
            same = [f"{d['system']}:{d['name']}" for d in expressible
                    if d["hash"] == definition["hash"]]
            if same:
                candidates.append({
                    "sites": definition["sites"], "semantic_hash": definition["hash"][:12],
                    "declaration": None, "refused": [], "matches_declaration": same,
                    "code_changes": []})
                continue
            emitted, reasons = emit_declaration(
                label, definition["core"], definition["dialect"], fmt, registry=registry)
            candidates.append({
                "sites": definition["sites"],
                "semantic_hash": definition["hash"][:12],
                "declaration": emitted,
                "refused": reasons,
                "matches_declaration": [],
                # What the CODE would have to change to match each declaration.
                "code_changes": [
                    {"declared_by": d["name"], "system": d["system"],
                     "diff": core_diff(d["components"], definition["components"],
                                       "declared", "computed")}
                    for d in expressible
                    if core_diff(d["components"], definition["components"],
                                 "declared", "computed")
                ],
            })
        proposals.append({
            "concept": label, "kind": kind,
            "severity": severity.get(label, 0),
            "declared": [{"name": d["name"], "system": d["system"], "model": d["model"],
                          "expressible": d["expressible"], "error": d["error"]}
                         for d in declared],
            "candidates": candidates,
            "excluded_below_proof": excluded,
        })
    return {"scan_id": scan["scan_id"], "scanned_at": scan["started_at"],
            "format": fmt, "proposals": proposals,
            "filtered_to": concept, "verified": registry is not None}


def write_proposals(result: dict, out_dir: str | Path) -> dict:
    """Write declaration files + RESOLUTIONS.md; returns what was written.
    The declarations directory is OURS and is rebuilt from scratch: a stale
    file from a previous run beside a fresh memo is a proposal nobody made."""
    out = Path(out_dir)
    decl_dir = out / "declarations"
    if decl_dir.exists():
        shutil.rmtree(decl_dir)
    decl_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for p in result["proposals"]:
        for cand in p["candidates"]:
            emitted: Emitted | None = cand["declaration"]
            if emitted is None:
                continue
            site = normalize_name(cand["sites"][0]) if cand["sites"] else "computed"
            # Full relation + hash: two sites sharing a table name in different
            # schemas overwrote each other's file silently.
            path = decl_dir / (f"{normalize_name(p['concept']) or 'concept'}__{site}__"
                               f"{cand['semantic_hash'][:8]}{emitted.suffix}")
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
             "For a contradiction with a declaration there are two honest "
             "resolutions and only a human can pick: **A** — the code is right and "
             "the ontology drifted, so replace the declaration; **B** — the "
             "ontology is right and the code drifted, so change the model. "
             "Nothing here has been applied.", ""]
    if result.get("filtered_to"):
        lines += [f"_Filtered to concept `{result['filtered_to']}`; other concepts "
                  f"in this scan are not shown._", ""]
    if not result.get("verified"):
        lines += ["_Round trips NOT verified: pass the estate sources "
                  "(`--sql-dir`/`--dbt`/`--views` and `--catalog`) so each emitted "
                  "declaration is checked against the scan's registry before it "
                  "is offered._", ""]
    if not result["proposals"]:
        lines.append("_No contradictions or shadow metrics"
                     + (" for this concept" if result.get("filtered_to") else
                        " in the latest scan") + "._")
        return "\n".join(lines) + "\n"
    for p in result["proposals"]:
        head = "CONTRADICTION" if p["kind"] == "resolution" else "SHADOW_METRIC"
        lines += [f"## {p['concept']} — {head} (sev {p['severity']})", ""]
        if p["declared"]:
            parts = []
            for d in p["declared"]:
                text = f"{d['system']}:{d['name']}" + (f" over `{d['model']}`" if d["model"] else "")
                if not d["expressible"]:
                    text += f" (not machine-readable: {(d['error'] or 'no reason recorded')[:120]})"
                parts.append(text)
            lines.append("Declared by: " + "; ".join(parts))
        elif p["kind"] == "resolution":
            lines.append("Declared nowhere — the code disagrees with ITSELF across "
                         "sites. Option A below would CREATE a declaration for one "
                         "side; there is no Option B, because there is no "
                         "declaration to hold the code to.")
        else:
            lines.append("Declared nowhere.")
        if p["excluded_below_proof"]:
            lines.append(f"_{p['excluded_below_proof']} computed definition(s) "
                         f"below proof grade were not proposed from._")
        lines.append("")
        multiple = len(p["candidates"]) > 1
        for i, cand in enumerate(p["candidates"], 1):
            sites = ", ".join(f"`{s}`" for s in cand["sites"])
            if cand["matches_declaration"]:
                lines += [f"### {sites} already matches "
                          f"{', '.join(cand['matches_declaration'])} — no change needed", ""]
                continue
            if p["kind"] == "resolution":
                lines.append(f"### Option A{i if multiple else ''} · update the "
                             f"declaration to match {sites}")
            else:
                lines.append(f"### Proposed declaration · as computed at {sites}")
            if cand["declaration"] is not None:
                lines.append(f"File: `{cand.get('file', '(not written)')}`"
                             + ("  — round trip verified against the estate"
                                if cand["declaration"].verified else
                                "  — round trip NOT verified (no estate sources passed)"))
                lines += ["", "```" + ("json" if cand["declaration"].format == "ossie" else "yaml"),
                          cand["declaration"].text.rstrip(), "```", ""]
            else:
                lines.append("Could not be emitted: " + "; ".join(cand["refused"]))
                lines.append("")
            if p["kind"] == "resolution":
                for change in cand["code_changes"]:
                    lines.append(f"### Option B · change the code at {sites} to match "
                                 f"{change['system']}:{change['declared_by']}")
                    for component, sides in change["diff"].items():
                        lines.append(f"- **{component}** — declared "
                                     f"`{sides['declared']}` · computed "
                                     f"`{sides['computed']}`")
                    lines.append("")
            elif p["kind"] == "promotion":
                lines.append("Accept by adding this declaration to the ontology, "
                             "or delete the model. Undeclared is not a stable state.")
                lines.append("")
    lines.append("---")
    lines.append("Open as a PR with this file as the body, e.g. "
                 "`gh pr create --body-file RESOLUTIONS.md`.")
    return "\n".join(lines) + "\n"


def _computed_definitions(store: Store, members: dict) -> tuple[list[dict], int]:
    """(distinct proof-grade computed cores with their sites, count of
    fingerprint rows below proof grade). Rows are per site since round
    seven, so every site of a shared core is listed."""
    by_hash: dict[str, dict] = {}
    excluded = 0
    for fp_id in members.get("fingerprints") or []:
        row = store.fingerprint_row(fp_id)
        if row is None:
            continue
        if row["canon_level"] not in PROOF_LEVELS:
            excluded += 1
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
    return sorted(by_hash.values(), key=lambda e: (-len(e["sites"]), e["hash"])), excluded


def _declared_definitions(store: Store, members: dict) -> list[dict]:
    """Every declared claim, expressible or not — a declaration the engine
    could not read is still a declaration, and describing the concept as
    'declared nowhere' misled the reviewer."""
    out = []
    for claim_id in members.get("claims") or []:
        row = store.claim_row(claim_id)
        if row is None:
            continue
        prov = json.loads(row.get("provenance_json") or "{}")
        expressible = bool(row["expressible"] and row["semantic_hash"])
        out.append({"name": row["name"], "system": prov.get("system", "?"),
                    "model": row.get("model"), "expressible": expressible,
                    "error": row.get("error"), "hash": row.get("semantic_hash"),
                    "components": core_components(SemanticCore.from_row(row))
                    if expressible else None})
    return out
