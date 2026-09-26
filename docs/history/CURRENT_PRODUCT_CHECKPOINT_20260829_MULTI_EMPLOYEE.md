# Eason One Product Checkpoint — 2026-08-29 — Multi-Employee Company Runtime

## Product position

This checkpoint is intentionally not described by test counts or invariant counts.

The governing v0.20 runtime has moved from a de-facto single-Work execution lane to a real bounded multi-Employee Project runtime:

Founder Project Contract
→ CEO-approved specialist Work set
→ Work topology / dependencies
→ independent Persistent Employees may execute concurrently
→ accepted upstream Work/Artifact/verification lineage becomes bounded downstream context
→ exact Work-scoped Founder exceptions pause only affected Work
→ Project-scoped Founder authority still freezes Project-wide execution
→ Founder/CEO read models project Work truth rather than stale Task truth.

Codex is only the Engineer's execution tool; the Company Kernel no longer treats Engineer/Codex as the whole Project runtime.

## Mainline changes in this checkpoint

- Retired duplicate PROJECT_PLAN Project creation authority and legacy Project/Task HTTP writers.
- Governed provider execution requires exact Project → Operation → Work authority lineage.
- Legacy Task writers are forbidden for Work-backed/governed Tasks.
- Company Kernel no longer forces every multi-Employee Operation into serial WorkDependency ordering.
- Existing bounded multi-agent orchestrator is reused only to choose dependency topology/parallelism among already-approved Work/assignees.
- Company Kernel can select and dispatch a bounded concurrent Work wave across distinct Persistent Employees.
- Same Employee is not scheduled twice in one wave; Engineer/Codex remains a single repository writer per wave.
- Multi-agent projection is Work-first for governed Projects.
- Project Founder surface now shows Company Team topology, employee ownership, dependencies, readiness and live state.
- CEO operating context and Founder/company read models use Work/WorkAssignment truth for governed Projects.
- Downstream Employee context uses ACCEPTED predecessor Work and accepted ArtifactVersion/Verification lineage, not merely stale Task summaries.
- Execution-scoped Founder gates block only the exact Work; unrelated authorized Work may continue.
- Project-scoped Founder gates (budget/scope/constraints/deadline/cancel) remain Project-wide blockers.
- FOUNDER_DECISION Work wait projections can bind exact escalation identities so resolving one question does not erase another.
- RECONCILIATION waits receive distinct durable incident identities rather than collapsing every integrity issue into one generic gate.
- Founder-negative budget compatibility path delegates lifecycle effects to canonical Governance owner.

## Important acceptance still required on the user's Windows repo

This environment does not have Flask / Flask-SQLAlchemy installed, so ORM/runtime acceptance cannot be honestly executed here.

The next live acceptance should prove product behavior, not merely unit-test success:

1. Create/approve one real Project whose CEO plan assigns at least two different Persistent Employees.
2. Confirm v0.20 Company Kernel creates/persists topology instead of auto-serializing every Work.
3. Confirm independent Works can be live concurrently.
4. Confirm a downstream Work receives only accepted declared predecessor Artifact/evidence lineage.
5. Open an execution-scoped Founder gate on one branch and confirm a sibling branch remains runnable.
6. Confirm Project page visibly shows multiple Employees, dependencies, and concurrent Work state.
7. Confirm Project-wide budget/scope authority still stops all affected execution.

## Next product work

Continue Company / Employee Runtime rather than returning to Governance micro-hardening:

- capability-aware team formation / assignment quality;
- collaboration and Meeting timing during real work, not only final coordination;
- richer non-engineering execution/tool capability where already-available providers/tools can be reused;
- persistent Employee memory/learning tied to accepted Work history;
- end-to-end real Project trial with multiple Employees through Outcome.

Governance should only be reopened for a root authority/truth defect that makes downstream Company Runtime unsafe. Nonblocking hardening belongs in backlog.
