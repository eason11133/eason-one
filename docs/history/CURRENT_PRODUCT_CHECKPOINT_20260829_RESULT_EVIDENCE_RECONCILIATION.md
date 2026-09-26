# Eason One Product Checkpoint — Result Evidence Reconciliation

**Date:** 2026-08-29  
**Status:** WORKING SOURCE CHECKPOINT — DO NOT INSTALL until explicitly instructed `現在請更新`.

## Active milestone

**First Usable Multi-Employee Project Trial** remains the only release milestone.

This checkpoint does not add a side feature. It closes one current-path Company Truth defect at the Result Ready boundary.

## Root defect closed

A durable `PROJECT_RESULT` was previously bound to the current Project Contract and budget authority, but not to the exact accepted Artifact/source-lineage evidence basis that made the Result true. If reconciliation later changed accepted evidence without changing Founder terms, an older Result projection could remain visible as current until a later completion-time check.

Additionally, once stale Result proof was detected while the Project was in `REVIEW`, the runtime opened a generic reconciliation wait instead of automatically resuming the existing Company outcome path. That could leave an unattended Project permanently stopped immediately before Founder review.

## Current semantics

1. Project-level deterministic host proof is current only when the exact referenced Work remains authoritatively accepted and the exact referenced ArtifactVersion remains `ACCEPTED` with the same content hash.
2. `PROJECT_RESULT` now persists `project_outcome_basis_hash`, binding:
   - current governing Project Contract hash,
   - current budget authority hash,
   - accepted ArtifactVersion/content/source/input-lineage review basis,
   - current criterion outcomes,
   - current deterministic host-proof basis.
3. `close_result_ready()` recomputes Project truth at the closure boundary. A caller-supplied or stale `SATISFIED` evaluation is diagnostic only and cannot manufacture Result Ready.
4. `result_ready_proof()` re-evaluates current Project truth and accepts only a Result proof whose Contract, authority and outcome-basis hashes still match.
5. If a Project is `REVIEW` but its Result proof becomes stale because the evidence basis changed, Company Runtime automatically:
   - withdraws the stale Result Ready projection,
   - returns the Project to `ACTIVE`,
   - reuses the ordinary terminal-Mission outcome path,
   - re-runs deterministic proof / independent Project review as needed,
   - plans bounded continuation only when the current Founder Contract is still not satisfied.
6. The Founder is not asked to repair internal evidence freshness. Only inability to validate governing authority/runtime lineage remains a Company recovery block.

## Why this is Eason One-owned work

This is not a generic orchestration/retry problem. It defines what makes a Founder-visible Company Result *currently true*. The authority/evidence binding therefore remains an Eason One product invariant rather than being delegated to an external agent framework.

## Validation in this environment

- Python source compile: **168 files PASS**
- `scripts/audit_v020_core.py`: **PASS**
- `scripts/audit_v020_governance.py`: **PASS (99 invariants)**
- `node --check eason_one/static/headquarters.js`: **PASS**
- New structural invariants require current-truth recomputation, exact ArtifactVersion/content-hash host proof, evidence-bound Result proof, and automatic stale-Result reconciliation.

Full Flask / Flask-SQLAlchemy runtime pytest remains unavailable in this sandbox because those dependencies are not installed and external wheel download is blocked here. No runtime pytest pass is claimed.

## Release path after this checkpoint

Founder Approve
→ Company Runtime / Team Formation
→ parallel Persistent Employee Work
→ accepted Artifact/Evidence handoff
→ independent verification
→ Project outcome review
→ bounded continuation if needed
→ **evidence-current Result Ready**
→ Founder reads the verified result.

The next work remains release-first: look only for blockers that prevent this exact path from being used as the first real local Project trial. `BETTER` items stay backlog.
