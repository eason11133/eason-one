# Slice 010.2 — Goal Completion Wiring Acceptance Contract

Frozen before production implementation on 2026-07-26 Asia/Taipei.

## Product invariant

Paid Review, Meeting, hiring, and CEO decision intelligence must materially
inform subsequent Employee execution. An approved hire created to resolve an
Operation capability gap must perform meaningful governed work before that
Operation can complete.

## Required behavior

1. Task execution within an Operation receives a bounded, auditable Operation
   context containing the objective, state, budget, relevant CEO decisions,
   structured Meeting results, latest Review details, remediation instructions,
   and relevant workforce changes.
2. Full Meeting transcripts and raw AgentRun history are excluded.
3. Latest Review decision, summary, issues, and required changes reach rework.
4. A strict CEO decision can create one remediation Task or reassign eligible
   incomplete work without new Founder micromanagement.
5. Dynamic Task creation is limited to 20 total Tasks per Operation.
6. CEO mutations retain the approved Project, Operation, budget, active
   Employee, reviewer, and audit boundaries.
7. Duplicate decision replay does not duplicate Task creation or reassignment.
8. DONE Tasks cannot be reassigned.
9. Hiring approval alone is not progress: unresolved work still blocks
   completion.
10. The newly approved Employee can be selected by CEO planning and must have
    at least one Operation AgentRun in the strengthened hiring lifecycle.
11. Company, Operation, Meeting, paid-failure, ambiguous-call, and Meeting
    recovery safety remain authoritative.

The accompanying test module is frozen with this contract. Neither file may be
changed after their SHA-256 hashes are recorded.
