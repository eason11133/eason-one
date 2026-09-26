# Product Gate Failure — Project #20 Evidence Handoff

Date: 2026-08-28
Project: #20 Founder Ping Endpoint
Live engineering snapshot at failure: governance-r5-20260828

## Observed product behavior

Project #20 produced three failed Mission envelopes and six rejected CODE_CHANGE ArtifactVersions.
The repository implementation was rolled back after each rejection, leaving `/api/founder-ping` absent on disk.
The Project stopped with a Company-owned SYSTEM_RECOVERY gate rather than inventing Founder authority.
Restart preserved this state correctly.

## Durable evidence

All six code ArtifactVersions have PASSED HOST_ENGINEERING_VALIDATION records bound to the exact ArtifactVersion.
The Windows host verifier observed real isolated loopback HTTP 200 responses containing:

- service = eason-one
- status = ok
- version = 0.20.0
- transport = REAL_LOOPBACK_HTTP

The corresponding independent semantic reviews did not report a semantic contradiction. They returned UNPROVEN because their prompt saw only the earlier Codex/WSL result text saying real HTTP could not run and did not receive the later authoritative host VerificationRecord.

## Root cause

Evidence ordering was correct but evidence visibility was split-brain:

1. Codex truthfully returned that WSL could not perform the real HTTP check.
2. Eason One Windows host validation then performed and persisted the real HTTP proof successfully.
3. `review_work()` built reviewer context from only `ArtifactVersion.content_text`/location.
4. The independent reviewer therefore treated the earlier WSL limitation as current evidence state.
5. Reviewer returned REVISE/UNPROVEN.
6. Runtime rejected the Artifact and safely rolled back the code delta.
7. Project continuation repeated the same pattern until SYSTEM_RECOVERY stopped the loop.

## r6 repair

- Work review now receives compact, exact ArtifactVersion-bound PASSED host VerificationRecords.
- Host proof is bound by frozen acceptance-contract hash and Artifact content hash.
- Reviewer remains the semantic proof owner; host evidence is evidence, not a semantic auto-pass.
- Earlier producer statements about WSL verification limits cannot override later authoritative host observations for facts the host directly observed.
- A narrowly classified historical SYSTEM_RECOVERY caused by host-proof/reviewer visibility mismatch may auto-resume once under `HOST_PROOF_REVIEW_HANDOFF_V2`.
- The one-time override is durable and consumed once; repeated failure after the repaired protocol stops again instead of looping.

## Acceptance intent

After installing r6, Project #20 should resume from its durable blocked state without Founder approval. Because prior rejected code changes were correctly rolled back, one new bounded Engineer/Codex execution may be required to recreate the repository delta. The repaired reviewer must then consume the matching Windows host proof for that exact new ArtifactVersion instead of rejecting it because WSL could not perform the same verification.
