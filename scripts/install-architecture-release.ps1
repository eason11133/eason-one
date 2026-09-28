param(
  [Parameter(Mandatory=$true)][string]$SourceRoot,
  [string]$TargetRoot = "D:\school\eason-one"
)
$ErrorActionPreference = "Stop"
$SourceRoot = (Resolve-Path $SourceRoot).Path
$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot)
$python = Join-Path $TargetRoot ".python313\python.exe"
if (-not (Test-Path $python -PathType Leaf)) { $python = Join-Path $TargetRoot ".venv\Scripts\python.exe" }
if (-not (Test-Path $python -PathType Leaf)) { throw "Python 3.13 project runtime not found." }
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path (Join-Path (Split-Path $TargetRoot -Parent) ".eason-one-backups") "architecture-release-$stamp"
$dbPath = Join-Path $TargetRoot "instance\eason_one.db"
$ownedDirectories = @("eason_one", "docs", "scripts", "tests", ".github")
$ownedFiles = @("run.py", "pyproject.toml", "README.md", ".gitignore")

if (-not (Test-Path $dbPath -PathType Leaf)) { throw "Live database not found: $dbPath" }
Push-Location $SourceRoot
try {
  & $python -m compileall -q eason_one scripts run.py
  if ($LASTEXITCODE -ne 0) { throw "Source compilation failed" }
  & $python scripts\audit_v020_core.py
  if ($LASTEXITCODE -ne 0) { throw "Core audit failed" }
  & $python scripts\audit_v020_governance.py
  if ($LASTEXITCODE -ne 0) { throw "Governance audit failed" }
  & $python scripts\audit_provider_contracts.py
  if ($LASTEXITCODE -ne 0) { throw "Provider contract audit failed" }
  & $python scripts\audit_review_evidence_contracts.py
  if ($LASTEXITCODE -ne 0) { throw "Review/evidence audit failed" }
  & $python scripts\audit_architecture_release.py --database $dbPath
  if ($LASTEXITCODE -ne 0) { throw "Project #21 DB-copy digital twin failed" }
} finally { Pop-Location }

New-Item -ItemType Directory -Force -Path $backup | Out-Null
foreach ($name in $ownedDirectories) {
  $current = Join-Path $TargetRoot $name
  if (Test-Path $current) { Copy-Item -LiteralPath $current -Destination (Join-Path $backup $name) -Recurse -Force }
}
foreach ($name in $ownedFiles) {
  $current = Join-Path $TargetRoot $name
  if (Test-Path $current -PathType Leaf) { Copy-Item -LiteralPath $current -Destination (Join-Path $backup $name) -Force }
}
New-Item -ItemType Directory -Force -Path (Join-Path $backup "instance") | Out-Null
& $python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $dbPath (Join-Path $backup "instance\eason_one.db")
if ($LASTEXITCODE -ne 0) { throw "SQLite backup failed" }

try {
  foreach ($name in $ownedDirectories) {
    $source = Join-Path $SourceRoot $name; if (-not (Test-Path $source)) { continue }
    $current = Join-Path $TargetRoot $name
    if (Test-Path $current) { Remove-Item -LiteralPath $current -Recurse -Force }
    Copy-Item -LiteralPath $source -Destination $current -Recurse -Force
  }
  foreach ($name in $ownedFiles) {
    $source = Join-Path $SourceRoot $name
    if (Test-Path $source -PathType Leaf) { Copy-Item -LiteralPath $source -Destination (Join-Path $TargetRoot $name) -Force }
  }
  Push-Location $TargetRoot
  try {
    & $python scripts\migrate_v020.py instance\eason_one.db
    if ($LASTEXITCODE -ne 0) { throw "Database migration failed" }
    & $python scripts\migrate_architecture_release.py instance\eason_one.db
    if ($LASTEXITCODE -ne 0) { throw "Project #21 structured Codex scope migration failed" }
    & $python scripts\audit_architecture_release.py --database instance\eason_one.db
    if ($LASTEXITCODE -ne 0) { throw "Installed DB-copy digital twin failed" }
  } finally { Pop-Location }
  Set-Content -LiteralPath (Join-Path $TargetRoot ".architecture-release-backup-path.txt") -Value $backup -Encoding UTF8
  Write-Host "Eason One architecture/reliability release installed. No real provider call was made." -ForegroundColor Green
} catch {
  Write-Host "Install failed. Run scripts\rollback-architecture-release.ps1 -BackupRoot '$backup'." -ForegroundColor Red
  throw
}
