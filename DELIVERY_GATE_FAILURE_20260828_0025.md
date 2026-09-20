# Delivery Gate Failure — 2026-08-28 00:25 +08:00

## Observed real Windows behavior

The r3 package itself was correct, but `scripts/install-v020.ps1` failed at `[3b/7]` while building the source/target SHA-256 manifest:

`[System.IO.Path]` did not contain `GetRelativePath`.

The user's environment is Windows PowerShell / .NET Framework where `System.IO.Path.GetRelativePath()` is unavailable. The installer therefore copied candidate files, failed before byte-for-byte verification, entered rollback, and restored the prior r1 application tree.

Because the bootstrap commands had been pasted interactively, later top-level commands continued after the installer exception. That produced misleading green text and ran verification/startup against the restored r1 tree. This was not a successful r3 deployment.

## Corrective action in engineering snapshot `governance-r4-20260828`

1. Replaced `System.IO.Path.GetRelativePath()` with a PowerShell-5.1-compatible `Get-RelativePathCompat` implementation based on normalized full paths and substring containment.
2. Governance structural audit now forbids `GetRelativePath(` in the installer and requires the compatibility helper.
3. Installer continues to fail closed and rollback if package/target byte manifests differ.
4. The user-facing bootstrap must execute as one script block with `$ErrorActionPreference = "Stop"`, so any installer exception terminates the whole bootstrap and no success/startup commands can run afterward.
5. Engineering snapshot identity is bumped to `governance-r4-20260828`.

## Product-gate status

OPEN. The `/headquarters` Contract read-surface repair still has not been exercised on the live Windows tree because r2/r3 were never successfully deployed.
