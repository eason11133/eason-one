# Product Gate Failure — Reopened Budget Authority After Founder Reject

Date: 2026-08-28
Project: #20 Founder Ping Endpoint
Observed snapshot before fix: governance-r6-20260828

## Live product evidence

Project #20 reached accepted Work/evidence after the r6 host-proof handoff repair. The Company then opened a precise Project budget amendment for the remaining closure/review cost. Founder explicitly REJECTED the additional authority. The canonical Decision cleared the gate and returned control to Company recovery inside the existing NT$5 cap.

On the next scheduler pass the same unchanged Project authority state attempted the paid closure/review again, computed another Project shortfall, and opened a new BUDGET_AUTHORIZATION Founder gate. This means a Founder REJECT was durable as an audit Decision but not durable as negative authority: automated Company code could keep asking again until the Founder accepted.

## Root cause

`governance.open_gate()` deduplicated only currently OPEN gates. It did not consult committed REJECT Decisions. `resolve_gate(REJECT)` correctly granted no authority and restored Project control to Company execution; the next paid step therefore hit the unchanged budget cap and `request_budget_gate()` created another gate.

## r7 correction

- Founder REJECT becomes a durable negative authority receipt bound to current governing Contract + budget authority hashes.
- Automated Project budget extension requests are suppressed after a rejection while those authority hashes are unchanged, even if the recomputed shortfall amount drifts slightly.
- Exact execution-scoped authority requests are suppressed only when the exact gate identity was rejected.
- Explicit Founder-initiated budget changes remain possible through manual authority controls.
- Company Kernel translates a suppressed budget re-request into `AUTHORITY_EXHAUSTED` / Project BLOCKED inside the current cap, with no new Founder attention.
- Every suppressed reopen emits `FOUNDER_GATE_REOPEN_SUPPRESSED` for audit.

This repair does not fabricate completion, increase budget, or erase prior Work/Decision history.
