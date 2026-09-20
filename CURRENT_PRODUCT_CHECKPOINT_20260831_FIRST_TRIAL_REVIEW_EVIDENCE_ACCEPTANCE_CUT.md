# Eason One — First Trial Review & Evidence Acceptance Cut

## Why this cut exists
Live Project #21 proved the Research execution path is now real: Perplexity Sonar completed TASK_EXECUTION successfully and the resulting Research Artifact reached semantic review. The next blocker exposed an acceptance-ownership defect rather than a provider failure: historical runtime required the TASK_REVIEW call itself to emit a second provider-observed source list, even though the exact Research ArtifactVersion was already backed by provider source lineage from its producing Execution.

That defect also caused Research review to override the frozen reviewer Employee with a live-research provider. This violated review actor locality and the current cost/routing policy in which the Critic is the independent reviewer and Claude is reserved for Critic work.

## Root correction
- Research source provenance is owned by the producing Execution and exact ArtifactVersion.
- Semantic review consumes that persisted, hash-bound provider source lineage.
- Missing producing source lineage fails closed before any paid review call.
- The frozen reviewer Employee owns TASK_REVIEW; normal execution policy chooses the reviewer's substrate.
- Research review does not require fresh web search or a redundant second provider source trace.
- Historical `RESEARCH_REVIEW_SOURCE_EVIDENCE_MISSING` attempts caused solely by the obsolete rule are retained as paid audit history but marked `SYSTEM_RESEARCH_REVIEW_EVIDENCE_SCOPE_SUPERSEDED` so they do not consume Work business retry quota.
- WAITING or ABANDONED Work can be audibly restored to VERIFYING without replaying Research implementation.
- Project outcome evidence already carries accepted provider-source and upstream Artifact lineage; that contract is preserved.

## First Trial expected recovery
For current Project #21, installer DB-copy smoke should prove:
- successful Research producing Run exists and has provider-observed sources;
- exact Research ArtifactVersion is bound to that producing Run;
- obsolete review-source retry gate is resolved;
- historical failed review run(s) are marked platform-superseded;
- Research resumes at VERIFYING (not EXECUTING/READY, so Research is not replayed);
- frozen reviewer remains Critic;
- effective review route is Anthropic/Claude;
- 0 provider calls are made by installer/recovery smoke.

## Acceptance target after restart
Research Artifact -> Critic/Claude semantic review -> accepted Research evidence -> Product Strategy -> independent critical review -> Engineer/Codex -> Project outcome review -> RESULT READY.
