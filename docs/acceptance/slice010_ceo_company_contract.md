# Slice 010 — CEO Company Operating Contract

This contract freezes the Founder/CEO authority boundary for Slice 010.

## Product law

The Founder manages the CEO. The CEO manages internal Company execution
inside a single Founder-approved, bounded operation. No browser request may
perform more than one provider operation, and no browser closure may leave a
hidden spending loop.

## CEO Office

The normal Command surface is the CEO Office, not a Company dashboard. It
contains the CEO presence, one deterministic persisted report, the command
composer, and collapsed recent conversation. GET navigation makes no provider
call.

## Operations

`OPERATION_PLAN` is a validated CEO output. A planned operation has no
executable Tasks until Founder approval. Approval materializes bounded Tasks
once, records the authorized TWD envelope, and starts a persisted,
browser-driven loop. Deterministic state selects Task execution, Review, or
final CEO synthesis. Each idempotent step performs at most one provider call.
Company and operation budget gates both apply. Pause, stop, paid failure, and
budget exhaustion prevent future execution until Founder authority permits it.
CEO-created Meetings reuse the existing Meeting engine and retain Meeting,
operation, and Company ceilings.

## Workforce governance

Human Resources is a real Department with an HR Director. A TalentTemplate is
an inert candidate definition: it is not headcount, has no authority, and
costs zero. Founder, CEO, and Directors may request staffing; HR review is
required; only Founder approval creates one Employee. Approval is idempotent
and itself makes no provider call. Pricing calculations use persisted
ModelConfig prices. Candidate imports preserve supplied records and are
idempotent. If the exact Founder 147-role source is absent, no roles are
fabricated.

## Founder override

Existing manual Task, Review, Meeting, briefing, and paid-retry controls remain
reachable under clearly secondary Founder Override / Advanced surfaces.

## Presentation

Founder-facing canvases use the canonical Deep Navy tokens. Team organization
contains hired Employees only and separates People Operations and Talent Pool.
Work prioritizes objective, CEO ownership, current activity, next step,
progress, budget, results, and Founder decisions.

## Safety

No external action, destructive action, model reassignment, paid retry,
authoritative Brain mutation, hiring, or budget expansion occurs without
Founder authority. Existing Meeting recovery, Economy Meeting behavior,
AgentRun history, CostEvent history, and provider reconciliation remain
unchanged.
