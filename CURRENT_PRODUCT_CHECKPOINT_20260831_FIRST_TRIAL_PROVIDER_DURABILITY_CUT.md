# Eason One — First Trial Provider & Durability Cut

Status: CONSOLIDATED RELEASE CUT (built from installed V7 source)
Milestone: First Usable Multi-Employee Project Trial

This cut intentionally replaces the one-bug/one-hotfix rhythm with one provider/runtime acceptance pass across the current Project path.

## What is consolidated

- One provider protocol contract now normalizes HTTP status, Retry-After, and output-limit stop reasons without mixing Company semantics into SDK adapters.
- OpenAI and Anthropic continue with SDK retries disabled (`max_retries=0`); Eason One owns durable bounded retry state.
- HTTP 400/401/403/404/422 are definitive request/configuration rejections and become Company SYSTEM_RECOVERY rather than guessed replay.
- HTTP 408/409/429 and >=500 are definitive transient HTTP failures: no accepted completion exists, the reservation is released, Retry-After/backoff is persisted, and bounded replay is safe.
- Network/timeout failures without a trustworthy HTTP response remain post-dispatch ambiguous and are never replayed by guesswork.
- Provider-specific truncation reasons (`length`, `MAX_TOKENS`, `max_tokens`, etc.) normalize to `OUTPUT_TRUNCATED`; retry stays on the same provider with a larger bounded output cap before fallback.
- Live Research starts with up to 2400 output tokens (bounded by ModelConfig; current Sonar is 2000) rather than repeatedly starting at 1600.
- Gemini HTTP errors now preserve status/headers for the same durable classification when Gemini is configured later.
- Project outcome review, Project continuation planning, and HR assessment distinguish permanent provider configuration rejection from retryable transient failures.

## Endpoint-specific provider contracts checked

- OpenAI: Responses structured output + one hosted Web Search call with TW / Asia-Taipei locality.
- Anthropic: `output_config.format` JSON Schema and current SDK error/retry semantics.
- Perplexity: current **Sonar Chat Completions** contract uses `response_format.type=json_schema` with the schema object. The separate Agent API named-schema requirement is not mixed into this endpoint.
- Gemini: current generateContent structured JSON shape is retained; status-preserving HTTP errors are added for future routing.

## Offline acceptance

- Python source compile PASS.
- v0.20 core audit PASS.
- Governance structural audit PASS (99 invariants).
- Headquarters JavaScript syntax PASS.
- `scripts/audit_provider_contracts.py` PASS with fake transports and zero provider calls.
- Installer additionally runs `scripts/audit_first_trial_release_cut.py` against a SQLite safety copy of the current local database, with Company Runtime autostart disabled and zero provider calls.

## Current Project #21 expectation

The installer accepts either side of the installed-V7 recovery boundary. If the historical Perplexity `json_object` HTTP-400 reconciliation still exists on the DB copy, current deterministic recovery must remove it without creating a new AgentRun. Research must still resolve to `perplexity/sonar`; Product Strategy staffing must no longer retain the historical `HIRING_REQUEST_3_NOT_MATERIALIZED` contradiction; Critic remains the Claude path.

The live Project is not declared successful by this cut. Product proof still requires the real approved Project to continue through Research Artifact -> downstream Product/Critic/Engineer handoff -> independent Project outcome review -> Result Ready.
