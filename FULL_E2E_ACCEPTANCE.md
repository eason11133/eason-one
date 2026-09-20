# Full E2E acceptance

Date: 2026-09-02  
External-call policy: all OpenAI, Anthropic, Perplexity, Gemini and Codex transports are deterministic fakes; real calls = 0.

## Project #21 durable starting point

- Project: `#21 First Company Trial — Taiwan AI Opportunity Brief`, LIVE, hard cap NT$30.
- Research Work #61 initially `ABANDONED` after review failures.
- Producing Run #198: `perplexity/sonar`, `SUCCEEDED`.
- ArtifactVersion #40 exists and is bound to Run #198 with 15 provider-observed sources.
- Review Runs #199/#200 are preserved platform/evidence-scope failures.
- Review Runs #201/#202 are preserved paid Claude `max_tokens` truncations under the obsolete 1,100-token cap.
- Works #62–#65, Product Strategist, Critic/Claude and Engineer/Codex assignments are read from the copied live database.

## Deterministic production-code progression

Command:

```text
.python313\python.exe scripts\audit_architecture_release.py --database instance\eason_one.db
```

Observed result:

```text
ARCHITECTURE_DIGITAL_TWIN_PASS
RECOVERIES: 1
RESTARTED_KERNEL_TICKS: 16
PROJECT_21_STATE: REVIEW
WORK_STATES: #61=ACCEPTED, #62=ACCEPTED, #63=ACCEPTED, #64=ACCEPTED, #65=ACCEPTED
RESEARCH_RUN_198_REPLAYED: False
PROJECT_COST_TWD: 6.984351
PROJECT_CAP_TWD: 30.0000
FAKE_PROVIDER_CALLS: 7
REAL_PROVIDER_CALLS: 0
```

The `REVIEW` label is Result Ready only because `result_ready_proof(Project #21)` returned current immutable `PROJECT_RESULT` artifact/proof bound to the governing Contract, authority hash and current evidence basis.

## Restart matrix

The harness recreates the Flask application between productive Company-kernel batches. Durable SQLite truth owns each continuation. The sequence covers exact-version Research review recovery, Work execution, Artifact commit, independent review, dependency release, fake Codex execution/host delta, Project Outcome Review and Result Ready. Re-running deterministic platform recovery applies zero additional recovery and creates zero provider attempts.

## Failure matrix

| Scenario | Expected durable outcome | Executable evidence |
|---|---|---|
| HTTP 400/401/403/409/422 | definitive rejection; release reservation; no ambiguous replay | provider protocol audit + HTTP classification tests |
| HTTP 408/429/500/503 | transient rejection; bounded retry/Retry-After; no SDK double retry | provider protocol audit + execution retry tests |
| timeout / connection reset after dispatch | `AMBIGUOUS_POST_DISPATCH`; reservation retained/marked ambiguous; no automatic replay | vNext ambiguity/sibling tests |
| malformed JSON / schema invalid | persisted failed postprocess; no fake Artifact/acceptance | structured-output execution tests |
| output truncation | usage/cost retained; bounded larger retry; historical old-cap recovery is review-only | Patch 004.1/007 tests + architecture regression |
| response received, crash before persistence | durable external-effect reconciliation; automatic paid replay denied | external-effect/restart tests |
| Artifact persistence or review crash | resume from producing run/exact version; never infer acceptance | Work recovery/review evidence tests |
| budget reservation crash / duplicate tick | unique reservation/idempotency keys; no double settlement | Operation/Work budget tests |
| restart / duplicate recovery | explicit one-time recovery markers and current hashes | architecture twin + recovery tests |
| Codex extra/source file | host delta outside exact allowed set restored; failed scope violation | architecture Codex boundary test |
| Codex proof gap | bounded proof-only retry/reconciliation; no implementation replay | governance structural audit + Work proof tests |
| review reject / unproven | no acceptance; bounded remediation/evidence continuation | review/evidence tests |
| dependency deadlock | no fake dispatch; management/recovery owns topology failure | Work dependency/kernel tests |
| impossible HR recommendation | deterministic capability gap/hiring recovery; no Founder budget authority minted | team formation/HR tests |

## Release gates

R2 installer note: the first second-review installer attempt was fail-closed by the new regression gate and automatically rolled back because several port tests reused retired historical fixtures/exception contracts. R2 keeps the production architecture changes, but rewrites those tests against current v0.20 semantics before the same target-side gates are allowed to commit.


- Python compilation: required.
- Core structural audit: PASS.
- Governance audit: PASS (99 invariants at execution time).
- Provider contract audit: PASS.
- Review/evidence contract audit: PASS.
- Budget: Project #21 remains below NT$30.
- Codex: exact one-path disposable workspace; no `.git`, `.env`, `instance/` or live DB; full-delta all-or-nothing copyback; real Codex calls 0.
- Test-contract gate: every test module is explicitly classified; current runtime tests are selected semantically rather than by filename pattern.
- Architecture/state machine: focused regressions + full pytest suite.
- The prior 99-test count is no longer the acceptance authority. The second-review closure adds explicit current-regression ports and an exact retired→current contract inventory; the installer executes the resulting current release gate on the target Python 3.13 runtime before commit.
- The initial unconstrained historical collection completed with `721` cases, `334` failures and `4` skips. Those failures clustered in mutually exclusive Slice 003–018 UI/model contracts plus a small v0.20 compatibility set. The v0.20 defects were fixed; pytest collection is now explicitly versioned to v0.20/release tests. Historical test files are retained, not deleted or represented as current acceptance.
- Final compilation, structural audits and `git diff --check` are recorded in the final release report.

## Ten final questions

1. Current #21 DB copy reaches Result Ready: YES.
2. Successful Research is not rerun: YES.
3. Ordinary Founder intervention: NONE.
4. NT$30 Contract respected: YES.
5. Fake Artifact/evidence/review accepted: NO; fakes replace transport responses, while production code persists and verifies actual twin artifacts/lineage.
6. Legacy Mission/Task alternate completion: NO.
7. Active autonomous Company scheduler count: ONE.
8. Completion derives from current Contract-bound evidence: YES.
9. Restart/retry/duplicate truth deterministic: YES for covered matrix.
10. Remaining structural debt is recorded: YES, in `BACKLOG.md`.
