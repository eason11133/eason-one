# Eason One v0.20 r6 — Project Evidence Review Hotfix

Date: 2026-08-25

## Live blocker reproduced

Project #19 already had deterministic real-loopback HTTP proof for both `/api/build-info` and `/api/healthz`, yet after restart the Company Kernel saw the latest bounded Mission failure and skipped Project-level semantic evidence review. It therefore planned another paid continuation and reopened Founder budget authority before reviewing evidence already present in the Project.

The Founder surface also displayed multiple stale OPEN v0.20 Escalations at once, even though a blocked Project can have only one current governing Founder gate.

## Fix

1. A prior bounded Mission failure no longer suppresses Project-level review of currently accepted evidence. If accepted evidence exists and any Founder criterion still needs semantic proof, the kernel reviews the current evidence packet before planning/buying another Engineer continuation.
2. Opening a new v0.20 Project gate supersedes every older OPEN Project gate. One blocked Project has one current Founder decision.
3. Founder Project read models expose only the newest first-class v0.20 Escalation if stale historical OPEN rows survived an earlier build. Older rows remain audit history.
4. `scripts/reconcile_v020_project_evidence.py` retires only stale recovery/budget gates for the selected Project after deterministic evidence recovery, returns the Project to ACTIVE, and lets the normal kernel re-evaluate current evidence. It does not expand Founder authority, create paid AgentRuns, rewrite the immutable Project Contract, or claim Project completion.

## Project #19 handoff

Run the reconciliation while Headquarters is stopped. After restart, Project #19 must first perform Project outcome review against the existing accepted Artifact + deterministic HTTP proof. It may only request additional Founder budget if that review proves a remaining immutable Founder criterion genuinely requires new paid work.

Do not approve the pre-r6 recovery/budget cards.
