# Semantic Verification: A Ground-Truth Layer for Enterprise Ontologies

**Concept one-pager — system-agnostic**

> **Implementation status (2026-10-25):** positive verdicts (CERTIFIED, ALIASING, conforming uses) are **suspended** in the implementation; equalities are reported as experimental comparisons and differences as suspected, and no receipt authorizes reliance. Statements below about certification describe the design target, not current behaviour — see [proof-policy.md](proof-policy.md).

## The problem

Enterprises are racing to give AI agents "business context" through ontologies, semantic layers, and knowledge graphs. The newest generation of these context layers is *inferred*: automatically extracted from tables, queries, dashboards, and documents, then ranked by authority signals such as source popularity, author seniority, and asset usage. This is consensus epistemology — the same approach that powers web-scale knowledge graphs — and it carries the same failure mode. Ranking the most *trusted* definition of a metric is not the same as verifying that the definition is *correct*. Meanwhile, the only artifact that cannot lie about what a metric actually computes is the transformation code itself. The compiled SQL is the de facto ontology of the business. In most stacks, nothing systematically reconciles the two: what the organization *declares* its metrics mean, and what its pipelines *actually compute*. Agents inherit that gap and answer with false confidence.

## The proposal

A **semantic verification engine**: a system that continuously extracts the de facto semantics of a data estate from compiled transformation code, reconciles them against the declared or inferred ontology, and reports, with evidence, where each definition it can read agrees or disagrees. It certifies sameness only within a stated scope and says plainly which definitions it could not verify.

## How it works

**Extract.** Parse compiled SQL across the estate into canonical semantic fingerprints: source tables, join paths, filters, aggregation expressions, and grain. Each fingerprint is the operational definition of a metric as the code actually computes it.

**Cluster.** Group fingerprints into business concepts using name resolution, lineage overlap, and semantic similarity, so that every variant computation of "net revenue" — governed or shadow — lands under one concept.

**Reconcile.** Diff computed definitions against declared ones. Every concept receives a verdict: *contradiction* (same name, different math), *aliasing* (same math, different names), *orphan claim* (declared but never computed), or *shadow metric* (computed but never declared). Where static equivalence is undecidable, execute competing definitions over a sample window and compare the numbers — divergence measured in dollars is unarguable.

**Attest.** Expose certification status as a runtime API. Before an agent answers with a number, it checks whether the underlying definition is certified, contested, or contradicted — and every answer ships with a *semantic receipt*: definition version, lineage, certification status, and known divergence from alternatives.

## The two capabilities that compound

**Metric change attribution.** Because the engine holds both fingerprint history and data, any KPI movement can be decomposed into definition change, pipeline change, and genuine business change — collapsing the most common fire drill in every data organization from days to seconds.

**Bitemporal semantics.** Every fingerprint is versioned in time, so the system answers "what did this metric *mean* on this date." That enables semantic restatement detection: flagging when historical comparisons silently span a definition change. Data platforms have time travel; meaning does not. That history has to be recorded as definitions change; it is hard to reconstruct afterwards from today's code.

## Why now

Every major data platform shipped an inferred context layer this year, and enterprises are deploying agents against them. The verification gap becomes visible the first time an executive gets a confidently wrong number. Calculation validity lives in transformation code, which popularity-ranked graphs do not read. Verification that sits outside the systems it audits avoids grading its own work, and it needs to be cross-platform, because real estates span multiple warehouses, semantic layers, and BI tools.

## Applications

Agent-rollout gating (certify the ontology before agents answer material questions); metric certification as governance and audit evidence; semantic mapping in migrations and M&A; golden definitions for text-to-SQL and agent evaluation; and finance-close reconciliation between reported KPIs and the pipelines that produce them. One engine, several buyers.

## The endgame

The near-term role is narrower than the vision: a discrepancy investigator that finds where declared and computed definitions disagree, explains the difference, and verifies sameness only within a scope it states and tests. Over time, the exhaust of continuous verification could become a reference set of definitions earned through reconciliation rather than approximated through popularity.
