# Eason One v0.20.0 — Company Core Rebuild Handoff

## What changed

v0.20 replaces Mission-governed Project closure with one Founder Project Contract / Company Kernel path:

`Founder Project Contract → Work → Execution → Artifact → Verification → Project Outcome → bounded continuation/recovery → Result Ready`

Key changes:

- immutable Founder Project Contract with hash plus append-only Founder budget amendment ledger;
- Project outcome evaluated across accepted verified Work rather than Mission completion criteria;
- Mission failure no longer implies Project failure;
- bounded CEO continuation inside existing Project authority without fake Founder approval;
- default Project approval exposes a deterministic total Project envelope with one bounded continuation/recovery reserve;
- Project Result is persisted as Artifact + Project Contract verification;
- active `company_runtime.py` is a thin process shell; legacy Project closure/failure is outside the v0.20 governing path;
- normal restart no longer replays historical migrations/cutovers;
- explicit `migrate_v020.py` freezes Project Contracts and reopens unsupported historical REVIEW/FAILED Project claims;
- bounded semantic-review/continuation retries prevent infinite provider spending.

The release constraints are in `docs/V0.20-ENGINEERING-CONSTITUTION.md`. Acceptance mapping is in `docs/acceptance/v020_core_rebuild_matrix.md`.

## Install

Use the supplied `install-eason-one-v020.ps1` with the release ZIP. The installer:

1. requires the existing target `.venv`;
2. runs a source structural preflight;
3. stops the existing Headquarters process when the installed stop script can identify it, then creates a timestamped rollback checkpoint using SQLite’s backup API for a consistent DB snapshot;
4. installs application-owned files without touching `.env` or `.venv`;
5. compiles source and runs the v0.20 structural audit;
6. runs the explicit v0.20 DB migration with runtimes disabled;
7. runs the focused v0.20 authority/migration diagnostics;
8. writes `.v020-backup-path.txt` for one-command rollback;
9. automatically restores code + the consistent pre-install DB checkpoint if installation fails.

## Engineering verification after install

From `D:\school\eason-one`:

```powershell
.\scripts\verify-v020.ps1
```

This is diagnostic evidence only. It is not Product acceptance.

## Founder Product acceptance — required

Run Headquarters:

```powershell
.\scripts\run-headquarters.ps1
```

Create one small **real** Project using the live configured provider/Codex. A good acceptance Project requires at least two Project-level criteria so Mission #1 can produce partial evidence and demonstrate autonomous continuation.

Observe all of the following:

1. Before approval, the proposal shows Project objective, success criteria, constraints, **total Project budget**, and first bounded Mission budget.
2. Approval creates one durable base Project Contract. Later Missions do not alter its hash/criteria/base budget/deadline. A later Founder budget grant must appear as a separate Contract Amendment with a changed effective authority hash.
3. Real Employee Work executes and produces a real Artifact/verification trail.
4. If Mission #1 does not satisfy the whole Project, Project stays ACTIVE and CEO creates the next bounded Mission without a fake Founder approval.
5. If one bounded Mission fails, Project is shown as recovering/continuing rather than automatically FAILED.
6. If remaining Project budget/deadline is genuinely exhausted, Founder receives an explicit authority decision instead of silent expansion.
7. When every Founder criterion is proven, Project moves to Result Ready and a Project Result Artifact + Project Contract verification are visible/persisted.
8. Cost/history/contribution views correspond to actual Runs/Artifacts/events; no decorative fake activity is accepted.
9. Restart Headquarters mid-Project once. Previously terminal/accepted Work must not replay, and the Project must resume from durable truth.

### PASS

PASS only if the real Project reaches Result Ready through the above path without hidden manual DB edits, fake activity, legacy Mission closure, authority expansion, duplicate external effects, or restart loss.

### FAIL

FAIL if any of these occur:

- Mission completion closes the Project without proving the Project Contract;
- Mission failure marks the Project FAILED;
- CEO routine continuation creates another Founder approval card;
- Project total budget was not visible before approval, changes without Founder action, or changes without a valid append-only Contract Amendment;
- accepted Work repeats after restart;
- provider/tool retries loop unboundedly;
- Result Ready has no durable Project Artifact/Verification;
- UI state contradicts durable Company Truth.

## What to send back on FAIL

Send:

- screenshot of the Project page and Attention page;
- the exact Founder command and approval proposal screenshot;
- PowerShell output from `run-headquarters.ps1` around the failure;
- `verify-v020.ps1` output;
- Project ID and Mission/Operation ID shown in the UI;
- if available, screenshot of System/runtime health.

Do **not** delete the DB or reinstall before preserving those artifacts.

## Rollback

Stop Headquarters, then:

```powershell
.\scripts\rollback-v020.ps1 -TargetRoot "D:\school\eason-one"
```

Rollback restores the complete application-owned directories/files and the pre-install SQLite DB from the checkpoint recorded in `.v020-backup-path.txt`. `.env` and `.venv` are not replaced.
