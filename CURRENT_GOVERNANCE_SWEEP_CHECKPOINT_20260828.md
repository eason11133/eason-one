# Eason One Governance / Project Runtime Full Sweep — Checkpoint

**Checkpoint date:** 2026-08-28 12:56 +08:00  
**User live baseline:** `governance-r7-20260828`  
**Working tree:** `/mnt/data/governance-sweep` in the originating ChatGPT work session  
**Release status:** **NOT READY / DO NOT INSTALL THIS CHECKPOINT AS A RELEASE**

## Why this checkpoint exists

The development method is no longer "wait for one Product Gate screenshot, patch one symptom, release rN+1". The active large target is the entire **Governance / Project Runtime floor**. During this sweep, every discoverable local defect is fixed immediately and shared root causes are redesigned together. A new installable package is published only after the whole floor is swept, structurally audited, regression-covered and then run through Windows diagnostics / real Founder-facing E2E.

This file and the accompanying source ZIP are a continuity artifact so a new conversation can continue from exact source truth if the current chat hits its limit.

## Current package progress

- Live user package: **r7**.
- Next consolidated install package: **not packaged yet**.
- Current working source intentionally still reports the r7 engineering snapshot string; snapshot ID will change only when the consolidated release is sealed.
- Python `compileall`: **PASS** at this checkpoint.
- Core structural audit: **PASS**.
- Governance structural audit: **99 / 99 invariants PASS**.
- Full Flask/SQLAlchemy ORM diagnostics: **not runnable in the current Linux sandbox because Flask is absent**; must run on the user's Windows `.venv` before release. This must not be represented as passed.

## Product Gate corpus retained

**Project #20 — Founder Ping Endpoint** remains intact as durable failure/recovery evidence. Do not delete, hand-edit, reset, or recreate it merely to obtain a clean demo.

It exposed, in sequence:
- Founder read-surface Contract fault containment failure.
- Delivery installer/source identity failures.
- Founder intent routing blind spot.
- Windows host proof vs semantic reviewer evidence split-brain.
- Repeated implementation caused by evidence-handoff failure.
- SYSTEM_RECOVERY dead-end / recovery ownership.
- Project budget exhaustion / amendment semantics.
- Founder REJECT not initially acting as durable negative authority.
- Duplicate budget re-ask after REJECT.
- Restart durability.
- Project closure convergence / evidence-to-result sequencing.

## Full-sweep changes already implemented in current working tree

### Immutable authority / acceptance
1. Work/Project acceptance schemas no longer truncate authority to 2 criteria x 120 chars. Bounded contract supports up to 8 criteria x 280 chars.
2. Shared text-normalization boundary strips provider U+FFFC/U+FFFD/zero-width/control garbage before immutable Project/Work Contract freeze/hash without changing semantic text.
3. Work acceptance freezes a `project_execution_terms_hash` covering Founder objective, success criteria, constraints and deadline. A later material Project amendment invalidates stale Work execution.
4. Work made stale by a real Project terms amendment is now **CANCELLED for Project replan**, instead of sitting forever in generic RECONCILIATION or executing against obsolete authority.

### Deterministic host / evidence truth
5. HTTP verifier now proves exact expected HTTP status, current `pyproject.toml` application version, response fields and required health preservation from frozen criteria.
6. Response-only HTTP JSON criteria bind to Host proof only when exactly one frozen primary endpoint is unambiguous; no path guessing.
7. Exact-replacement verification is derived only from frozen acceptance criteria, not mutable Task prose.
8. Independent semantic reviewer receives authoritative Host VerificationRecords bound to the **same Work, ArtifactVersion, acceptance Contract hash and Artifact content hash**.
9. Earlier Codex/WSL text saying verification could not run cannot override a later authoritative Windows Host PASS.
10. `UNPROVEN` semantic review is evidence state, not implementation failure. It preserves implementation + host proof and performs bounded evidence-review retry rather than rerunning Codex implementation.
11. Evidence-review exhaustion persists `EVIDENCE_REVIEW_EXHAUSTED` + `continuation_failure_mode=EVIDENCE_ONLY` so Project continuation cannot flatten it into generic implementation failure.
12. Evidence-only continuation is deterministically narrowed to **READ-ONLY EVIDENCE RECONCILIATION**; same implementation cannot be replayed merely to obtain proof.
13. Deterministic proof gaps no longer create ownerless `VERIFICATION` waits. A bounded `HOST_PROOF_RETRY` reruns local Host proof without a new implementation call, then moves to explicit reconciliation if proof still cannot be produced.

### Project outcome / closure
14. Project outcome criteria have stable Contract-bound IDs `P1..P8`; semantic reviewer addresses IDs rather than model-paraphrased criterion text.
15. Project semantic review is bound to current Contract hash + current accepted evidence input hash; stale review cannot close a changed Project.
16. A Project review cannot cite abandoned/nonaccepted Work as proof. `SATISFIED` must cite current durably accepted Work.
17. Result Ready refuses unresolved Founder authority or active delivery Work and cannot rewrite Mission lifecycle history.
18. Founder final Project acceptance remains a separate verified Project-level Decision.

### Founder authority
19. Founder authority is durable FIFO truth. A second authority need cannot silently supersede an unanswered first one just because UI wants one card.
20. Out-of-order Founder gate resolution is rejected.
21. Whole-Project Cancel may retire queued authority as moot without fabricating Founder answers.
22. Founder REJECT is durable **negative authority** bound to current Project Contract/budget authority. Company cannot reopen equivalent budget authority by slightly changing the requested amount.
23. Explicit Founder reconsideration remains possible; automated company nagging does not.
24. Rejected delivery budget now always creates a management-owned `AUTHORITY_EXHAUSTED` boundary even when the rejected gate originated on delivery Work; the exact paid Work is terminalized and cannot silently resume.

### Project lifecycle / recovery
25. Added one central `PROJECT_HARD_WAIT_TYPES` / `project_hard_blockers()` / `project_can_activate()` truth. Lifecycle writers may not set Project ACTIVE merely because their local gate disappeared.
26. Kernel, Governance resolver, manual Mission Stop/Resume, restart recovery, Project outcome and compatibility budget reconciliation use the central activation guard.
27. A hard blocker on **management or sibling Work freezes every runnable Work in the Project**, preventing side-channel spending while another Work says AUTHORITY_EXHAUSTED / SYSTEM_RECOVERY / RECONCILIATION / FOUNDER_PAUSE.
28. Restart adoption preserves durable BLOCKED/REVIEW state instead of resetting Project ACTIVE.
29. Startup adoption failure is persisted as visible Company reconciliation truth rather than silently skipped.
30. Platform repair may reopen terminal ABANDONED Work only through explicit `reopen_abandoned()` + `WORK_SYSTEM_REOPENED` audit event.
31. Current Founder read truth examines **all open Work gates**, not only the oldest gate per Work, so an older retry gate cannot hide a newer authority/integrity blocker.

### Wait ownership
32. Added `VNEXT_WAIT_OWNERS`: every vNext Work wait must have a named owner and exit policy.
33. Valid vNext wait families currently include DEPENDENCY, INTERNAL/RETRY_BACKOFF, EVIDENCE_REVIEW_RETRY, HOST_PROOF_RETRY, FOUNDER_DECISION, FOUNDER_PAUSE, AUTHORITY_EXHAUSTED, SYSTEM_RECOVERY and RECONCILIATION.
34. Unknown/legacy generic waits such as `BUDGET` or `VERIFICATION` now fail closed on governed Work instead of creating immortal WAITING state.

### Codex / engineering authority
35. Codex read/write decision for vNext uses durable Work title/purpose/acceptance, not mutable Task/Operation prose.
36. Added frozen `CODEX_EXECUTION_BOUNDARY_V1` on Work before execution: repository, allowed paths, forbidden paths, max changed files, read-only bit and Project execution-terms hash are hash-bound.
37. Codex retries/restarts consume that frozen boundary instead of rereading mutable `Operation.memory_json[engineering]`.
38. Governed repository resolution ignores mutable Operation engineering memory; Project Contract / configured trusted root owns vNext repo scope.
39. Legacy `operations._materialize_task_success()` now fail-closes with `VNEXT_LEGACY_TASK_MATERIALIZER_FORBIDDEN` for vNext, preventing an alternate Artifact acceptance / direct CODEX_RISK_APPROVAL writer.

### Existing earlier r7 guarantees retained
40. Governed Project budget reads use validated Contract authority ledger; historical fallback is compatibility-only.
41. Result Ready locks scope/deadline/constraints/budget/execution exception changes except explicit entire Project Cancel.
42. Durable exact-action Founder receipts bind Codex/external-effect exception to exact Work/action/Contract/authority hash and require fresh post-approval execution.
43. Delegated hiring may create persistent Employee capacity inside Project authority but grants `new_budget_authority_twd=0`.
44. Initial Project proposal authority is separate from generic pending Proposal inbox.
45. Installer delivery integrity retains source->target byte verification, Windows PowerShell 5.1 compatibility and fail-stop rollback.

## Current structural gate

Governance audit currently passes **99 invariants**. The count increased from the r7 sweep start (72/then 88) because the audit itself was expanded to make the new shared-root architecture enforceable rather than relying on developer memory.

## Remaining blocking work before next install package

1. Finish classifying remaining `RECONCILIATION` writers. Recoverable cases must get a bounded owner; genuinely ambiguous/unsafe cases must remain explicit platform-integrity stops, not vague generic waits.
2. Continue direct writer/reader scan for every Project Contract field, Operation budget writer and legacy compatibility writer; prove all vNext mutations are historical-only or canonical Governance/Contract-ledger writes.
3. Sweep external-effect/restart reconciliation so exact effect ambiguity cannot be replayed or accidentally converted into Founder authority.
4. Recheck Project outcome/continuation after evidence-only failure to ensure a read-only evidence Mission cannot accidentally be normalized back into engineering implementation by a downstream adapter.
5. Complete baseline-preservation regression coverage for existing successful Founder intake / Proposal / approve / execution behavior.
6. Add executable ORM tests for the newest hard-block, Codex-boundary, stale-Work, host-proof-retry and rejected-budget-management rules; tests are written progressively but cannot be run in this Linux environment without Flask.
7. Rerun full writer/reader structural scan after final changes.
8. Seal a new engineering snapshot ID only after sweep completion.
9. Build **one consolidated install ZIP + one-click PowerShell installer**.
10. On Windows: force package byte identity, compile, structural audits, focused ORM diagnostics, restart recovery diagnostics, then real Founder-facing E2E. Only then call the Governance floor sealed.

## Handoff instruction for a new conversation

Continue from this checkpoint/source snapshot. Do **not** return to r6/r7 one-bug hotfix cadence and do not restart Governance analysis from old handoff summaries. The active large target remains: **Governance / Project Runtime full defect sweep + shared-root repair + one consolidated release**.
