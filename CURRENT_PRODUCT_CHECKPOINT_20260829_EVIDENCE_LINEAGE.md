# Eason One Current Product Checkpoint — Multi-Employee Evidence Lineage

Date: 2026-08-29
Status: WORKING CHECKPOINT — DO NOT INSTALL TO USER LIVE REPO YET

## Product movement

This checkpoint turns multi-Employee handoff from text sharing into durable company provenance.

### 1. Governed dependency truth
- `WorkDependency` is the source of truth for current Project handoff context.
- The legacy orchestration-memory Task graph is not allowed to become a second handoff truth.

### 2. Transitive accepted company inputs
- Handoff traverses accepted upstream dependency ancestry within a bounded context budget.
- Research -> Product -> Engineer therefore preserves Research evidence/source lineage through the Product layer.
- Source and verification lineage is prioritized before long prose excerpts so context truncation cannot silently erase provenance.

### 3. Exact input Artifact lineage on execution
Each producing execution can now retain structured handoff lineage including:
- upstream Work ID
- Task ID
- dependency depth
- accepted ArtifactVersion ID
- Artifact content hash
- producing execution ID
- authoritative VerificationRecord IDs
- provider-observed source URLs/titles where applicable

Generic Employee execution receives this through its governed context composition. Engineer/Codex execution also persists the Company handoff composition and exact input artifact lineage.

### 4. Engineer/Codex company handoff
- The accountable Engineer's Codex Job Spec includes accepted company inputs, decisions and evidence without expanding authority.
- If an Engineer Work has upstream dependencies and governed handoff context cannot be built, Codex does not launch without those inputs.

### 5. Project outcome evidence continuity
- Accepted output snapshots include both provider source lineage and input artifact lineage.
- Project semantic review packet receives provider-observed source lineage directly from the producing AgentRun.
- Project semantic-review input hashing is bound to provider source lineage, so corrected/reconciled Research lineage invalidates stale review instead of reusing an old judgment.

### 6. Topology semantics
- The multi-Employee topology planner receives each Work's authoritative required capability.
- Runtime instructions explicitly preserve a Research -> downstream dependency when current external facts are the required input to later specialist work.

## Local-repo warning

This is a construction checkpoint only. The user's local Eason One repository has NOT been updated by this package. It remains on the last version the user actually installed.
