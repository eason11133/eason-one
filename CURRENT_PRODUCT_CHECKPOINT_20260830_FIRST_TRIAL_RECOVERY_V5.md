# Eason One — First Trial Recovery V5

Status: LIVE FIRST-USABLE-TRIAL BLOCKER FIX

This checkpoint fixes two blockers observed in the first approved Project on the real Windows host.

1. Research Work exhausted two OpenAI attempts with `OUTPUT_TRUNCATED: max_output_tokens` before the Researcher was moved to Perplexity Sonar. Runtime now treats a terminal Research failure as superseded only when all of the following are true: no accepted/submitted Artifact exists, the failed attempts are safe/known, the Work exhausted bounded retries, and the currently effective formal Research provider is different. It reopens the Work once through the explicit auditable `reopen_abandoned()` recovery boundary and preserves all historical failed AgentRuns.
2. Retry output ceilings now grow after `OUTPUT_TRUNCATED` instead of repeating the same 1600-token cap. The retry remains bounded by the selected ModelConfig.
3. HR `USE EXISTING STAFF` can no longer strand a capability-gap Work when deterministic roster truth shows that no eligible non-CEO specialist owns the required capability. Existing qualified staff are reassigned; otherwise, inside valid delegated Project hiring authority, the impossible model recommendation is deterministically corrected to HIRE and a real Persistent Employee is materialized without a second HR provider call or Founder interruption.
4. Superseded provider-route failures do not consume the newly configured provider's retry budget.

The approved Founder Project, Contract, budget cap, Work identities, historical AgentRuns, and Founder authority are preserved.
