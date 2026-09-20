# Eason One Current Product Checkpoint — Company Handoff Chain

Date: 2026-08-29
Status: WORKING CHECKPOINT — DO NOT INSTALL TO USER LIVE REPO YET

## Product movement

This checkpoint closes a major multi-Employee handoff gap after live Research became real.

### Research execution substrate
- Formal Research can use OpenAI hosted Web Search or Perplexity/Sonar.
- Hosted-search calls are bounded and priced inside Work/Project budget authority.
- Research succeeds only with provider-observed source lineage.

### WorkDependency is now the handoff truth
- Governed Employee context reads durable `WorkDependency` edges rather than the orchestration-memory Task graph.
- Execution eligibility and artifact handoff therefore use the same dependency truth.

### Transitive company lineage survives multi-step work
- Handoff context traverses accepted upstream Work dependencies (bounded) rather than only one direct predecessor.
- A `Research -> Product Strategy -> Engineer` chain therefore preserves the original Research Artifact/source lineage for Engineer instead of depending on Product to copy URLs into prose.
- Evidence lineage is placed before long prose excerpts so bounded context truncation does not silently erase sources.

### Engineer/Codex consumes Company handoff context
- The accountable Engineer's bounded Codex Job Spec now includes the Engineer's current Company context: accepted upstream Artifacts, verification lineage, provider-observed sources, Meeting decisions, persistent Employee experience and Founder feedback.
- This context is evidence/decision input only and explicitly does not expand Codex authority.
- If an Engineer Work has upstream dependencies and Company handoff context cannot be built, execution fails closed rather than launching Codex without the accepted company inputs.

### Project-level review retains structured Research sources
- Project outcome evidence packet now includes provider-observed sources from the exact AgentRun that produced each accepted ArtifactVersion.
- Final Critic/semantic review no longer depends on citations happening to remain inside a truncated Artifact excerpt.

### Topology planner receives staffing semantics
- Work topology planning now sees each Task's authoritative required capability from its Work staffing contract.
- Planner instructions explicitly preserve Research -> downstream specialist dependency when current external facts are the input to later strategy/design/engineering work.
- CEO instructions no longer falsely claim the Founder-facing Operation schema directly encodes dependencies; Runtime owns the dependency graph after approval.

## Local-repo warning

This is a construction checkpoint only. The user's actual local Eason One repository remains on the previously installed version until an explicit installation instruction is given and executed successfully.
