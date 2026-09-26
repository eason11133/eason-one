# Eason One — Perplexity JSON Schema Recovery V7

Status: FIRST USABLE MULTI-EMPLOYEE PROJECT TRIAL · live blocker repair.

## Live evidence

Project #21 reached Perplexity Sonar for Research Work #61. Run #197 was rejected with HTTP 400 because the adapter sent OpenAI legacy `response_format.type=json_object`; Perplexity explicitly accepts `json_schema` or `text`. No Research Artifact was accepted.

## Repair

- Perplexity structured Task output now uses `response_format.type=json_schema` with the frozen Eason One response schema.
- Definitive provider client rejections (400/401/403/404/422) are recorded as `FAILED_KNOWN` / `REJECTED_POST_DISPATCH`, not ambiguous external effects; reservations are released.
- Historical Run #197 is narrowly reconciled only when its exact Perplexity `json_object` HTTP 400 signature is present, no submitted/accepted Artifact exists, and its exact reconciliation gate is still open.
- The repaired platform attempt does not consume Work retry quota.
- The Work returns to READY for one fresh Sonar call; no Founder action is required.

No provider call is made by installer smoke.
