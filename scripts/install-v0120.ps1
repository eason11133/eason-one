param(
  [Parameter(Mandatory=$true)][string]$SourceRoot,
  [string]$TargetRoot = "D:\school\eason-one"
)

$ErrorActionPreference = "Stop"
$SourceRoot = (Resolve-Path $SourceRoot).Path
$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot)
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupBase = Join-Path (Split-Path $TargetRoot -Parent) ".eason-one-backups"
$backupRoot = Join-Path $backupBase "v0120-hotfix2-$stamp"
$hadExistingTarget = Test-Path $TargetRoot

$ownedDirectories = @("eason_one", "docs", "scripts", "tests", ".github")
$ownedFiles = @(
  "run.py",
  "pyproject.toml",
  "README.md",
  ".gitignore",
  "INSTALL_V0120_POWERSHELL.txt"
)

function Copy-RequiredFile {
  param([string]$Source, [string]$Destination)
  if (-not (Test-Path $Source -PathType Leaf)) {
    throw "Required source file is missing: $Source"
  }
  $parent = Split-Path $Destination -Parent
  if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
  Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Restore-PreviousCode {
  Write-Host "Restoring the previous application files..." -ForegroundColor Yellow

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
    $targetInstance = Join-Path $TargetRoot "instance"
    New-Item -ItemType Directory -Force -Path $targetInstance | Out-Null
    Copy-Item -LiteralPath $savedDb -Destination (Join-Path $targetInstance "eason_one.db") -Force
  }
}

Write-Host "Eason One V0.12.0 hotfix2 installer" -ForegroundColor Cyan
Write-Host "Source: $SourceRoot"
Write-Host "Target: $TargetRoot"

New-Item -ItemType Directory -Force -Path $TargetRoot | Out-Null
New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null

try {
  if ($hadExistingTarget) {
    Write-Host "[1/6] Saving current code by same-drive move..."

    # Preserve the live environment and data in place. Only application-owned
    # code is moved to the backup. This avoids recursively reading cache folders
    # and therefore avoids the robocopy exit-code-9 failure.
    foreach ($name in $ownedDirectories) {
      $current = Join-Path $TargetRoot $name
      if (Test-Path $current) {
        Move-Item -LiteralPath $current -Destination (Join-Path $backupRoot $name)
      }
    }

    foreach ($name in $ownedFiles) {
      $current = Join-Path $TargetRoot $name
      if (Test-Path $current) {
        Move-Item -LiteralPath $current -Destination (Join-Path $backupRoot $name)
      }
    }

    # Back up the only live database that the migration will modify. The
    # installer deliberately does not replace the user's instance directory
    # with the database snapshot included in the package.
    $currentDb = Join-Path $TargetRoot "instance\eason_one.db"
    if (Test-Path $currentDb -PathType Leaf) {
      $dbBackupDir = Join-Path $backupRoot "instance"
      New-Item -ItemType Directory -Force -Path $dbBackupDir | Out-Null
      Copy-Item -LiteralPath $currentDb -Destination (Join-Path $dbBackupDir "eason_one.db") -Force
    }

    $currentEnv = Join-Path $TargetRoot ".env"
    if (Test-Path $currentEnv -PathType Leaf) {
      Copy-Item -LiteralPath $currentEnv -Destination (Join-Path $backupRoot ".env") -Force
    }
  }

  Write-Host "[2/6] Installing rebuilt application files..."
  foreach ($name in $ownedDirectories) {
    $source = Join-Path $SourceRoot $name
    if (Test-Path $source) {
      Copy-Item -LiteralPath $source -Destination (Join-Path $TargetRoot $name) -Recurse -Force
    }
  }
  foreach ($name in $ownedFiles) {
    $source = Join-Path $SourceRoot $name
    if (Test-Path $source -PathType Leaf) {
      Copy-Item -LiteralPath $source -Destination (Join-Path $TargetRoot $name) -Force
    }
  }

  Write-Host "[3/6] Preserving or initializing runtime data..."
  $targetInstance = Join-Path $TargetRoot "instance"
  New-Item -ItemType Directory -Force -Path $targetInstance | Out-Null
  $targetDb = Join-Path $targetInstance "eason_one.db"
  if (-not (Test-Path $targetDb -PathType Leaf)) {
    Copy-RequiredFile -Source (Join-Path $SourceRoot "instance\eason_one.db") -Destination $targetDb
  }

  # Non-database package defaults are added only when absent. Existing local
  # runtime files are never overwritten.
  $sourceInstance = Join-Path $SourceRoot "instance"
  if (Test-Path $sourceInstance) {
    Get-ChildItem -LiteralPath $sourceInstance -File | Where-Object { $_.Extension -ne ".db" } | ForEach-Object {
      $destination = Join-Path $targetInstance $_.Name
      if (-not (Test-Path $destination)) {
        Copy-Item -LiteralPath $_.FullName -Destination $destination
      }
    }
  }

  Write-Host "[4/6] Checking Python environment..."
  $python = Join-Path $TargetRoot ".venv\Scripts\python.exe"
  if (-not (Test-Path $python -PathType Leaf)) {
    python -m venv (Join-Path $TargetRoot ".venv")
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed with exit code $LASTEXITCODE" }
  }
  $python = Join-Path $TargetRoot ".venv\Scripts\python.exe"

  Push-Location $TargetRoot
  try {
    Write-Host "[5/6] Installing dependencies and migrating the preserved database..."
    & $python -m pip install -e ".[test]"
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed with exit code $LASTEXITCODE" }

    & $python "scripts\migrate_v0120.py" "instance\eason_one.db" --no-backup
    if ($LASTEXITCODE -ne 0) { throw "Database migration failed with exit code $LASTEXITCODE" }

    Write-Host "[6/6] Compiling and verifying package structure..."
    & $python -m compileall -q eason_one scripts tests run.py
    if ($LASTEXITCODE -ne 0) { throw "Python compilation failed with exit code $LASTEXITCODE" }
  }
  finally {
    Pop-Location
  }

  Write-Host "Installed Eason One V0.12.0 hotfix2." -ForegroundColor Green
  Write-Host "Previous code backup: $backupRoot"
  Write-Host "Current database preserved and migrated: $targetDb"
  Write-Host "Run tests: .\scripts\verify-v0120.ps1"
  Write-Host "Start HQ:  .\scripts\run-headquarters.ps1"
}
catch {
  Write-Host "Installation failed: $($_.Exception.Message)" -ForegroundColor Red
  if ($hadExistingTarget) {
    try {
      Restore-PreviousCode
      Write-Host "Previous application files and database were restored." -ForegroundColor Yellow
    }
    catch {
      Write-Host "Automatic rollback also failed: $($_.Exception.Message)" -ForegroundColor Red
      Write-Host "Backup remains at: $backupRoot" -ForegroundColor Red
    }
  }
  throw
}
