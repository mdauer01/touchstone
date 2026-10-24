"""Scan orchestrator: adapters → extraction → store → resolution →
reconciliation → interfaces. One scan is one atomic observation of the
estate; everything downstream of the store is a read model."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from semantix.adapters.agents_schema import load_agents_schema
from semantix.adapters.base import (
    ComputedArtifact,
    DeclaredSpec,
    duplicate_relations,
    spec_content_text,
    topo_sort,
)
from semantix.adapters.catalog import load_catalog
from semantix.adapters.dbt import load_dbt_detailed, load_dbt_metrics
from semantix.adapters.metrics_yaml import load_metrics
from semantix.adapters.ossie import load_ossie
from semantix.adapters.sql_dir import load_sql_dir, load_views_file
from semantix.adapters.uc_metrics import load_uc_metrics
from semantix.extraction.canonicalize import canonicalize_sql
from semantix.extraction.fingerprint import extract_fingerprints
from semantix.extraction.registry import SchemaRegistry
from semantix.models import (
    PROOF_LEVELS,
    CanonLevel,
    Claim,
    Concept,
    Fingerprint,
    Provenance,
    Verdict,
    VerdictType,
)
from semantix.services.reconcile import reconcile
from semantix.services.relationships import same_relation as same_rel
from semantix.services.resolution import resolve_concepts
from semantix.store import Store
from semantix.toolchain import QUALIFIED_SQLGLOT, toolchain_status


@dataclass
class ScanResult:
    scan_id: str
    observed_at: str
    label: str | None
    dialect: str
    artifacts: int
    fingerprints: list[Fingerprint]
    claims: list[Claim]
    concepts: list[Concept]
    verdicts: list[Verdict]
    canon_levels: dict[str, int] = field(default_factory=dict)
    execution: dict | None = None  # set when a seed dataset was executed against
    # Estate the scan could NOT read, surfaced so coverage is never mistaken
    # for completeness: models with no compiled SQL, artifacts that yielded no
    # measure, and relations claimed by more than one artifact.
    skipped: list[dict] = field(default_factory=list)
    unmeasured: list[str] = field(default_factory=list)
    duplicate_relations: dict[str, list[str]] = field(default_factory=dict)
    # Artifacts whose SQL would not PARSE. Computed since the completeness gate
    # landed but never exposed, so an operator saw only "yielded no measure" —
    # which reads as "this model defines no metric" and actively misled them
    # about which models the engine could not read at all.
    unparseable: list[str] = field(default_factory=list)
    # Session defaults the operator supplied for omitted qualifiers; recorded
    # so monitor/propose rebuild the SAME identity space the scan used.
    execution_context: dict = field(default_factory=dict)
    # Parser version the equalities were computed under (toolchain.py).
    toolchain: dict = field(default_factory=dict)

    @property
    def parse_coverage(self) -> float:
        """% of computed artifacts that parsed and QUALIFIED (optimized or
        qualified). A statement about reading the SQL — not about whether any
        measure in it can be verified."""
        total = sum(self.canon_levels.values())
        if not total:
            return 0.0
        good = self.canon_levels.get("optimized", 0) + self.canon_levels.get("qualified", 0)
        return 100.0 * good / total

    @property
    def unread_artifacts(self) -> int:
        """Artifacts whose measures we never saw: SQL that failed to parse,
        and enumerated models with no SQL at all."""
        return (self.canon_levels.get(CanonLevel.FAILED.value, 0)
                + sum(1 for s in self.skipped if not s.get("informational")))

    @property
    def verifiable_measures(self) -> tuple[int, int]:
        """(computed measures at proof grade, measures in scope). An artifact
        we could not read counts as at least one unverifiable measure:
        counting only the fingerprints we managed to extract let an estate
        with an unparseable model still report 100%."""
        total = len(self.fingerprints) + self.unread_artifacts
        good = sum(1 for fp in self.fingerprints if fp.canon_level in PROOF_LEVELS)
        return good, total

    @property
    def coverage(self) -> float:
        """% of computed MEASURES that reached proof grade — the number to
        judge readiness by. It was the parse level recorded BEFORE the
        completeness gate, so an estate whose only metric used TABLESAMPLE
        reported 100% while nothing in it could be certified."""
        good, total = self.verifiable_measures
        return 100.0 * good / total if total else 0.0

    def stats(self) -> dict:
        return {
            "artifacts": self.artifacts,
            "fingerprints": len(self.fingerprints),
            "claims": len(self.claims),
            "concepts": len(self.concepts),
            "verdicts": Counter(v.verdict_type.value for v in self.verdicts),
            "canon_levels": self.canon_levels,
            "coverage_pct": round(self.coverage, 1),
            "parse_coverage_pct": round(self.parse_coverage, 1),
            "verifiable_measures": list(self.verifiable_measures),
            "execution_context": self.execution_context,
            "toolchain": self.toolchain,
            "skipped": len(self.skipped),
            "unmeasured": len(self.unmeasured),
            "duplicate_relations": len(self.duplicate_relations),
        }


def run_scan(
    dbt_path: str | None = None,
    metrics_path: str | None = None,
    catalog_path: str | None = None,
    db_path: str = "semantix.db",
    dialect: str = "duckdb",
    label: str | None = None,
    execute_seed: str | None = None,
    sql_dir: str | None = None,
    views_path: str | None = None,
    agents_schema_path: str | None = None,
    osi_path: str | None = None,
    uc_metrics_path: str | None = None,
    default_database: str | None = None,
    default_schema: str | None = None,
) -> tuple[ScanResult, Store]:
    if not (dbt_path or sql_dir or views_path):
        raise ValueError("no computed-SQL source: pass dbt_path, sql_dir, or views_path")
    store = Store(db_path)
    try:
        return _run_scan(store, dbt_path, metrics_path, catalog_path, dialect,
                         label, execute_seed, sql_dir, views_path,
                         agents_schema_path, osi_path, uc_metrics_path,
                         default_database, default_schema)
    except BaseException:
        # An adapter refusal mid-scan used to leave the store OPEN with its
        # transaction, so the next Store() in the same process hung on the
        # SQLite lock. The unfinished scans row stays (every reader already
        # ignores scans without stats); the connection must not.
        store.close()
        raise


def _run_scan(store: Store, dbt_path, metrics_path, catalog_path, dialect,
              label, execute_seed, sql_dir, views_path, agents_schema_path,
              osi_path, uc_metrics_path, default_database=None,
              default_schema=None) -> tuple[ScanResult, Store]:
    scan_id, observed_at = store.begin_scan(label, dialect)
    toolchain = toolchain_status()

    catalog = load_catalog(catalog_path) if catalog_path else {}
    registry = SchemaRegistry(catalog, default_database, default_schema)

    artifacts: list[ComputedArtifact] = []
    skipped: list[dict] = []
    if dbt_path:
        dbt_artifacts, dbt_skipped = load_dbt_detailed(dbt_path)
        artifacts += dbt_artifacts
        skipped += [{**s, "system": "dbt"} for s in dbt_skipped]
    if sql_dir:
        sql_artifacts, sql_skipped = load_sql_dir(sql_dir, dialect)
        artifacts += sql_artifacts
        skipped += [{**s, "system": "sql"} for s in sql_skipped]
    if views_path:
        view_artifacts, view_skipped = load_views_file(views_path, dialect)
        artifacts += view_artifacts
        skipped += [{**s, "system": "views"} for s in view_skipped]
    # Re-sort globally: cross-source dependencies (a view over a dbt mart)
    # must still register upstream-first.
    artifacts = topo_sort(artifacts)
    # Fixed before any fingerprint: whether a reference is under-qualified
    # must be a property of the estate, never of registration order.
    registry.set_estate_depth([a.relation for a in artifacts]
                              + [d for a in artifacts for d in a.depends_on])
    dupes = duplicate_relations(artifacts)
    # Models the adapter enumerated but could not hand us SQL for. Recording
    # them as known-but-unreadable is what stops a routine partial compile
    # from looking like a deletion.
    for s in skipped:
        if s.get("informational"):
            continue  # content WAS read; disclosure only, never absence-blocking
        store.record_artifact_state(scan_id, s["system"], s["object_id"], readable=False)
    fingerprints: list[Fingerprint] = []
    unmeasured: list[str] = []
    unparseable: list[str] = []
    canon_levels: Counter = Counter()

    # Content per duplicated relation, so the ambiguity qualifier can tell a
    # GENUINE conflict (two different definitions racing for one name) from a
    # vendored identical copy, which is noise.
    dupe_content: dict[str, set[str]] = {rel: set() for rel in dupes}

    for artifact in artifacts:
        canon = canonicalize_sql(artifact.sql, registry.sqlglot_schema(dialect), dialect)
        canon_levels[canon.level.value] += 1
        # Keyed later on the artifact's fingerprint CORES when it has any:
        # rendered text carries CTE NAMES, so two vendored copies differing
        # only in a CTE rename read as "differing content ... decided by scan
        # order" — false by the fingerprint module's own invariant.
        if artifact.relation in dupe_content:
            dupe_pending = artifact.relation
        else:
            dupe_pending = None
        registry.register_model(artifact.relation, canon.ast)
        store.record_artifact(
            kind="computed",
            system=artifact.system,
            object_id=artifact.object_id,
            relation=artifact.relation,
            path=artifact.path,
            raw_text=artifact.sql,
            canon_level=canon.level.value,
            error=canon.error,
            scan_id=scan_id,
        )
        provenance = Provenance(
            system=artifact.system, object_id=artifact.object_id, path=artifact.path
        )
        # Readable iff we actually got an AST to extract from. A failed parse
        # means we cannot speak to what this artifact defines.
        store.record_artifact_state(
            scan_id, artifact.system, artifact.object_id,
            readable=canon.level != CanonLevel.FAILED,
        )
        if canon.level == CanonLevel.FAILED:
            unparseable.append(f"{artifact.system}:{artifact.object_id}")
        found = extract_fingerprints(canon, artifact.relation, provenance, dialect, registry)
        if dupe_pending is not None:
            if found:
                content_key = "|".join(sorted(fp.core.hash() for fp in found))
            else:
                content_key = (canon.ast.sql(dialect=dialect)
                               if canon.ast is not None else artifact.sql)
            dupe_content[dupe_pending].add(content_key)
        if not found:
            # Parsed fine, yielded no measure (a set operation, a non-aggregate
            # model, an unreadable shape). Not an error, but not coverage either.
            unmeasured.append(f"{artifact.system}:{artifact.object_id}")
        for fp in found:
            fingerprints.append(fp)
            store.record_fingerprint(fp, scan_id, observed_at)

    specs: list[DeclaredSpec] = []
    if dbt_path:
        # Both sides of the reconciliation live in the same manifest: reading
        # only `nodes` reported every dbt Semantic Layer metric as a shadow
        # metric while its declaration sat unread in the file already open.
        specs += load_dbt_metrics(dbt_path)
    if metrics_path:
        specs += load_metrics(metrics_path)
    if agents_schema_path:
        specs += load_agents_schema(agents_schema_path)
    # These run AFTER the record_artifact_state loop above, so any
    # non-informational entry is recorded here explicitly — otherwise an
    # unread declared object would be absent from artifact state and a later
    # scan could read its disappearance as a deletion with proof.
    for source_system, path, loader in (("ossie", osi_path, load_ossie),
                                        ("uc_metrics", uc_metrics_path,
                                         load_uc_metrics)):
        if not path:
            continue
        source_specs, source_skipped = loader(path, dialect)
        specs += source_specs
        for s in source_skipped:
            entry = {**s, "system": source_system}
            skipped.append(entry)
            if not entry.get("informational"):
                store.record_artifact_state(scan_id, source_system,
                                            entry["object_id"], readable=False)

    claims: list[Claim] = []
    for spec in specs:
        claim = build_claim(spec, registry, dialect)
        claims.append(claim)
        # raw_text convention is mirrored by services.freshness — the shared
        # spec_content_text() is the single definition of "this claim changed".
        store.record_artifact(
            kind="declared",
            system=spec.system,
            object_id=spec.name,
            relation=spec.model,
            path=spec.path,
            raw_text=spec_content_text(spec),
            canon_level=claim.canon_level,
            error=claim.error,
            scan_id=scan_id,
        )
        store.record_claim(claim, scan_id, observed_at)

    concepts = resolve_concepts(fingerprints, claims)
    verdicts = reconcile(concepts)
    # Everything in the estate we did not understand: enumerated but without
    # SQL, or with SQL we could not parse. Both block certification.
    unread = [f"{s['system']}:{s['object_id']}" for s in skipped
              if not s.get("informational")] + unparseable

    execution: dict | None = None
    if execute_seed:
        # BEFORE the unread-estate downgrade, and before save_results.
        #
        # Ordering matters: annotate_with_execution only looks at CONTRADICTION,
        # CERTIFIED and ALIASING, so running it after the downgrade — which
        # rewrites CERTIFIED/ALIASING to UNRESOLVED — silently switched off
        # execution-based falsification for the entire estate whenever a single
        # unrelated artifact was unreadable. That is exactly backwards: a
        # partially-read estate is the riskiest one, and execution is the only
        # instrument that can falsify a canonical equality.
        from semantix.services.execute import annotate_with_execution

        execution = annotate_with_execution(
            concepts, verdicts, artifacts, execute_seed, dialect
        )
    _qualify_for_unreadable(verdicts, unread)
    _qualify_for_toolchain(verdicts, toolchain)
    contested = {rel: dupes[rel] for rel, contents in dupe_content.items()
                 if len(contents) > 1}
    # TRANSITIVE closure over the dependency graph: a CTE-mediated reader's
    # join_nodes are `cte:<digest>` strings, and a downstream model's
    # source_columns resolve THROUGH the registry past the contested relation
    # to raw terminals — so neither direct test saw non-direct consumers, and
    # a model one hop downstream of the collision kept a signed CERTIFIED
    # with scope.complete: true. The adapters' depends_on edges see every
    # reference (including through CTEs), so contamination propagates there.
    tainted = set(contested)
    for artifact in artifacts:  # already topologically sorted
        if artifact.relation in tainted:
            continue
        if any(any(same_rel(dep, t) for t in tainted) for dep in artifact.depends_on):
            tainted.add(artifact.relation)
    contested_closure = {
        rel: contested.get(rel, [f"downstream of {', '.join(sorted(contested))}"])
        for rel in tainted
    }
    _qualify_for_ambiguous_relations(verdicts, concepts, contested_closure)
    _qualify_for_untranslated_claims(verdicts, concepts)
    store.save_results(scan_id, concepts, verdicts)

    result = ScanResult(
        scan_id=scan_id,
        observed_at=observed_at,
        label=label,
        dialect=dialect,
        artifacts=len(artifacts),
        fingerprints=fingerprints,
        claims=claims,
        concepts=concepts,
        verdicts=verdicts,
        canon_levels=dict(canon_levels),
        execution=execution,
        skipped=skipped,
        unmeasured=unmeasured,
        duplicate_relations=dupes,
        unparseable=unparseable,
        execution_context=registry.execution_context,
        toolchain=toolchain,
    )
    store.finish_scan(scan_id, result.stats())
    return result, store


def _qualify_for_untranslated_claims(verdicts: list[Verdict],
                                     concepts: list[Concept]) -> None:
    """No affirmative equivalence for a concept with an unread declaration.

    Unlike an unreadable SQL artifact — which may define ANY concept, so it
    qualifies everything — an untranslatable DECLARATION is scoped: we know
    exactly which concept it belongs to. But within that concept it is the
    same problem, and it was not treated as one: a scan carrying an
    untranslatable LookML measure reported 100% coverage and 0 skipped, and
    still emitted an ALIASING equivalence for the very concept whose second
    declaration it had not read.
    """
    blocked = {
        concept.concept_id: [c for c in concept.claims if c.core is None]
        for concept in concepts
    }
    for verdict in verdicts:
        untranslated = blocked.get(verdict.concept_id) or []
        if not untranslated or "untranslated_claims" in verdict.evidence:
            continue
        was_certifying = verdict.verdict_type in (VerdictType.CERTIFIED,
                                                  VerdictType.ALIASING)
        # ALSO annotate a verdict another qualifier already downgraded.
        # Skipping it meant a concept blocked by BOTH an unreadable artifact
        # and an untranslated declaration recorded only the first reason —
        # fixing the named blocker would reveal a second one the scan had
        # already seen and not recorded.
        already_downgraded = (
            verdict.verdict_type is VerdictType.UNRESOLVED
            and ("unread_estate" in verdict.evidence
                 or "ambiguous_relations" in verdict.evidence)
        )
        if not (was_certifying or already_downgraded):
            continue
        names = ", ".join(f"{c.name} ({c.provenance.system})" for c in untranslated)
        note = (f"{len(untranslated)} declaration(s) of this concept could not "
                f"be translated and were never compared ({names}).")
        if was_certifying:
            verdict.verdict_type = VerdictType.UNRESOLVED
            verdict.severity = max(verdict.severity, 45)
            verdict.summary = f"Cannot certify: {note} {verdict.summary}"
        else:
            verdict.summary = f"{verdict.summary} Additionally: {note}"
        verdict.evidence = {
            **verdict.evidence,
            "untranslated_claims": [
                c.error or "no machine-readable expression was declared"
                for c in untranslated
            ],
        }


def _qualify_for_ambiguous_relations(verdicts: list[Verdict],
                                     concepts: list[Concept],
                                     contested: dict[str, list[str]]) -> None:
    """No certification over a relation the estate GENUINELY defines twice.

    Two artifacts producing the same relation with DIFFERENT content is a
    conflict the scan cannot resolve: `registry.register_model` is
    last-write-wins, so the certified core — and the physical lineage signed
    into the receipt — is decided by registration order.

    Two refinements over the first version, which downgraded every
    certification estate-wide for any duplicate:

    * IDENTICAL duplicates (a vendored copy of the same file) are excluded by
      the caller before this runs — "what they mean is decided by scan order"
      is false when both mean the same thing at proof grade.
    * The downgrade is SCOPED to concepts whose members actually touch a
      contested relation. Unlike an unreadable artifact — which may define
      ANY concept — a duplicated relation's blast radius is knowable: both
      duplicates were read and canonicalized, so a concept whose lineage
      never meets the collision keeps its certificate. One vendored widget
      file must not un-certify an estate's revenue.
    """
    if not contested:
        return

    def touches(concept: Concept) -> list[str]:
        hit = set()
        for fp in concept.fingerprints:
            for relation in contested:
                if (same_rel(fp.relation, relation)
                        or relation in fp.core.join_nodes
                        or any(same_rel(node, relation)
                               for node in fp.core.join_nodes
                               if not node.startswith("cte:"))
                        or any(col.rsplit(".", 1)[0] == relation
                               or same_rel(col.rsplit(".", 1)[0], relation)
                               for col in fp.core.source_columns
                               if "." in col)):
                    hit.add(relation)
        for claim in concept.claims:
            for relation in contested:
                # Exact-string here shared the case/arity blindness the
                # re-aggregation lookup had; the YAML's spelling of a model
                # need not match the artifact's.
                if claim.model and same_rel(claim.model, relation):
                    hit.add(relation)
        return sorted(hit)

    by_id = {c.concept_id: c for c in concepts}
    for verdict in verdicts:
        was_certifying = verdict.verdict_type in (VerdictType.CERTIFIED,
                                                  VerdictType.ALIASING)
        # Compose with the unreadable downgrade rather than skipping — the
        # same one-pair-over composition hole round five fixed for
        # untranslated claims: with one unread artifact anywhere, the
        # contested-relation reason was recorded on NO verdict in the scan,
        # so fixing the compile error revealed a "new" blocker this scan had
        # already observed.
        already_downgraded = (
            verdict.verdict_type is VerdictType.UNRESOLVED
            and "unread_estate" in verdict.evidence
        )
        if not (was_certifying or already_downgraded):
            continue
        if "ambiguous_relations" in verdict.evidence:
            continue
        concept = by_id.get(verdict.concept_id)
        hit = touches(concept) if concept else []
        if not hit:
            continue
        names = ", ".join(hit[:3]) + (f" (+{len(hit) - 3} more)" if len(hit) > 3 else "")
        note = (f"this concept reads relation(s) defined by more than one artifact "
                f"with differing content ({names}), so what they mean is decided "
                f"by scan order rather than by the estate.")
        if was_certifying:
            verdict.verdict_type = VerdictType.UNRESOLVED
            verdict.severity = max(verdict.severity, 45)
            verdict.summary = f"Cannot certify: {note} {verdict.summary}"
        else:
            verdict.summary = f"{verdict.summary} Additionally: {note}"
        verdict.evidence = {**verdict.evidence,
                            "ambiguous_relations": {k: contested[k] for k in hit}}


def _qualify_for_toolchain(verdicts: list[Verdict], toolchain: dict) -> None:
    """No certification from a parser version nobody qualified. The engine's
    equality is sqlglot's canonical form; a different release is a different
    equality, and the one that reopened tested false certifications ran
    every scan as if nothing had changed."""
    if toolchain.get("qualified"):
        return
    for verdict in verdicts:
        if verdict.verdict_type in (VerdictType.CERTIFIED, VerdictType.ALIASING):
            verdict.verdict_type = VerdictType.UNRESOLVED
            verdict.severity = max(verdict.severity, 45)
            verdict.summary = (
                f"Cannot certify: sqlglot {toolchain.get('sqlglot')} is not a "
                f"qualified parser version for semantix (qualified: "
                f"{', '.join(sorted(QUALIFIED_SQLGLOT))}); equality under it is "
                f"untested. {verdict.summary}")
            verdict.evidence = {**verdict.evidence, "toolchain": toolchain}


def _qualify_for_unreadable(verdicts: list[Verdict], unread: list[str]) -> None:
    """No certification over an estate that was not fully read.

    An artifact the scan could not read — no compiled SQL, or SQL that would
    not parse — may define a competing version of ANY concept; not reading it
    is precisely why we cannot say. Earlier versions of this guard consulted
    prior scans to decide which concepts were "affected", which meant it did
    nothing at all on a first scan of a fresh store: the documented CI flow,
    and the exact case it was written for.

    So the rule is unconditional and deliberately unclever. Missing a
    certification is an acceptable cost; issuing a false one is not. The
    qualification names what blocked it, and `skipped` artifacts are usually
    one `dbt compile` away from resolving.
    """
    if not unread:
        return
    shown = ", ".join(sorted(unread)[:3])
    more = f" (+{len(unread) - 3} more)" if len(unread) > 3 else ""
    for verdict in verdicts:
        if verdict.verdict_type in (VerdictType.CERTIFIED, VerdictType.ALIASING):
            lead = (f"Cannot certify: {len(unread)} artifact(s) in this estate could "
                    f"not be read ({shown}{more}), and any of them may define this "
                    f"concept differently.")
        elif verdict.verdict_type is VerdictType.ORPHAN_CLAIM:
            # "Declared but never computed" is an ASSERTION OF ABSENCE, and an
            # unread artifact is exactly what could falsify it.
            lead = (f"Cannot confirm this is uncomputed: {len(unread)} artifact(s) "
                    f"could not be read ({shown}{more}), and any of them may compute it.")
        else:
            continue
        verdict.verdict_type = VerdictType.UNRESOLVED
        verdict.severity = max(verdict.severity, 45)
        verdict.summary = f"{lead} {verdict.summary}"
        verdict.evidence = {**verdict.evidence, "unread_estate": sorted(unread)}


def build_claim(spec: DeclaredSpec, registry: SchemaRegistry, dialect: str) -> Claim:
    """Give a declared definition the same canonical treatment as computed SQL
    (§4) by synthesizing the query it claims to be."""
    provenance = Provenance(system=spec.system, object_id=f"{spec.system}.{spec.name}", path=spec.path)
    claim = Claim(
        # Identity covers EVERYTHING the declaration says (model, filters,
        # group_by, untranslatable reasons) plus where it was said: keyed on
        # system|name|expr, two same-named declarations over different models
        # — or two refusals with expr=None — collapsed to one store row, and
        # every downstream reader (propose, publish, monitor) presented the
        # wrong declaration.
        claim_id=Claim.make_id(spec.name, provenance,
                               spec_content_text(spec) + "|" + (spec.path or "")),
        name=spec.name,
        provenance=provenance,
        description=spec.description,
        owner=spec.owner,
        model=spec.model,
        expr_raw=spec.expr,
        filters_raw=tuple(spec.filters),
        group_by_raw=tuple(spec.group_by),
    )
    # A claim the adapter could only PARTLY translate is not a weaker claim,
    # it is a different one. Reconciling it would certify or contradict a
    # definition nobody wrote, so it stays inexpressible and says why.
    # Checked BEFORE the expr/model check: most adapter refusals ship
    # expr=None, and the generic "no expr/model declared" was masking the
    # actual reason the adapter took care to write down.
    if spec.untranslatable:
        claim.error = (
            "could not translate part of the declaration, so it cannot be "
            "verified without inventing it: " + "; ".join(spec.untranslatable)
        )
        return claim

    if not spec.expr or not spec.model:
        claim.error = "no expr/model declared — not machine-expressible"
        return claim

    # A declaration that RE-AGGREGATES a model's aggregate output —
    # `sum(revenue)` over a mart that already sums — is a cross-LEVEL claim
    # the static comparison cannot make honestly: the mart's fingerprint is
    # over its sources, the claim is over the mart, and comparing them
    # produced a sev-96 CONTRADICTION blaming the mart's own internals for
    # faithfully restating itself. This is the standard metric-over-mart
    # pattern, so on a pilot's first scan the flagship report was mostly
    # top-severity noise. Refuse with the real reason; through-model
    # resolution is the Phase 1 fix.
    if spec.model and spec.expr:
        agg_cols = registry.aggregate_outputs(spec.model)
        if agg_cols:
            referenced = _columns_in_expr(spec.expr) & {c.lower() for c in agg_cols}
            if referenced and _is_aggregate_expr(spec.expr):
                claim.error = (
                    f"declares an aggregate over {', '.join(sorted(referenced))}, "
                    f"which {spec.model} already aggregates — a re-aggregation "
                    f"the static core cannot compare against the model's own "
                    f"definition; will not blame the model's internals "
                    f"(through-model resolution is Phase 1)"
                )
                return claim

    sql = _synthesize_claim_sql(spec)
    canon = canonicalize_sql(sql, registry.sqlglot_schema(dialect), dialect)
    claim.canon_level = canon.level.value
    if not canon.usable or canon.level == CanonLevel.PARSED:
        claim.error = canon.error or "canonicalization below proof grade"
        return claim

    fps = extract_fingerprints(
        canon, spec.model, provenance, dialect, registry
    )
    if len(fps) != 1:
        claim.error = f"expected exactly one measure in declared expr, found {len(fps)}"
        return claim

    claim.expressible = True
    claim.core = fps[0].core
    claim.canonical_sql = fps[0].canonical_sql
    # The COMPLETENESS GATE applies to declarations too. `claim.canon_level`
    # was set from canonicalize_sql, which only ever returns optimized /
    # qualified / parsed / failed — the INCOMPLETE downgrade happens inside
    # extract_fingerprints and was never copied back. So a declared definition
    # using a construct the core cannot model was labelled proof-grade, and
    # reconcile's `proof_grade` check — which reads exactly this field — fed it
    # straight into the CERTIFIED gate. Two declarations over different arrays
    # were provably indistinguishable at proof grade.
    claim.canon_level = fps[0].canon_level
    claim.gaps = fps[0].gaps
    if fps[0].gaps:
        claim.error = ("declaration uses constructs the semantic core cannot "
                       "represent: " + "; ".join(fps[0].gaps))
    return claim


def _columns_in_expr(expr: str) -> set[str]:
    """Lowercased column names a declared expression references."""
    import sqlglot
    from sqlglot import exp as _exp

    try:
        node = sqlglot.parse_one(expr)
    except Exception:  # noqa: BLE001 — arbitrary declared text
        return set()
    return {c.name.lower() for c in node.find_all(_exp.Column)}


def _is_aggregate_expr(expr: str) -> bool:
    import sqlglot
    from sqlglot import exp as _exp

    try:
        node = sqlglot.parse_one(expr)
    except Exception:  # noqa: BLE001
        return False
    return node.find(_exp.AggFunc) is not None


def _synthesize_claim_sql(spec: DeclaredSpec) -> str:
    parts = []
    for gb in spec.group_by:
        parts.append(gb)
    parts.append(f"{spec.expr} AS {spec.name}")
    sql = f"SELECT {', '.join(parts)} FROM {spec.model}"
    if spec.filters:
        sql += " WHERE " + " AND ".join(f"({f})" for f in spec.filters)
    if spec.group_by:
        sql += " GROUP BY " + ", ".join(spec.group_by)
    return sql
