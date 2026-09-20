param([Parameter(Mandatory=$true)][string]$TargetRoot)
$ErrorActionPreference = "Stop"
$marker = Join-Path $TargetRoot ".v0186-backup-path.txt"
if (-not (Test-Path $marker)) { throw "No v0.18.6 rollback checkpoint found." }
$backupRoot = (Get-Content -LiteralPath $marker -Raw).Trim()
if (-not (Test-Path $backupRoot)) { throw "Rollback backup not found: $backupRoot" }
$files = @(
  "eason_one\services\company_runtime.py",
  "instance\eason_one.db"
)
foreach ($rel in $files) {
  $src = Join-Path $backupRoot $rel
  $dst = Join-Path $TargetRoot $rel
  if (Test-Path $src) {
    New-Item -ItemType Directory -Force -Path (Split-Path $dst -Parent) | Out-Null
    Copy-Item $src $dst -Force
  }
}
Write-Host "Rolled back v0.18.6 from: $backupRoot"
