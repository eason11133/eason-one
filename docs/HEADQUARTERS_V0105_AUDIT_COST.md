# Headquarters V0.10.5 — Audit linkage and truthful cost authority

V0.10.5 closes the gaps discovered during the first real CEO Direct Line Run.

## Fixed

- A successful `CEO_FOUNDER_REQUEST` Run is back-linked to the governed Operation created from that exact validated output.
- Application-level structured validation is persisted as `PASSED` or `FAILED`.
- Run latency is rendered from persisted `started_at` and `finished_at` timestamps.
- CEO planning cost is shown separately from the Founder-authorized execution envelope.
- Planning cost remains company spend, but does not consume or reduce execution authority.
- Independent Missions no longer inherit unrelated `NULL`-scoped Meetings or Contribution events.
- Existing V0.10.4 planning Runs are repaired transactionally when a unique exact Operation match exists.

## Cost semantics

- **Planning cost**: the CEO Provider call that interprets Founder intent and proposes an Operation.
- **Authorized execution**: the budget the Founder may approve for subsequent specialist work.
- **Execution spent**: costs from Runs already scoped to the approved Operation runtime.
- **Company spent**: all real model costs paid by the company, including planning.
