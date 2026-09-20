# Eason One v0.20 — Current Core Handoff

Current mainline: **v0.20.0 Company Core Rebuild**.

Do not resume from older v0.12/v0.18 closure semantics. Read, in order:

1. `docs/V0.20-ENGINEERING-CONSTITUTION.md`
2. `docs/acceptance/v020_core_rebuild_matrix.md`
3. `V020_CORE_REBUILD_HANDOFF.md`
4. `eason_one/services/project_contract.py`
5. `eason_one/services/company_kernel.py`
6. `eason_one/services/project_outcome.py`
7. `eason_one/services/company_runtime.py`

Hard continuity rules:

- Founder Project Contract is highest Project authority.
- Project/Work/Execution/Artifact/Verification is the business truth spine.
- Mission completion/failure cannot determine Project completion/failure.
- CEO continuation may be autonomous only inside existing Founder budget/deadline/constraints and must use `PROJECT_DELEGATED_CEO`.
- Normal boot may not replay historical migrations.
- Legacy runtime Project closure/failure is not a v0.20 governing path.
- Founder UI is a projection of durable Company Truth and may not fabricate activity/approval/completion.
- Engineering diagnostics never substitute for the real Founder Product E2E gate.

Current release verification:

```powershell
.\scripts\verify-v020.ps1
```

Current runtime entry:

```powershell
.\scripts\run-headquarters.ps1
```
