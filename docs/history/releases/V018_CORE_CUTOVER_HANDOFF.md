# Eason One v0.18.0 — Core Cutover Handoff

## Governing spine

For newly approved work, the only governing execution path is:

Founder approval → Work → Employee → Provider/Codex tool → Host/structured verification → Artifact → CEO closure.

`Operation` remains a proposal/audit compatibility envelope. `Task` remains a one-way execution adapter. Neither may decide whether v0.18 Work is schedulable, waiting, retryable, complete, or currently active.

## What changed

- Added explicit `WORK_CORE_V018` runtime semantics.
- v0.18 gates/retry checkpoints live on `Work.runtime_control_json`; `WaitCondition` is historical/migration-only for v0.18.
- Company Runtime schedules only v0.18 Work.
- Legacy `WORK_VNEXT` Operations are dormant. At upgrade, at most the newest safe approved v0.17 candidate is migrated once. Ambiguous/running external effects block migration rather than replay.
- OperationKernel boot/recovery and OperationRuntime skip current v0.18 and retired v0.17 Work-first rows.
- Founder pause/resume/cancel and Founder budget decisions for v0.18 use Work/Project authority.
- Project budget is the Founder hard execution envelope. Operation/Work sub-budgets are planning metadata for v0.18 and cannot fabricate a second Founder gate.
- Current-working Employee, global runtime banner, Project state, and Employee activity ignore historical RUNNING AgentRuns.
- One runtime-focus polling source remains (`global-runtime.js`); Project pages subscribe to its event.
- Founder work phases are `Inspect → Prepare → Implement → Verify → Deliver`.
- Active v0.18 Projects show auditable Work closed counts instead of synthetic workflow percentages.
- Cost surfaces now distinguish local usage-price calculations from provider billing. Codex remains plan/subscription usage and is not presented as API spend.

## Migration safety

The installer backs up both application-owned code and `instance/eason_one.db` before applying the schema/cutover. The migration starts the Flask app with company/operation runtimes disabled. It never uses a provider.

The one-time legacy candidate migration refuses to replay a Work if it finds a RUNNING execution or `FAILED_AMBIGUOUS` external effect. Once any approved v0.18 Operation exists, restarts cannot migrate progressively older historical v0.17 Projects.

## Engineering diagnostics

`scripts/audit_v018_core.py` is dependency-free and checks structural invariants. `scripts/verify-v018.ps1` additionally compiles source and opens the preserved database with runtimes disabled. These are diagnostics only, not product acceptance.

## Live acceptance

Acceptance is based on a real Founder-approved Project in the actual Eason One environment, not on test counts. See the delivery message for the exact scenario and PASS/FAIL evidence to return.
