# Consolidated architecture/reliability changelog

Release identity: `architecture-reliability-20260902`

## Correctness changes

- Codex now executes only in a disposable source mirror. The mirror excludes `.git`, `.env`, `instance/`, databases, runtime state and environments; the live dirty tree is never the Codex working directory.
- The complete isolated delta is validated before copyback. Any extra path or concurrent live-path change makes copyback all-or-nothing and applies nothing; only exact frozen approved paths can be copied back.
- Codex write authority is now `CODEX_WRITE_SCOPE_V1`, an explicit structured, hash-bound Work record originating in the approved Operation plan. Repository paths mentioned in objectives, criteria, constraints or prohibitions grant no authority.
- Added the one explicit release migration for Project #21 Work #64, authorizing only `trial_company_output/first_company_trial.html` before any Codex execution.
- Replaced the fixed 1,100-token Work review output cap with a deterministic criterion-count envelope (1,800–3,200, bounded by ModelConfig).
- Added a one-time, evidence-proven recovery for historical review truncations. It preserves paid Runs, source lineage and exact ArtifactVersion, and resumes at `VERIFYING` without replaying implementation/Research.
- Tightened Codex authority from a descriptive prompt to exact, contract-derived repository paths. Actual host delta is compared with that set; out-of-scope changes are restored and the run fails closed.
- Added safe pre-execution tightening for legacy broad Codex boundaries only when no Codex run exists.
- Made unchanged `AUTHORITY_EXHAUSTED` Project reconciliation quiescent so an older blocked Project cannot starve runnable sibling Projects.
- Preserved queued Founder authority projection: same-type newer exact proposals become actionable while different authority types remain FIFO; legacy unkeyed waits resolve without clearing keyed gates.
- Ordered Result Ready guards so unresolved Founder authority and active delivery Work fail before contract evaluation, while current contract evidence is still recomputed at commit.
- Added deterministic compatibility inference of one canonical capability for pre-capability Engineer/Researcher/Critic/Product Strategist plans.
- Made Windows host validation prefer the repository's Python 3.13 runtime before a stale `.venv` launcher.
- Accepted the current v0.20 new-Project proposal envelope (`project` plus `project_id`) without weakening strict field validation, and persisted that Founder-visible Project specification into the approved Operation memory.
- Guaranteed that the reconciled Operation envelope can contain its own bounded Meeting allocation.
- Reconciled approved Persistent Employee assignments during Company-kernel adoption so capability truth exists before eligibility selection.
- Allowed recovery code to represent a historical terminal-Work wait without illegally reopening the terminal Work state.
- Accepted the current v0.20 new-Project proposal envelope (`project` plus `project_id`) without weakening strict field validation, and persisted that Founder-visible Project specification into the approved Operation memory.
- Guaranteed that the reconciled Operation envelope can contain its own bounded Meeting allocation.
- Reconciled approved Persistent Employee assignments during Company-kernel adoption so capability truth exists before eligibility selection.
- Allowed recovery code to represent a historical terminal-Work wait without illegally reopening the terminal Work state.

## Second-review closure

- R2 corrected the release-test ports after the first installer safely rolled back: obsolete historical Project-without-Contract fixtures, legacy Meeting concurrency entrypoints, old budget-exception expectations, and unconfigured paid-provider assumptions are no longer treated as current contracts. The same invariants are now exercised directly against current v0.20 Project Contract / Work / durable execution semantics.

- Separated the live-source snapshot from the isolated Codex post-run observer. The latter records every created/changed file-like path, including protected names and symlinks, instead of inheriting live-source exclusions.
- Moved Codex transport output/schema files outside the disposable repository so transport plumbing cannot be mistaken for approved Work output.
- Added all-or-nothing adversarial coverage for `.git/HEAD`, `instance/eason_one.db`, `.env`, existing source files, and arbitrary extras: approved HTML plus any forbidden extra yields zero live copyback.
- Added `TEST_CONTRACT_INVENTORY.md`, exact retired→current pytest node mappings, and a release-gated current-regression port module for still-valid invariants from preserved historical tests.
- The release-contract gate now verifies target node existence and required invariant coverage rather than asserting a hand-picked module set equals itself.

## Acceptance/release changes

- Added `scripts/audit_architecture_release.py`: live Project #21 DB-copy twin, fake model/Codex transports, real production kernel/database/state machines, exact isolated Codex file effect, restart batches, budget/result/lineage assertions.
- Added focused architecture regression tests.
- Added consolidated installer and rollback scripts. The installer compiles and runs structural, governance, provider, review/evidence and DB-copy twin gates; it never performs a real provider call.
- Updated the engineering snapshot identity.
- Added an exhaustive semantic test-contract registry. Every preserved module is classified as `CURRENT_REGRESSION`, `RETIRED_CONTRACT`, or `MIGRATION_ONLY`; pytest no longer uses filename-pattern selection, and all v0.20 runtime correctness modules are in the current release gate.

No UI redesign, product scope, real provider call, budget expansion, provider architecture replacement or workflow-framework migration is included.
