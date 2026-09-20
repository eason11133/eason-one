# Eason One v0.20 r5 — Deterministic HTTP Recovery

Purpose: stop paying Codex/Engineer repeatedly for deterministic HTTP proof that the host runtime can verify locally.

## Root cause fixed

1. `HOST_HTTP_CONTRACT` used Flask `test_client`, while the Founder Project Contract explicitly required a started service and a real HTTP request.
2. Project outcome could not reuse deterministic HTTP proof at Project scope, so the CEO kept creating paid verification Missions.
3. Repeated delegated failures were fingerprinted with a changing Mission id and could loop until Project budget exhaustion.
4. Budget escalations could accumulate as stale Founder cards across recovery Missions.

## r5 behavior

- HTTP proof starts an isolated Werkzeug server on an ephemeral `127.0.0.1` port and issues a real urllib HTTP request.
- Existing accepted code Artifacts can receive `PROJECT_HOST_HTTP_REVERIFY` proof without a new AgentRun or provider call.
- Project outcome consumes that proof before planning another paid Mission.
- The same failure signature repeating twice stops as `SYSTEM_RECOVERY_REQUIRED` instead of spending more Founder budget.
- Only one current Project budget decision remains live; superseded budget-shaped gates are resolved.
- `scripts/reconcile_v020_http_proof.py` can recover an already-blocked Project from existing accepted code evidence without expanding Founder authority or refunding/rewriting actual Company spend.

## Current Product Gate

The r5 installer targets Project #19 by default. It will run deterministic live HTTP proof against the existing accepted repository Artifact and retire only budget-shaped escalations that are proven unnecessary by that HTTP evidence.
