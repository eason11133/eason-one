# Eason One v0.20 r3 — Host HTTP Validation Hotfix

Observed during the first Windows Product Gate after r2 installation:

- a historical ACTIVE Version Info API Project correctly resumed after restart/migration;
- host verification reached criterion-level HTTP proof mapping;
- `host_validation.py` referenced `_HTTP_RE`, which was never defined in that module;
- runtime emitted `name '_HTTP_RE' is not defined` and correctly did **not** claim verified completion.

## Fix

- define one module-local `_HTTP_METHOD_PATH_RE` matcher in `host_validation.py`;
- use that same matcher for HTTP contract extraction and criterion-level proof mapping;
- add a focused v0.20 regression test for the exact Version Info API matcher path;
- extend the dependency-free structural audit to reject the old undefined `_HTTP_RE.search` reference.

No Project Contract, budget authority, migration, Company Kernel, or persistence semantics changed in r3.

## Product interpretation

The old Project appearing before a new Founder prompt is expected **only if** migration/restart considers that Project non-terminal and not fully verified. v0.20 is designed to resume durable incomplete Projects. The observed card said no verified completion was claimed, so resuming it is consistent with the restart contract.
