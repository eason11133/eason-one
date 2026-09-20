param(
  [string]$TargetRoot = "D:\school\eason-one",
  [string]$BackupRoot = ""
)
$ErrorActionPreference = "Stop"
$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot)
if (-not $BackupRoot) {
  $pointer = Join-Path $TargetRoot ".architecture-release-backup-path.txt"
  if (Test-Path $pointer -PathType Leaf) { $BackupRoot = (Get-Content -LiteralPath $pointer -Raw).Trim() }
}
if (-not $BackupRoot -or -not (Test-Path $BackupRoot -PathType Container)) { throw "Architecture release rollback checkpoint not found." }
$ownedDirectories = @("eason_one", "docs", "scripts", "tests", ".github")
$ownedFiles = @("run.py", "pyproject.toml", "README.md", ".gitignore", "ARCHITECTURE_REVIEW.md", "BLOCKER_INVENTORY.md", "CONSOLIDATED_CHANGELOG.md", "FULL_E2E_ACCEPTANCE.md", "BACKLOG.md")
foreach ($name in $ownedDirectories) {
  $current = Join-Path $TargetRoot $name
  if (Test-Path $current) { Remove-Item -LiteralPath $current -Recurse -Force }
  $saved = Join-Path $BackupRoot $name
  if (Test-Path $saved) { Copy-Item -LiteralPath $saved -Destination $current -Recurse -Force }
}
foreach ($name in $ownedFiles) {
  $current = Join-Path $TargetRoot $name
  if (Test-Path $current) { Remove-Item -LiteralPath $current -Force }
  $saved = Join-Path $BackupRoot $name
  if (Test-Path $saved) { Copy-Item -LiteralPath $saved -Destination $current -Force }
}
$savedDb = Join-Path $BackupRoot "instance\eason_one.db"
if (Test-Path $savedDb -PathType Leaf) {
  $liveDb = Join-Path $TargetRoot "instance\eason_one.db"
  Remove-Item -LiteralPath "$liveDb-wal" -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath "$liveDb-shm" -Force -ErrorAction SilentlyContinue
  Copy-Item -LiteralPath $savedDb -Destination $liveDb -Force
}
Write-Host "Architecture/reliability release rollback complete. Secrets and Python runtimes were untouched." -ForegroundColor Green
