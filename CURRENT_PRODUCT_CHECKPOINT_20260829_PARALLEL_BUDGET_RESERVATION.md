# Eason One — Parallel Company Budget Reservation Checkpoint

Date: 2026-08-29
Status: WORKING CHECKPOINT — DO NOT INSTALL unless Founder is explicitly told `現在請更新`.

## Product movement

Real provider dispatches owned by vNext Work now enter the durable CostReservation ledger before the provider boundary.

Before this checkpoint, Work-first execution checked settled Project cost but deliberately skipped the legacy Operation reservation mechanism. With parallel Persistent Employees, two provider calls could therefore observe the same unsettled Project balance before either CostEvent existed.

Current behavior:

- every real provider call with governed Work authority creates a durable pre-dispatch reservation;
- SQLite uses the existing `BEGIN IMMEDIATE` boundary; row-locking databases lock Company → Project → Operation in deterministic order;
- the atomic check includes active/ambiguous reservations from other Employee executions in the same Project;
- Company-wide active reservations are also counted before new paid dispatch;
- vNext still treats the Founder-approved Project Contract budget as the hard business envelope;
- legacy Operation/stage/Work budget estimates are not promoted back into independent Founder authority for vNext;
- reservation settlement continues to distinguish CONSUMED / RELEASED / AMBIGUOUS provider-effect truth.

## Why this matters for first usable Project trial

Parallel execution is not safe if two Employees can both spend the same remaining Founder-authorized budget. This checkpoint makes concurrency and budget authority use the same durable boundary instead of relying on settled cost after provider responses return.

## Local install state

The Founder's local Eason One repo is still the previous local baseline. This checkpoint is cumulative-source progress only.
