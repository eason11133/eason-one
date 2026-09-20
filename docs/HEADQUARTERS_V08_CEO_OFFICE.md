# Eason One Headquarters V0.8 — CEO Office

## Release objective

Headquarters is the CEO's office, not a generic dashboard or a chat wrapper. The Founder should understand the company before asking a question, receive persisted facts immediately, and wait for a paid model only when judgment or governed action is actually needed.

## Runtime paths

### BRIEF

- Read-only.
- Generated from persisted SQL state.
- No provider call.
- Used for status, costs, people, Meetings, blockers, and recent changes.

### ADVISE

- Shows immediate company context first.
- Calls the assigned CEO runtime only for prioritization, trade-offs, or recommendation.
- Produces an auditable AgentRun.
- Does not mutate authority.

### ACT

- Shows immediate company context first.
- Calls the CEO to prepare governed work.
- New authority remains pending Founder approval.
- Never auto-approves a Mission or Project plan.

## CEO Office UX

- Deterministic briefing on entry.
- `Since your last visit` activity derived from the browser session timestamp and authoritative events.
- CEO recommendation based on governance, blocking incidents, live Meetings, delivered results, or the current Mission.
- Founder desk for decisions, current Mission, live Meeting, and company activity.
- Inline CEO direct line with AUTO / BRIEF / ADVISE / ACT routing.
- Visible phases: request received, state loaded, CEO runtime, response ready.
- First useful information appears before a paid judgment finishes.

## Research capture

Every CEO-office interaction automatically records:

- route,
- first-useful latency,
- total latency,
- server preview latency,
- provider execution latency,
- success/failure,
- linked AgentRun when available.

These `UX_METRIC` records remain in the full Research export but do not clutter Founder-authored evidence records.

## Acceptance

- GET `/headquarters` constructs no provider.
- BRIEF preview constructs no AgentRun.
- ADVISE and ACT return immediate context before execution.
- New authority still requires Founder approval.
- Old V0.7 Meeting, Research, Employee AI Core, and deterministic status-report tests remain green.
