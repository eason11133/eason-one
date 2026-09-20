# Eason One Current Governance Working State — 2026-08-27

This file is the current source-state handoff for the reconstructed v0.20 Founder / Company Governance work. It supersedes older progress summaries only for this working copy.

## What happened

The Windows ZIP supplied on 2026-08-27 was older than the final Governance work reported in the previous chat. The missing sandbox-only work was reconstructed against the actual source, then audited as a subsystem rather than replayed as serial hotfixes.

## Material source changes versus the supplied baseline

Core changed/added production files include:

- `eason_one/services/governance.py` (new canonical Founder Governance owner)
- `eason_one/services/project_contract.py`
- `eason_one/services/project_outcome.py`
- `eason_one/services/project_company.py`
- `eason_one/services/proposal_authority.py` (new)
- `eason_one/services/work_budget.py`
- `eason_one/services/work_execution.py`
- `eason_one/services/codex_connector.py`
- `eason_one/services/company_kernel.py`
- `eason_one/services/runtime_recovery.py`
- `eason_one/services/operations.py`
- `eason_one/services/operation_kernel.py`
- `eason_one/services/command.py`
- `eason_one/services/current_company.py`
- `eason_one/services/headquarters.py`
- `eason_one/services/founder_decisions.py`
- `eason_one/services/escalations.py`
- `eason_one/services/workforce.py`
- `eason_one/services/meeting_coordination.py` (new)
- `eason_one/services/meeting_kernel.py`
- `eason_one/services/meetings.py`
- `eason_one/routes.py`
- `eason_one/templates/hq_project.html`
- `eason_one/templates/meeting.html`
- `eason_one/templates/hq_meeting.html`

Engineering gates added/changed:

- `tests/test_v020_governance_floor.py`
- `scripts/audit_v020_governance.py`
- `scripts/install-v020.ps1`
- `scripts/verify-v020.ps1`
- v0.18/v0.20/migration acceptance updates

## Governance semantics now implemented in this working copy

1. Founder-only authority is an explicit whitelist; generic authority labels cannot mint Founder gates.
2. Governed Projects use a validated immutable Project Contract plus append-only amendments as governing authority truth.
3. Missing/corrupt vNext Contract truth fails closed; historical compatibility is not a normal runtime fallback.
4. Budget Founder gates require exact positive Project-scoped amounts.
5. APPROVE consumes the exact frozen payload. Caller-provided override values are rejected. MODIFY is separately validated and audited.
6. Current Project state is revalidated at commit time, preventing stale gates from mutating Result Ready/terminal Projects.
7. Codex/external execution approval is a durable exact-action receipt bound to Work, Contract hash and Project authority hash; pre-approval runs are not reused post-approval.
8. Manual Pause/Resume, Mission Stop and whole-Project Cancel have separate semantics.
9. Result Ready cannot rewrite historical Mission FAILED/ABANDONED evidence.
10. Delegated hiring can materialize a persistent Employee inside valid Project authority but grants zero new budget authority and records Company Decision/Event provenance.
11. Meeting synthesis/provider/validation failures remain Company recovery; only an explicit router question creates Founder conversational input.
12. Initial Project proposal authority is separated from generic pending Evidence/Knowledge proposals.
13. Founder Project UI now exposes canonical exact APPROVE / REJECT and type-limited MODIFY controls that POST to the Governance resolver.
14. Installer/verify scripts now require both Core and Governance structural audits plus focused diagnostics.
15. Founder/read-only Contract projections are fault-contained: one historical or integrity-broken Project cannot crash Headquarters; governed vNext execution remains strict fail-closed with no legacy fallback.

## Latest diagnostics and real Windows evidence

- Python compile of changed Governance path: PASS.
- v0.20 Core structural audit: PASS.
- v0.20 Governance structural audit: **64/64 PASS** (r4 delivery contract).
- Governance focused ORM: **32/32 PASS**.
- Combined focused diagnostics (`v018_core_cutover`, `v020_core_rebuild`, `v020_governance_floor`, `v020_migration`): **57 PASS / 1 Windows-only SKIP** in the reconstruction environment.
- Previous candidate installed successfully on the real Windows tree on 2026-08-27; installer and independent verify each completed 55 focused diagnostics, migration, structural audits, and runtime-off health smoke.
- First real Founder Product Gate attempt then exposed a true product blocker: aggregate `GET /headquarters` returned HTTP 500 because one Project without valid Contract truth caused the strict `governing_terms()` read to raise `PROJECT_CONTRACT_MISSING`.
- The current working copy contains the shared-root read-surface fault-containment repair. See `PRODUCT_GATE_FAILURE_20260827_234126.md`.

## What is NOT proven yet

This working copy has **not** passed the final real Windows Founder Product Gate. The previous candidate did install/verify successfully on Windows, but the first product attempt failed on the Headquarters read-surface bug described above. The current repair still requires Windows reinstall/retest. Remaining host/product acceptance work includes:

- reinstall this current repaired working copy and confirm `/headquarters` no longer 500s on the live database;
- real provider/Codex Project through canonical Founder Governance UI;
- Project reaches truthful Result Ready and Founder acceptance without manual DB repair/reconciliation;
- restart still preserves exact authority/receipt/outcome semantics.

Therefore this ZIP is a **current engineering source snapshot**, not a declaration that Eason One v0.20 Governance is product-sealed.

## 2026-08-28 delivery-integrity addendum

Engineering snapshot: `governance-r4-20260828`.

After the first read-only Contract fault-containment repair, Windows still produced the exact pre-r2 traceback even though the r2 ZIP contained the corrected source. The delivery mechanism therefore became part of the Governance release boundary. The installer now verifies the complete application-owned target tree against the package source by SHA-256 before compilation/migration, and Headquarters startup prints both the engineering snapshot ID and the critical `project_company.py` source fingerprint. A package may no longer claim successful installation if its target bytes differ from the package.

The actual Founder Product Gate remains OPEN until Windows Headquarters can load against the live DB and a new real Founder Project reaches truthful Result Ready under the current Constitution.


## 2026-08-28 r4 delivery compatibility correction

The first r3 forced-install attempt proved the live application tree was still r1. The r3 package hash and packaged `project_company.py` hash were correct, but the installer failed before manifest verification because Windows PowerShell/.NET Framework does not expose `[System.IO.Path]::GetRelativePath()`. Automatic rollback restored r1. The interactive bootstrap then continued executing later commands, which created misleading success output while verifying and starting the restored r1 tree.

Snapshot `governance-r4-20260828` replaces that API with `Get-RelativePathCompat`, adds a structural invariant that forbids `GetRelativePath(` in the installer, and requires installation to be invoked from one fail-stop script block. See `DELIVERY_GATE_FAILURE_20260828_0025.md`.

## 2026-08-28 Founder Product Gate — CEO intent routing correction (r5)

Windows r4 reached the Headquarters surface, but the exact Founder request to add `/api/founder-ping` with a Project budget was misrouted as a read-only budget/status brief. The router recognized `預算` but not the Chinese work verb `新增`, so no governed Project proposal was created.

r5 corrects the shared Founder-to-CEO work-intent classifier and operation kernel so Chinese `新增` / `加入` / `加上` requests remain governed work even when the same request includes budget/status vocabulary. The exact Founder request is now a focused Governance acceptance requiring `ACT / AUTO_DELEGATION`.
