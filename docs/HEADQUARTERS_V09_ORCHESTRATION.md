# Eason One Headquarters V0.9 — CEO Delegation Loop

## Product contract

Headquarters remains the CEO office. Founder messages use one persistent CEO dialogue, one composer, and one Send action. The input clears immediately. Internal BRIEF / ADVISE / ACT routing is no longer exposed as a Founder control.

A request to solve, research, investigate, analyze, build, or deliver work is routed to a governed CEO Operation proposal. The CEO reply must identify the proposed Employees, independent reviewer, Operation budget, completion criteria, and a bounded Meeting envelope when specialist reconciliation is justified.

No new authority starts until Founder approval.

## Approved execution loop

After `APPROVE & START`, the CEO office drives the existing authoritative Operation runtime:

1. Materialize Project and Tasks.
2. Assign the approved Employee and reviewer.
3. Execute the Task.
4. Run independent review.
5. Open a bounded internal Meeting when the approved trigger requires it.
6. Advance only within round, speaker, output, token, retry, and real-cost ceilings.
7. Consume the latest Meeting result into the Operation evidence packet.
8. Verify every approved completion criterion.
9. Persist the final CEO-to-Founder report.

The HQ live panel shows status, whole-loop progress, working Employees, current Task, latest governed event, Meeting summary, Meeting token/cost usage, Operation budget spent/remaining, and any additional budget request.

## Meeting governance

The CEO proposes:

- trigger: `NEVER`, `ON_MATERIAL_CONFLICT`, or `BEFORE_FINAL_REPORT`
- participants: maximum four
- rounds: one to three
- speakers per round: one to four
- contribution output cap: 192–1024 tokens
- cumulative Meeting token ceiling: 1,200–12,000 tokens
- Meeting real-cost ceiling inside the Operation budget
- retry allowance: zero or one

A deterministic feasibility floor raises an under-provisioned cumulative token ceiling before the proposal is shown to Founder. For the tested two-round, three-speaker envelope, the approved ceiling is normalized to 11,000 tokens. This remains a hard ceiling, not a usage target.

A paid truncated or structurally invalid Meeting contribution receives at most one compact automatic retry when that retry was already included in the Founder-approved contract. The failed call remains in cost and audit history. A second failure stops and requests Founder authority.

## Whole-loop progress

Progress no longer becomes 100% merely because Tasks are done. The read model accounts for:

- approved contract
- Task execution and review
- required Meeting
- goal verification
- final Founder report

The deterministic end-to-end fixture advances through 8%, 58%, 75%, 80%, 85%, 95%, and 100% as the governed stages complete.

## Safety behavior

Opening HQ never silently resumes an existing paid Operation. Existing RUNNING work displays `RESUME CEO`; newly approved work starts immediately because the Founder approved it in the current interaction. Closing the page stops the browser runner; reopening HQ preserves authoritative state and permits explicit resume.

Operation and Meeting budgets are checked before every provider call. Insufficient authority changes the Operation to `WAITING_FOR_FOUNDER`; the CEO office displays the exact additional authorization request.

## Verification

Focused V0.9 regression suite:

- one CEO composer and immediate input clearing
- natural persistent dialogue shell
- automatic problem-to-ACT routing in English and Chinese
- proposal team, reviewer, budget, Meeting envelope, and approval
- bounded automatic Meeting retry
- live worker / progress / budget / Meeting read model
- direct service-level full loop
- HTTP-level Founder problem → approval → Employee execution → review → Meeting → goal verification → final CEO report
- V0.8, V0.7, Headquarters V1, cross-surface, CEO adaptive-stage, and slice 0091 regressions

Current focused result: 67 passed, 0 failed. SQLAlchemy legacy API warnings remain non-blocking.
