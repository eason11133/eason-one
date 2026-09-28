# Architecture backlog

These items are explicitly outside the consolidated blocker release.

## Structural debt

1. Split `services/operations.py` and `routes.py` into canonical vNext services plus narrow read-only compatibility adapters. Preserve route behavior until deprecation telemetry exists.
2. Retire legacy Task/Mission mutation endpoints in stages after all callers use Project/Work commands. Keep immutable audit/migration reads.
3. Replace process-local scheduling/leases with a mature durable-workflow or transactional job substrate before supporting multiple active scheduler processes. Founder/company semantics remain in Eason One.
4. Normalize critical JSON identifiers and uniqueness constraints incrementally. Keep versioned immutable JSON snapshots for audit, not primary mutable authority.
5. Add explicit scheduler fairness cursor/lease if Company scale exceeds the current deterministic scan; quiescent-result suppression is sufficient for the single-scheduler V1 deployment.
6. Expand chaos injection at every database commit boundary under a dedicated test database. Current release covers the declared failure classes without introducing a second recovery engine.
7. Consolidate historical installers/checkpoint documents outside the runtime source package after release retention policy is agreed.
8. Move preserved pre-v0.20 pytest contracts into an explicit historical-contract archive. They are intentionally outside current pytest discovery because their UI routes and removed model types are mutually exclusive with the v0.20 cutover.

## Better, not blocking

- Improve Founder projection of recovery owner, evidence basis and retry timing without changing UI architecture.
- Replace magic status strings with typed enums after migration compatibility is isolated.
- Add tracing/OpenTelemetry around existing durable IDs; do not make tracing a truth source.
- Evaluate PostgreSQL when concurrent writers or multi-process scheduling become product requirements.
