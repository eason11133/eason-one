# Eason One v0.20 r7 — Deterministic Project Closure + Founder Attention Truth

Date: 2026-08-25

## Root causes fixed

1. `current_company.unresolved_governance()` treated every pending `Proposal` as Founder authority. Engineer/Codex knowledge/evidence proposals therefore appeared as duplicate `Project decision` cards even though they were not governance gates.
2. Project #19 already had accepted Engineer code, persisted Codex test/diff evidence, real loopback HTTP proof, and recovery history. The Project outcome layer did not compile those existing records into deterministic Project proof, so it tried to buy another Mission merely to restate delivery evidence.
3. `close_result_ready()` rewrote failed Mission statuses to `COMPLETED`, destroying recovery history, and did not retire now-obsolete open Founder escalations.

## r7 behavior

- Only `PROJECT_PLAN` proposals can enter the legacy Proposal Founder-governance read model. Knowledge/evidence proposals remain audit/memory candidates and never block the Founder.
- Project outcome can deterministically prove explicit criteria for:
  - JSON `service = eason-one` from persisted real-loopback HTTP response evidence.
  - response `version` matching `[project].version` in `pyproject.toml`.
  - an Engineer delivery evidence bundle from the already accepted Engineer execution: Codex changed files, tests, summary, real HTTP observations, Work verification, and recovery history.
- No new AgentRun, provider call, Codex run, or Founder budget expansion is needed to compile these facts.
- Result Ready resolves obsolete open Founder escalations.
- Previously FAILED Missions remain FAILED in the audit trail when the Project reaches Result Ready.

## Guarded Project #19 reconciliation

The r7 installer is intentionally strict. After patching and focused diagnostics, it runs deterministic reconciliation for Project #19.

Installation succeeds only if all of the following are true:

- Project #19 becomes `REVIEW` / Result Ready from existing evidence.
- zero new AgentRuns are created by reconciliation.
- Founder Project budget authority is unchanged.
- failed Mission history is preserved.
- no open Founder escalation remains for the now-satisfied Project.
- a Project Result Artifact/Verification is persisted.

If Project #19 still has insufficient evidence, or any invariant above fails, the installer exits non-zero and restores both code and SQLite from its checkpoint. It does not leave another half-installed hotfix.
