# Eason One — Durable vNext Elapsed Recovery

Status: FIRST USABLE TRIAL BLOCKER FIX

Observed on live Project #21:
- Research Work #61 was correctly reopened onto Perplexity/sonar by V5.
- AgentRun #195 selected Perplexity but failed PRE_DISPATCH with `Operation maximum elapsed time reached`.
- fallback Run #196 failed at the same PRE_DISPATCH gate. No provider request was sent.

Root cause:
`Operation.max_elapsed_seconds=3600` is a legacy Mission wall-clock planning guard measured from persistent `runtime_started_at`. It was still enforced for vNext Work even though other legacy Operation planning caps had already been fenced away from Work/Project authority. A durable Project resumed the next day and was therefore vetoed before dispatch.

Fix:
- legacy Operation wall-clock elapsed limit remains for legacy Operations only;
- vNext Work provider authorization is governed by current Project/Work authority and resource truth;
- exact historical PRE_DISPATCH elapsed-veto failures are reconciled once, marked as system/platform fault, and do not consume Work retry budget;
- no accepted Artifact/effect may exist for this automatic reopen.

This is a current-path release blocker repair, not a feature expansion.
