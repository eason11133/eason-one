# Eason One v0.20 r4 — Founder Authority Truth Hotfix

Observed live on Project #18 after r3 restart/recovery.

This hotfix fixes three truth-boundary defects without changing the Founder Project objective or success criteria:

1. **Historical explicit Founder budget restoration**
   - v0.20 migration could freeze a derived execution envelope (for example NT$1.0183) even when the original Project-creating approved Operation retained an explicit Founder hard cap (for example NT$20).
   - r4 accepts only durable, approved initial-Project Operation memory containing both `new_project_spec` and `founder_declared_budget_cap_twd`.
   - CEO/model estimates and inferred numbers are never accepted as Founder authority.
   - A fresh migration restores the proven cap before Contract freeze.
   - An already-frozen Project keeps its immutable base Contract and receives an append-only reconciliation amendment up to the previously approved Founder cap.
   - A stale budget escalation is auto-resolved only when its exact old authorized cap/additional amount are mathematically covered by the restored cap.

2. **Founder budget read-model consistency**
   - CEO planning cost remains Company cost and does not consume the Project execution authority envelope by design.
   - The Project page now computes `execution authority left` using the same `project_remaining_authority()` definition as runtime budget enforcement instead of subtracting all Project cost events.

3. **Durable governance state wins over transient runtime pulse**
   - Project pages no longer let a transient runtime-focus event overwrite durable `NEEDS_YOU`, `RESULT_READY`, terminal, or BLOCKED truth with a stale `WORKING` state/action.

The hotfix installer stops Headquarters, creates a consistent SQLite backup, applies guarded file replacements, runs the authority reconciliation with runtimes disabled, compiles/audits the source, and runs focused v0.20 diagnostics using the project-local diagnostics temp directory.
