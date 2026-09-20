# Eason One — Founder Project Handoff Checkpoint

Date: 2026-08-29
Status: WORKING SOURCE CHECKPOINT — DO NOT INSTALL until ChatGPT explicitly says `現在請更新`.

## Product release-path position

This checkpoint continues from the cumulative Parallel Budget Reservation source.
The active milestone remains the **First Usable Multi-Employee Project Trial**:

Founder approves Project → Company Runtime takes ownership → Team Formation → bounded topology → first independent parallel Work wave → Artifact/Evidence handoff → verification/continuation → Project Outcome / Result Ready.

This checkpoint does **not** reopen Governance as the construction target.

## Meaningful chunk completed

### Founder approval now hands the UI to the Company-owned Project surface

Two current approval surfaces were still visually preserving the old Mission/CEO control loop after backend ownership had already moved to Company Runtime.

1. `POST /operations/<id>/approve`
   - v0.20 `WORK_CORE_V018` approval now redirects to `/headquarters/projects/<project_id>?live=1`.
   - legacy/non-v0.20 operations retain the Mission/audit destination.
   - approval copy now says Company Runtime started, not CEO runtime.

2. HQ CEO dialogue `APPROVE PROJECT`
   - a new-Project proposal now carries an explicit `createsProject` client marker.
   - after approval succeeds and the canonical Project href is returned, the Founder is automatically moved to the Project/company surface.
   - existing-Project / non-create approvals do not get forced through this navigation.

The Project page already subscribes to the single global runtime pulse. Once Company Work becomes live it projects `COMPANY NOW → WORKING`; when the bounded live step ends it reloads durable Project truth rather than asking the Founder to manually step the Mission.

## Why this is BLOCKING for the first usable trial

Without this handoff the backend could correctly start Team Formation / parallel Work while the Founder remained on the old proposal/Mission surface. The first real Project would therefore *behave* like the new Company Runtime but *look* like the old CEO-runner product.

This fix aligns Founder-facing navigation with the canonical product ownership boundary:

Founder approval → Project Company surface → Company Runtime / Persistent Employees.

## Verification completed in this environment

- Python source compile (168 `.py` files): PASS
- `scripts/audit_v020_core.py`: PASS
- `scripts/audit_v020_governance.py`: PASS (99 current-source invariants)
- `node --check eason_one/static/headquarters.js`: PASS
- Current Parallel-Budget cumulative payload was reconstructed and all 62 payload hashes matched its manifest before this change.

## Environment limitation

This Linux tool environment does not contain Flask / Flask-SQLAlchemy and cannot install them from the network, so ORM/Flask route acceptance is **not** claimed here. Windows/local product acceptance remains required before release/install approval.

## Traceability note (not a reopened construction target)

An older conversation checkpoint reported 117 Governance invariants, while the current source audit contains 99. Package-chain comparison across CODEX-AUTOSTART → ENGINEERING-READINESS → RESILIENT-TOPOLOGY → PARALLEL-BUDGET showed the Governance audit file hash is identical across those recent packages. Therefore this is not a regression introduced by the current Parallel-Budget or Founder-handoff work; it is retained as historical traceability debt rather than reopening Governance.

## Next release-path work

Continue the current first usable trial dry-run / source audit from the Project surface through:

1. automatic Team Formation after approval;
2. first bounded parallel Employee wave;
3. accepted upstream evidence handoff;
4. downstream continuation without CEO/Engineer collapse;
5. independent Project outcome review;
6. Result Ready projection and Founder-visible final result.

Only BLOCKING ROOT / INTEGRATION DEFECT findings on that path should interrupt progression. BETTER/cleanup remains backlog.
