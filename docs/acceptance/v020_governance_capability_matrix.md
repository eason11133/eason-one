# Eason One v0.20 Governance Capability Matrix

Updated: 2026-08-27

This matrix is an engineering seal checklist for the Founder / Company Governance subsystem. It does **not** replace the real Windows Founder Product Gate.

| Capability | Canonical owner / durable truth | Main writers | Main readers / surfaces | Executable proof | Product proof |
| --- | --- | --- | --- | --- | --- |
| Founder-only authority classification | `services/governance.py` whitelist | canonical Governance gate writers | Command / HQ / Project | `test_v020_governance_floor.py` | Windows Project Gate pending |
| Exact gate identity / idempotency | Governance gate hash over type + exact options + scope | `governance.open_gate()` | Governance attention | ORM focused PASS | Windows pending |
| Single current Project gate | Governance supersession + Work wait reconciliation | Governance owner | Project / Work projections | ORM focused PASS | Windows pending |
| Frozen APPROVE payload integrity | frozen gate options + first-class `Decision` | `resolve_gate()` | Decision history / Project UI | ORM focused PASS | Windows pending |
| Founder MODIFY validation | Governance centralized type validation | `resolve_gate(action=MODIFY)` | Project UI | ORM focused PASS | Windows pending |
| Result Ready / terminal authority lock | Governance + Project Contract boundary | all Founder authority commits | Project surface | ORM focused PASS | Windows pending |
| Project budget authority | immutable Project Contract + append-only amendments | two budget HTTP writers via Governance | Work budget / Mission / Project projection | ORM + structural PASS | Windows pending |
| Deadline / scope / constraint amendment | Project Contract amendments | Governance resolver | Project / CEO context | structural + focused ORM | Windows pending |
| Codex exact-action Founder approval | durable execution authority receipt | Governance resolver | Codex Job Spec | focused ORM PASS | real Codex post-approval run pending |
| External-effect exact-action approval | durable execution authority receipt | Governance resolver | external effect execution | focused ORM PASS | real external-effect Project pending |
| Pre/post approval execution separation | AgentRun provenance + receipt | Work/Codex execution | execution runtime | focused ORM PASS | Windows pending |
| Generic authority failure stays Company-owned | reconciliation / Work wait | Work/Kernel recovery | Command / HQ | structural + ORM PASS | Windows pending |
| Initial Project proposal authority | `proposal_authority.py` classifier | initial Project proposal | Command / HQ | focused ORM PASS | Windows pending |
| Generic Proposal Inbox separation | Proposal truth, not Founder authority | proposal writers | Command / HQ | focused ORM PASS | Windows pending |
| Manual Pause / Resume | `FOUNDER_PAUSE` control wait, not Governance | operation control routes | Mission / Project UI | current acceptance PASS | Windows pending |
| Mission Stop vs Project Cancel | Operation control vs Project Governance | Mission stop / Project cancel route | Project / Mission surfaces | focused ORM PASS | Windows pending |
| Project Cancel | canonical Governance + Project status + Work cancellation | Project cancel route | Project / history | focused ORM PASS | Windows pending |
| Meeting Founder conversational input | `meeting_coordination.py` classifier | router explicit Founder question only | Command / Meeting UI | focused ORM PASS | Windows pending |
| Meeting provider/synthesis failure recovery | Meeting state machine `PAUSED` + bounded retry | Meeting kernel | Meeting / Command | focused ORM PASS | Windows pending |
| Delegated hiring | Project Contract authority + Company Decision/Event | workforce / HR review | People / history | focused ORM PASS | Windows multi-employee Project pending |
| Delegated hiring grants no budget authority | `new_budget_authority_twd = 0` | workforce | Decision/Event audit | focused ORM PASS | Windows pending |
| Founder Result acceptance | verified Project Outcome + first-class Decision | Project result route | Project / history | structural + current acceptance | clean current-constitution E2E still pending |
| Historical Mission outcome preservation | Mission lifecycle history | Project outcome closure | Mission / Project history | focused ORM PASS | Windows pending |
| v0.20 explicit migration | `scripts/migrate_v020.py` first freeze only | migration script | Project Contract readers | migration acceptance PASS | installer-on-live-copy pending |
| Legacy runtime exclusion | current v0.20 owners | Company Kernel / recovery | normal tick | structural audit PASS | Windows runtime pending |
| Founder Project Governance action surface | canonical Project POST route → Governance resolver | `routes.py` | `hq_project.html` | source/route present; focused suite PASS | click-through Windows Product Gate pending |
| Founder read-surface Contract fault containment | `project_contract.read_projection()`; strict authority readers unchanged | read-only Founder/organizational projections | HQ Project cards / Mission snapshot / CEO broad context | governed-missing + historical-missing ORM cases PASS | Windows `/headquarters` regression retest pending |
| Installer Governance preflight | `install-v020.ps1` | installer | install output | source hooks present | real PowerShell install simulation pending |
| Rollback safety | consistent SQLite backup + owned-file restore | installer / rollback | operator | script review | real Windows rollback simulation pending |

## Current seal state

- Core structural audit: PASS.
- Governance structural audit: 60/60 PASS.
- Governance focused ORM: 32/32 PASS.
- Combined v0.18 + v0.20 core + Governance + migration diagnostics: 57 PASS / 1 SKIP in the reconstruction environment. The skip is the Windows-host real-loopback product check and is intentionally not faked.
- Previous reconstructed candidate: installed and independently verified on Windows on 2026-08-27 (55 focused diagnostics PASS; runtime-off health smoke 200).
- First real Windows Founder Product Gate attempt: **FAILED before Project creation** because `/headquarters` crashed on an unrelated Project with `PROJECT_CONTRACT_MISSING`. This exposed read-surface fault-containment as a real product blocker.
- Current working copy contains the shared-root read-projection repair and regression acceptance, but that repair still needs Windows reinstall/retest.
- Governance subsystem status: **engineering working candidate after a real Product-Gate failure; not Product-Gate sealed**.
