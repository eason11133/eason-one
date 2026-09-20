param(
  [Parameter(Mandatory=$true)][string]$SourceRoot,
  [string]$TargetRoot = "D:\school\eason-one"
)

$ErrorActionPreference = "Stop"
$SourceRoot = (Resolve-Path $SourceRoot).Path
$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot)
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupBase = Join-Path (Split-Path $TargetRoot -Parent) ".eason-one-backups"
$backupRoot = Join-Path $backupBase "v018-core-cutover-$stamp"
$hadTarget = Test-Path $TargetRoot
$ownedDirectories = @("eason_one", "docs", "scripts", "tests", ".github")
$ownedFiles = @(
  "run.py", "pyproject.toml", "README.md", ".gitignore",
  "INSTALL_V018_POWERSHELL.txt", "V018_CORE_CUTOVER_HANDOFF.md",
  "CODEX_HANDOFF_HEADQUARTERS.md"
)

function Restore-Backup {
  if (-not (Test-Path $backupRoot)) { return }
  Write-Host "Restoring pre-v0.18 code and database..." -ForegroundColor Yellow
  foreach ($name in $ownedDirectories) {
    $current = Join-Path $TargetRoot $name
    if (Test-Path $current) { Remove-Item -LiteralPath $current -Recurse -Force -ErrorAction SilentlyContinue }
    $saved = Join-Path $backupRoot $name
    if (Test-Path $saved) { Move-Item -LiteralPath $saved -Destination $current -Force }
  }
  foreach ($name in $ownedFiles) {
    $current = Join-Path $TargetRoot $name
    if (Test-Path $current) { Remove-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue }
    $saved = Join-Path $backupRoot $name
    if (Test-Path $saved) { Move-Item -LiteralPath $saved -Destination $current -Force }
  }
  $savedDb = Join-Path $backupRoot "instance\eason_one.db"
  if (Test-Path $savedDb) {
    New-Item -ItemType Directory -Force -Path (Join-Path $TargetRoot "instance") | Out-Null
    Copy-Item -LiteralPath $savedDb -Destination (Join-Path $TargetRoot "instance\eason_one.db") -Force
  }
}

Write-Host "Eason One v0.18.0 Core Cutover installer" -ForegroundColor Cyan
Write-Host "Source: $SourceRoot"
Write-Host "Target: $TargetRoot"

New-Item -ItemType Directory -Force -Path $TargetRoot | Out-Null
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null

try {
  $python = Join-Path $TargetRoot ".venv\Scripts\python.exe"
  if (-not (Test-Path $python -PathType Leaf)) {
    throw "Existing Eason One .venv was not found. This cutover installer intentionally does not create/download a new environment."
  }

  Write-Host "[1/5] Backing up current application code and live database..."
  if ($hadTarget) {
    foreach ($name in $ownedDirectories) {
      $current = Join-Path $TargetRoot $name
      if (Test-Path $current) { Move-Item -LiteralPath $current -Destination (Join-Path $backupRoot $name) }
    }
    foreach ($name in $ownedFiles) {
      $current = Join-Path $TargetRoot $name
      if (Test-Path $current) { Move-Item -LiteralPath $current -Destination (Join-Path $backupRoot $name) }
    }
    $dbPath = Join-Path $TargetRoot "instance\eason_one.db"
    if (Test-Path $dbPath -PathType Leaf) {
      New-Item -ItemType Directory -Force -Path (Join-Path $backupRoot "instance") | Out-Null
      Copy-Item -LiteralPath $dbPath -Destination (Join-Path $backupRoot "instance\eason_one.db") -Force
    }
  }

  Write-Host "[2/5] Installing v0.18 application-owned files..."
  foreach ($name in $ownedDirectories) {
    $source = Join-Path $SourceRoot $name
    if (Test-Path $source) { Copy-Item -LiteralPath $source -Destination (Join-Path $TargetRoot $name) -Recurse -Force }
  }
  foreach ($name in $ownedFiles) {
    $source = Join-Path $SourceRoot $name
    if (Test-Path $source -PathType Leaf) { Copy-Item -LiteralPath $source -Destination (Join-Path $TargetRoot $name) -Force }
  }

  Write-Host "[3/5] Running dependency-free Core Cutover audit..."
  Push-Location $TargetRoot
  try {
    & $python "scripts\audit_v018_core.py"
    if ($LASTEXITCODE -ne 0) { throw "v0.18 structural audit failed" }

    Write-Host "[4/5] Compiling installed Python source..."
    & $python -m compileall -q eason_one scripts run.py
    if ($LASTEXITCODE -ne 0) { throw "Python compilation failed" }

    Write-Host "[5/5] Applying schema + one-time safe Project cutover with runtimes disabled..."
    & $python "scripts\migrate_v018.py" "instance\eason_one.db"
    if ($LASTEXITCODE -ne 0) { throw "v0.18 database migration failed" }
  }
  finally { Pop-Location }

  Set-Content -LiteralPath (Join-Path $TargetRoot ".v018-backup-path.txt") -Value $backupRoot -Encoding UTF8
  Write-Host "Installed Eason One v0.18.0 Core Cutover." -ForegroundColor Green
  Write-Host "Rollback backup: $backupRoot"
  Write-Host "Next: .\scripts\run-headquarters.ps1"
}
catch {
  Write-Host "Installation failed: $($_.Exception.Message)" -ForegroundColor Red
  try { Restore-Backup } catch { Write-Host "Automatic rollback failed. Backup remains at: $backupRoot" -ForegroundColor Red }
  throw
}
