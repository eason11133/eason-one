# Eason One blocker inventory

Baseline: 2026-09-02 pre-consolidation working tree and live database, with real external calls prohibited.

## BLOCKING

| ID | Defect / risk | Release proof required | Status |
|---|---|---|---|
| B-01 | Project #21 Work #61 was terminal `ABANDONED` although producing Run #198 and its immutable Research artifact/source lineage existed. | DB-copy recovery reuses version #40, resumes review only, and reaches ACCEPTED without a new Research execution. | CLOSED |
| B-02 | Legacy Operation/Task/Meeting mutation routes coexist with the Company Kernel. | structural audits and tests prove Task is a compatibility adapter, terminal dispatch is rejected, and Project completion is available only through `project_outcome`. Legacy surfaces remain debt, not a second autonomous scheduler. | CLOSED |
| B-03 | `REVIEW` is overloaded as the persisted Result Ready phase label. | `result_ready_proof()` is required for kernel, Founder acceptance and projections; active Work/Founder authority is checked before evaluation. | CLOSED |
| B-04 | Paid side effects cross database/remote boundaries. | provider contract/failure tests prove durable pre-dispatch, rejected, ambiguous and settled paths; twin restarts do not duplicate Research or cost. | CLOSED |
| B-05 | Critical protocol truth is partly stored in JSON. | current Contract, Work acceptance, Codex boundary and recovery JSON are version/hash validated and fail closed; JSON alone cannot authorize completion. | CLOSED |
| B-06 | Codex could previously treat a descriptive allowed-path prompt as enforcement. | exact Contract paths are frozen/hash-bound; host-observed out-of-scope deltas are restored and fail `CODEX_SCOPE_VIOLATION`; #21 permits exactly one HTML path. | CLOSED |
| B-07 | Full real-state progression was unproven. | `scripts/audit_architecture_release.py` clones live DB, uses fake transports with production orchestration, restarts between batches, and reaches current Result Ready under cap. | CLOSED |
| B-08 | An unchanged authority-exhausted older Project was returned as productive work forever and starved later Projects. | management persists the evaluated evidence hash; unchanged blocked truth becomes quiescent and Project #21 advances in the same kernel. | CLOSED |
| B-09 | Pytest collected mutually exclusive historical product contracts as if every Slice were the current release, obscuring real v0.20 regressions with 334 failures. | Current pytest discovery is pinned to v0.20/release contracts; historical sources remain auditable. The actual v0.20 proposal, Meeting envelope, team eligibility and terminal-wait compatibility regressions are fixed and the release suite passes. | CLOSED |
| B-10 | Live Codex received the governed dirty repository directly and relied on after-the-fact restoration. | Codex receives only a disposable protected-state-free mirror; the post-execution observer sees every created/changed file-like path including `.git`, `instance`, `.env`, DB files and symlinks; any extra path rejects the whole attempt before copyback. | CLOSED |
| B-11 | Codex write paths were extracted from prose, so a prohibited path could become authority. | Only structured hash-bound `CODEX_WRITE_SCOPE_V1` frozen on Work is accepted; adversarial prohibition-path test passes. | CLOSED |
| B-12 | Release PASS depended on an over-narrow semantic module list that retired still-current runtime invariants. | `TEST_CONTRACT_INVENTORY.md` maps every retired module to exact release-gated superseding nodes; `test_v020_current_regressions.py` re-expresses durable restart, no-recall, dependency, dispatch idempotency, retry, cost, budget, immutable-run and Persistent Employee invariants against current v0.20 semantics rather than obsolete fixtures. | CLOSED |

## STRUCTURAL DEBT

| ID | Debt | Backlog direction |
|---|---|---|
| S-01 | `services/operations.py` and `routes.py` are god modules spanning generations of runtime. | isolate compatibility adapters and enforce one-way dependencies after release. |
| S-02 | Project/Work and Mission/Operation/Task vocabularies remain live in the same process. | formally deprecate legacy writers; retain read-only audit migration paths. |
| S-03 | Home-grown durable workflow, retries, leases and tracing carry high proof burden. | evaluate a mature durable-workflow engine without moving Founder/company semantics into it. |
| S-04 | SQLite and process-local execution constrain concurrency and lease safety. | document single-scheduler deployment; later evaluate transactional job/lease storage. |
| S-05 | Critical protocol snapshots live in JSON. | incrementally normalize identifiers/unique keys and retain versioned immutable snapshots. |
| S-06 | Historical CSS/templates/services increase hot-path discoverability cost. | remove only after route/import telemetry proves they are dead. |
| S-07 | Historical Slice 003–018 pytest modules are retained but no longer execute in the current release gate. | archive them by release/version in a later repository-history cleanup; never silently re-enable mutually exclusive contracts. |

## BETTER

| ID | Improvement | Why deferred |
|---|---|---|
| P-01 | Consolidate Founder observability for retry owner, evidence basis and recovery ETA. | useful projection work, but not required if current truth is accurate and actionable. |
| P-02 | Reduce naming aliases and magic status strings. | cleanup alone does not establish correctness. |
| P-03 | Broader framework/module rewrite. | prohibited for this reliability release and would increase regression risk. |

Statuses in this file must be updated only after executable evidence exists; a code review opinion is not closure.
