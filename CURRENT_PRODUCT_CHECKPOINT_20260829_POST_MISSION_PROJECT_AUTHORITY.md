# Eason One Product Checkpoint — Post-Mission Project Authority

Date: 2026-08-29
Install state: DO NOT INSTALL unless ChatGPT explicitly says `現在請更新`.

## Release target

First Usable Multi-Employee Project Trial.

## Blocking current-path defect fixed

The normal v0.20 closeout sequence intentionally marks a bounded Mission Operation `COMPLETED` before the Project-level independent outcome review runs. Failed Missions likewise become `FAILED` before CEO bounded continuation planning.

The shared provider reservation layer still treated the legacy Operation lifecycle as provider-call authority for every run. Therefore both of these legitimate Project-management calls could be rejected after delivery had already finished:

- `COMPLETED Mission -> PROJECT_OUTCOME_REVIEW`
- `FAILED Mission -> CEO_PROJECT_CONTINUATION`

This was a release blocker: multi-Employee delivery could complete successfully and still never reach Project Result Ready.

## Fix

`operation_kernel` now distinguishes true vNext Work by durable `runtime_semantics` and validates provider dispatch against Project/Work authority instead of blindly reviving Operation lifecycle authority.

- READY/RUNNING vNext delivery keeps the normal Mission execution boundary.
- A vNext `MANAGEMENT` Work may continue after a `COMPLETED` or `FAILED` Mission only while the governed Project and Work remain active.
- Terminal Mission state never reopens ordinary delivery Work, preventing stale provider/Codex replay.
- Legacy Work adapters remain under legacy Operation limits; merely possessing a Work row no longer grants vNext semantics.

This preserves the authority chain:

Founder Project Contract -> approved Mission lineage -> active Project MANAGEMENT Work -> outcome review / bounded continuation

without turning `Operation.status` back into Project authority.

## Regression coverage added

- completed Mission permits Project outcome management review
- failed Mission permits bounded continuation management planning
- completed Mission still rejects stale delivery provider execution
- v0.20 core structural audit now fails if terminal Mission lifecycle again revokes Project-management provider authority

## Validation available in this environment

- Python source compile: PASS (168 files before generated-cache cleanup)
- v0.20 core structural audit: PASS
- v0.20 Governance structural audit: PASS (99 invariants; Governance was not reopened)
- `headquarters.js` syntax check: PASS
- Full Flask/SQLAlchemy pytest runtime: still unavailable in this sandbox because Flask / Flask-SQLAlchemy are absent. Official PyPI metadata was checked; restoring the sandbox dependency set itself is not treated as Eason One product work.

## Next release-path focus

Continue the same dry run from Project outcome review -> bounded retry / continuation -> current Contract-bound Result Ready -> Founder outcome surface. Only release-blocking current-path defects should delay the first Windows Project trial.
