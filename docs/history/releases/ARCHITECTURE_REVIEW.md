# Eason One Architecture Review (pre-consolidation snapshot)

Date: 2026-09-02 (Asia/Taipei)  
Scope: the actual dirty working tree and `instance/eason_one.db`, before this review's corrective changes.  
Safety boundary: no real provider or Codex request is permitted by this review.

## 1. Current architecture diagram

```text
Founder
  |
  v
Headquarters / Flask routes ----------------------------------------------+
  |                                                                      |
  | Project proposal + explicit decisions                               | legacy UI/routes
  v                                                                      v
Canonical Governance -----> immutable Project Contract              Operation / Task / Meeting
  |                               |                                      compatibility surface
  | exact authority gates         v
  +-----------------------> Company Kernel (active vNext scheduler)
                                   |
                         dependency-ready Work wave
                                   |
                    +--------------+---------------+
                    |                              |
              Work execution                  Work review
                    |                              |
           Employee -> provider/Codex       frozen reviewer Employee
                    |                              |
                    +------ AgentRun / cost -------+
                                   |
                       immutable ArtifactVersion
                                   |
                    source lineage + host/semantic proof
                                   |
                      Project Outcome Review
                                   |
            current Contract-bound result proof only
                                   |
                              RESULT_READY
                                   |
                      explicit Founder acceptance
                                   v
                              COMPLETED

SQLite is the durable system of record. CompanyEvent, OperationEvent,
AgentRun, CostReservation and CostEvent form the recovery/audit trail.
```

## 2. Module and authority map

| Concern | Actual responsible modules | Authoritative durable data | Principal reads | Principal writes / side effects |
|---|---|---|---|---|
| Founder UI | `routes.py`, `services/headquarters.py`, `templates/hq_*` | projections only | Project, Work, Artifact, Decision, Escalation, cost/event rows | explicit Founder endpoints; must delegate to governance/project services |
| Founder authority | `services/governance.py`, `founder_decisions.py`, `proposal_authority.py` | committed `Decision`, canonical governance gates and contract hashes | kernel, execution, HQ | exact bounded authority decisions |
| Project contract | `services/project_contract.py`, `acceptance_contract.py` | Project contract/contract hash, criteria and Founder cap | kernel, budget, outcome | proposal/adoption/amendment only through authority checks |
| Company scheduler | `services/company_kernel.py` | Project + Work + gates/events | approved operations, dependency graph, work/review/outcome state | work claims, dispatch, recovery, continuation, outcome transition |
| Work state | `services/work_runtime.py` | `Work`, `WorkAssignment`, `WorkDependency`, work runtime JSON and CompanyEvent | kernel, HQ | guarded transitions and gates |
| Persistent employees / HR | `workforce.py`, `team_formation.py`, `employee_memory.py` | Employee, Position, capability/profile/history, HiringRequest | planning, routing, review independence | durable hire/materialization/assignment |
| Provider execution | `providers.py`, `provider_protocol.py`, `execution.py`, `external_effects.py` | AgentRun + ExternalEffectAttempt | work/operation execution | one external request, result metadata, usage and reconciliation |
| Codex boundary | `codex_connector.py`, `engineering_runtime.py`, `host_validation.py` | approved job spec/scope hashes, AgentRun metadata, repository proof | engineering work | bounded Codex invocation and host filesystem verification |
| Artifact/evidence | `artifacts.py`, `research.py`, `host_validation.py` | Artifact, immutable ArtifactVersion, VerificationRecord, ResearchRecord | downstream work, reviews, outcome | append version and lineage/proof; never overwrite accepted content |
| Review | `work_execution.py`, `reviews.py`, `project_outcome.py` | review AgentRun, VerificationRecord, exact reviewed version/hash | kernel and outcome | accept/reject/unproven proof against frozen version |
| Budget | `work_budget.py`, `costs.py`, `execution.py` | Founder Project hard cap, CostReservation, CostEvent, settled AgentRun usage | kernel/provider dispatch/HQ | reserve before side effect; settle/release/reconcile exactly once |
| Completion | `project_outcome.py` | current Project status plus current contract-bound Project Result proof | kernel/HQ | RESULT_READY, then explicit Founder completion |
| Persistence/recovery | `models.py`, `runtime_recovery.py`, kernel/runtime modules | SQLite rows and durable events | startup adoption and scheduler | transactional transitions, retry/reconciliation ownership |
| Legacy compatibility | `operations.py`, `task_execution.py`, `meetings.py`, `legacy_*` and old routes | Operation/Task/Meeting historical truth | compatibility screens and vNext adapters | still contains active writers; must not become alternate Project authority |

External effects are provider HTTP calls and Codex/repository execution. They must occur only after a durable claim and reservation, and must be reconciled from AgentRun/ExternalEffectAttempt rather than replayed after an ambiguous post-dispatch failure.

## 3. Data authority answers

- Project truth: `Project`, interpreted by the current Project Contract and its hash; Operation/Mission is not Project completion truth.
- Founder authority: committed canonical `Decision`/governance gate bound to the exact Project/action/contract context.
- Budget truth: Founder Project hard cap plus settled `CostEvent` and active `CostReservation`; Operation budgets are subordinate allocations.
- Work truth: `Work` with assignments, dependencies, runtime gate metadata and durable events. Legacy `Task` is not vNext delivery truth.
- Employee truth: persistent `Employee`, Position/capability/profile/history and durable WorkAssignment. Provider/model is execution substrate only.
- Artifact truth: immutable `ArtifactVersion` selected by explicit acceptance/current lineage, not mutable Work output text.
- Evidence truth: provider observations/sources on the producing run/version, host VerificationRecord, and semantic review records remain distinct.
- Review truth: the latest applicable verification bound to the exact current ArtifactVersion/hash and reviewer identity.
- Provider execution truth: AgentRun plus ExternalEffectAttempt, provider response/request IDs, usage and exactly-once cost settlement.
- Project completion truth: current contract criteria + accepted current delivery artifacts/proof + independent Project Outcome Review + no unresolved authority/active delivery work + budget integrity.

Potential shadow truth exists in Operation memory JSON, Task status/results, legacy founder report JSON, UI labels and historical reviews. These are valid audit/compatibility inputs only when reconciled to the canonical records above; none may independently authorize spending or completion.

## 4. Runtime execution map

```text
approved Operation adoption
  -> validate immutable Project authority/contract
  -> materialize/reconcile Work graph and persistent assignments
  -> choose dependency-ready Work (Project hard blockers fail closed)
  -> claim + reserve budget
  -> dispatch employee's provider/Codex through ExternalEffectAttempt
  -> persist AgentRun response/usage and settle exactly once
  -> commit immutable ArtifactVersion + source/host lineage
  -> VERIFYING with frozen independent reviewer
  -> ACCEPTED, bounded internal retry, or durable recovery gate
  -> repeat parallel-ready wave
  -> current contract evaluation + Project Outcome Review
  -> Project Result artifact/proof
  -> RESULT_READY
  -> Founder acceptance -> COMPLETED
```

Crash ownership is expected to be deterministic at: pre-reservation (no call), reserved/pre-dispatch (release/reclaim), post-dispatch unknown (ambiguous reconciliation, no automatic paid replay), response persisted (resume post-processing), artifact committed (reuse exact version), and review persisted (reuse exact review). The acceptance harness must prove these expectations rather than infer them from code shape.

## 5. State machines

Project states observed by the vNext kernel:

```text
DRAFT/PROPOSED -> APPROVED/PLANNING -> ACTIVE <-> BLOCKED
                                         |         |
                                         +-> REVIEW+  (outcome reconciliation)
                                               |
                                         RESULT_READY (represented by REVIEW + current result proof)
                                               |
                                   explicit Founder accept -> COMPLETED
Any non-terminal state -> CANCELLED only through authority.
```

The implementation retains historical aliases, so the proof object—not a label alone—distinguishes ordinary `REVIEW` from Result Ready.

Work states (`WORK_CORE_V018`):

```text
PLANNED -> READY -> EXECUTING -> VERIFYING -> ACCEPTED
                    |              |
                    +-> WAITING <---+  (owned durable gate/retry)
                    +-----------------> ABANDONED
Any eligible non-terminal state ------> CANCELLED
ABANDONED may reopen only through the explicit system-reopen contract.
```

Terminal Work states are `ACCEPTED`, `ABANDONED`, and `CANCELLED`. Ordinary scheduling may not revive them. Every WAITING state must name a durable owner, exit policy and retry/authority boundary.

## 6. Legacy paths

| Class | Paths | Assessment |
|---|---|---|
| A — compatibility read-only | old Command/Operation/Task/Meeting views and historical reports where they only project canonical rows | acceptable with projection tests |
| B — migration only | `legacy_v015.py`, `legacy_company_runtime_v018.py`, `core_v018.py`, migration/reconcile scripts | acceptable only when not reachable as live scheduler |
| C — dead/historical | old templates/static assets and archived runtime helpers not imported by production startup | debt; inventory before deletion in a later release |
| D — still active | Operation planning/execution, Meetings, legacy Task execution used as adapters beneath approved Company Work | must remain subordinate and idempotent |
| E — dangerous alternate writers | any old route/service able to spend, complete Project, revive Work, approve authority, or dispatch vNext delivery outside Company Kernel | release blocker until tests prove absence or the path is gated |

The repository presently exposes both vNext Headquarters/Company routes and old `/operations`, `/tasks`, `/meetings`, `/command` writers. Their mere presence is not proof of an alternate scheduler, but each mutation endpoint is in the blocking audit set. Company Kernel must remain the sole loop that autonomously advances Project delivery.

## 7. Provider boundaries

- OpenAI Responses, Anthropic Messages, Perplexity Sonar Chat Completions and Gemini keep endpoint-specific request/finish/refusal/citation/token semantics in provider adapters.
- The shared boundary normalizes only execution outcome, identifiers, usage/cost, ambiguity and structured payload—not employee identity or business state.
- Codex is not a chat provider. Its authority is an exact job spec, repository snapshot/scope hash, allowed/forbidden paths and host-observed diff/proof.
- Provider output cannot directly change Project/Work/authority truth. Application validation and durable transition services own those writes.

## 8. Artifact and evidence lineage

```text
provider-observed source / Codex filesystem effect
  -> producing ExternalEffectAttempt + AgentRun
  -> immutable ArtifactVersion (content hash, producer Work/Employee/run)
  -> source records and/or host VerificationRecord
  -> exact-version independent semantic review
  -> Work acceptance
  -> downstream context cites accepted version IDs/hashes
  -> Project Outcome Review cites current accepted outputs
  -> immutable PROJECT_RESULT version and contract proof
```

Provider citations, model claims, host proof, code proof, semantic review and acceptance are separate evidence types. A count or prose assertion cannot substitute for lineage.

## 9. Budget authority

The Founder Project Contract hard cap is the ceiling. Cost is admissible only when:

```text
sum(settled actual Project cost + valid outstanding reservations) <= Project hard cap
```

Operation allocation, retry policy and provider estimates cannot raise that ceiling. Reservations precede side effects; definitive no-send/rejection releases them; completed calls settle once; ambiguous post-dispatch attempts remain reserved/reconciled and may not auto-replay. Project #21's cap is NT$30.

## 10. Completion authority

RESULT_READY is valid only for the current governing contract and current evidence basis. It requires all required delivery Work accepted, current immutable artifacts and lineage, independent Project Outcome Review, no unresolved Founder authority, no active/blocking delivery Work, and budget integrity. Mission/Operation completion, Task success, CEO prose, management Work, stale review/artifact, or a UI status cannot independently complete a Project.

## 11. Persistence, transactions, security and observability

- SQLite is authoritative; JSON is used for protocol snapshots and recovery metadata. Critical JSON values require schema/version/hash guards because database constraints cannot enforce their internals.
- Side-effect transactions cannot be atomic with remote providers; durable intent/attempt + idempotent reconciliation is therefore mandatory.
- Founder-only mutation endpoints, API-secret redaction, provider error sanitization, Codex path traversal/repository boundaries and untrusted HTML are the relevant security boundary.
- Founder projections must show current Work owner, gate/retry, provider, cost, failure/recovery owner and next action from durable truth. Projection mismatch is blocking when it can solicit false authority or unsafe replay.

## 12. Build versus borrow

| Area | Classification |
|---|---|
| Founder authority, Project Contract, Company truth, employee accountability, evidence-backed Result Ready | BUILD justified (product semantics) |
| Provider SDK/HTTP lifecycle and structured-output parsing | ALREADY standard where official SDKs are used; keep adapters endpoint-specific |
| durable workflow scheduling, leases, backoff, concurrency and tracing | BORROW candidate after this release |
| current home-grown Operation/Company recovery and JSON protocols | UNNECESSARILY reinvented in parts; migration is structural debt, not an in-release framework rewrite |
| Codex sandbox/path validation | BUILD product policy over standard process/filesystem primitives |

## 13. Top pre-change architectural risks

1. **BLOCKING candidate — live Project recovery discontinuity:** Project #21 retains successful Research run/artifact/source evidence while Work #61 is `ABANDONED`. Recovery must reuse the immutable research and perform only the missing review/proof path.
2. **BLOCKING candidate — alternate legacy writers:** old Operation/Task/Meeting endpoints remain active beside Company Kernel and need proof they cannot bypass claims, Project budget, authority or completion.
3. **BLOCKING candidate — Result Ready aliasing:** Project `REVIEW` is overloaded; every projection and transition must require the current contract-bound result proof.
4. **BLOCKING candidate — hidden protocol truth:** runtime, contract and recovery invariants stored in JSON can drift unless version/hash validation fails closed.
5. **BLOCKING candidate — ambiguous paid effects:** every provider/Codex path must prove no automatic replay after post-dispatch ambiguity and exactly-once cost settlement.
6. **STRUCTURAL DEBT — god modules:** `routes.py` and `services/operations.py` contain broad historical and current responsibilities, increasing alternate-path risk.
7. **STRUCTURAL DEBT — two generations of vocabulary:** Project/Work and Mission/Operation/Task coexist; adapter direction must remain one-way toward canonical Project truth.
8. **STRUCTURAL DEBT — SQLite concurrency:** process-local scheduling plus SQLite limits safe multi-worker concurrency; deployment must remain single active scheduler until durable leases are externally proven.
9. **BETTER — projection clarity:** recovery owner/gate/evidence basis can be surfaced more directly without redesigning Headquarters.

This document is the required pre-correction map. Claims marked “candidate” remain blockers until deterministic production-code tests and the Project #21 DB-copy twin resolve them.
