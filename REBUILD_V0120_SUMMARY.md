# Eason One V0.12.0 Rebuild Summary

This package was rebuilt from `eason-one-current-20260805.zip`.

## Implemented

- Deterministic five-path CEO routing
- Direct deterministic pause/resume/cancel/cost commands without model calls
- Authoritative Operation state machine and append-only events
- DB-backed queue, checkpoint, worker lease, stale-worker recovery, and idempotent steps
- Atomic SQLite worker leasing and pre-provider budget reservation
- Provider pre-call cost reservations with hard caps and ambiguous-spend holding
- Expired unknown provider calls remain `AMBIGUOUS` and continue holding authority until reconciled
- Maximum provider calls, revisions, messages, and elapsed time
- Persistent bounded Meeting lifecycle and Meeting events
- Meeting creation at approval only when the route explicitly requires it
- Initial approval vs later Founder gate separation
- Cancelled Operation no longer leaves a CEO-created Project falsely Current
- Founder Mission screen shows route, stage, actual/reserved/available budget, limits, checkpoint, and authority events
- People and Meetings hidden from primary navigation while historical routes remain accessible
- Additive idempotent SQLite migration and PowerShell install/verify scripts

## Verification performed in the build environment

- Python compileall: passed
- Jinja parse for 44 templates: passed
- SQLite integrity_check: passed
- SQLite foreign_key_check: zero violations
- Migration run twice on the same copied database: passed and idempotent
- Package secret scan: no `.env`, private key, or real provider credential included
- Full pytest: not executable in the build container because Flask and Flask-SQLAlchemy are unavailable from its package source

Run `scripts\\verify-v0120.ps1` inside the existing Windows virtual environment to execute the full test suite.
