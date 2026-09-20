# Founder Product Gate Failure — 2026-08-27 23:41:26 +08:00

## Observed real Windows behavior

`GET /headquarters` returned HTTP 500 before a new Product-Gate Project could be created.

Trace root:

`project_company.home_snapshot()` → `_project_cards()` → `project_card()` → `project_contract.governing_terms(project)` → `PROJECT_CONTRACT_MISSING`.

`GET /headquarters/projects/19` still returned HTTP 200, proving the failure came from another Project included in the home aggregate rather than Project #19 itself.

## Root cause

The v0.20 rule "governed vNext Project Contract missing/corrupt => fail closed" was correct for authority/execution paths, but the same strict reader was used by aggregate Founder/read-only surfaces. One unrelated historical or integrity-broken Project could therefore crash the entire Headquarters.

## Shared-root repair

Added `project_contract.read_projection()` as the only fault-contained read-only Contract projection:

- valid Contract/ledger → validated governing terms;
- governed vNext missing/corrupt Contract → no legacy fallback, returns integrity error;
- historical compatibility Project → may use non-persisted legacy projection;
- authority/execution writers continue using strict `governing_terms()` / `effective_authority()` and still fail closed.

Updated read-only aggregate paths in:

- `project_company.py`
- `headquarters.py`
- `context.py` broad evidence retrieval
- `ceo.py` broad company summary

## Regression evidence

New ORM cases prove:

1. historical Project without Contract does not crash `/headquarters`;
2. governed Project missing Contract does not crash `/headquarters`, surfaces `Governance integrity blocked`, while strict `governing_terms()` still raises `PROJECT_CONTRACT_MISSING`;
3. read projection never falls back to mutable compatibility columns for governed missing Contract.

Current diagnostics after repair:

- Governance focused ORM: 32/32 PASS
- Governance structural audit: 60/60 PASS
- Combined current focused diagnostics: 57 PASS / 1 Windows-only SKIP

This is engineering evidence only. Windows `/headquarters` regression retest and the real Founder Product Gate remain required.

## Delivery follow-up — 2026-08-28 00:00

The r2 package itself contained the intended `project_company.py` read-only `read_projection(project)` fix, but the Windows traceback still executed the pre-r2 `governing_terms(project)` line. This establishes a deployment/runtime identity failure: package correctness alone was insufficient to prove that the live target/process was running the package bytes.

Corrective action in engineering snapshot `governance-r3-20260828`:

- installer computes a SHA-256 manifest of every application-owned package file immediately after copy and refuses to continue unless the target tree matches source byte-for-byte;
- any mismatch is a hard installer failure and triggers the existing rollback path;
- Headquarters startup prints `ENGINEERING_SNAPSHOT_ID` plus the SHA-256 of `eason_one/services/project_company.py` before launching Flask;
- Governance structural audit now treats both delivery-integrity properties as mandatory invariants.

This does not change the Product Gate result: the 23:41/00:00 Headquarters 500 remains a real failed Product Gate attempt and must not be reclassified as success.
