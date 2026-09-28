param(
  [Parameter(Mandatory=$true)][string]$SourceRoot,
  [string]$TargetRoot = "D:\school\eason-one"
)
$ErrorActionPreference = "Stop"
$SourceRoot = (Resolve-Path $SourceRoot).Path
$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot)
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupBase = Join-Path (Split-Path $TargetRoot -Parent) ".eason-one-backups"
$backupRoot = Join-Path $backupBase "v020-core-rebuild-$stamp"
$python = Join-Path $TargetRoot ".venv\Scripts\python.exe"
$dbPath = Join-Path $TargetRoot "instance\eason_one.db"
$ownedDirectories = @("eason_one", "docs", "scripts", "tests", ".github")
$ownedFiles = @(
  "run.py", "pyproject.toml", "README.md", ".gitignore",
  "INSTALL_V020_POWERSHELL.txt"
)



function Get-RelativePathCompat {
  param(
    [Parameter(Mandatory=$true)][string]$Root,
    [Parameter(Mandatory=$true)][string]$FullName
  )
  $rootFull = [System.IO.Path]::GetFullPath($Root).TrimEnd([char[]]@('\','/'))
  $fileFull = [System.IO.Path]::GetFullPath($FullName)
  $comparison = [System.StringComparison]::OrdinalIgnoreCase
  if (-not $fileFull.StartsWith($rootFull, $comparison)) {
    throw "Manifest file is outside root. Root=$rootFull File=$fileFull"
  }
  $rel = $fileFull.Substring($rootFull.Length).TrimStart([char[]]@('\','/'))
  return $rel.Replace('\','/')
}

function Get-ApplicationManifest {
  param([Parameter(Mandatory=$true)][string]$Root)
  $entries = New-Object System.Collections.Generic.List[object]
  foreach ($name in $ownedDirectories) {
    $base = Join-Path $Root $name
    if (-not (Test-Path $base -PathType Container)) { continue }
    Get-ChildItem -LiteralPath $base -File -Recurse |
      Where-Object { $_.FullName -notmatch "[\\/]__pycache__[\\/]" -and $_.Extension -ne ".pyc" } |
      ForEach-Object {
        $rel = Get-RelativePathCompat -Root $Root -FullName $_.FullName
        $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        $entries.Add([pscustomobject]@{ Path = $rel; Hash = $hash })
      }
  }
  foreach ($name in $ownedFiles) {
    $file = Join-Path $Root $name
    if (Test-Path $file -PathType Leaf) {
      $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
      $entries.Add([pscustomobject]@{ Path = $name.Replace("\\", "/"); Hash = $hash })
    }
  }
  return @($entries | Sort-Object Path)
}

function Assert-InstalledTreeMatchesSource {
  $sourceManifest = @(Get-ApplicationManifest -Root $SourceRoot)
  $targetManifest = @(Get-ApplicationManifest -Root $TargetRoot)
  $sourceMap = @{}
  $targetMap = @{}
  foreach ($row in $sourceManifest) { $sourceMap[$row.Path] = $row.Hash }
  foreach ($row in $targetManifest) { $targetMap[$row.Path] = $row.Hash }

  $problems = New-Object System.Collections.Generic.List[string]
  foreach ($path in $sourceMap.Keys) {
    if (-not $targetMap.ContainsKey($path)) {
      $problems.Add("MISSING target file: $path")
    } elseif ($sourceMap[$path] -ne $targetMap[$path]) {
      $problems.Add("HASH mismatch: $path")
    }
  }
  foreach ($path in $targetMap.Keys) {
    if (-not $sourceMap.ContainsKey($path)) {
      $problems.Add("EXTRA target file: $path")
    }
  }
  if ($problems.Count -gt 0) {
    $preview = ($problems | Select-Object -First 20) -join [Environment]::NewLine
    throw "Installed application tree does not match package source. Refusing to continue. Problems=$($problems.Count)`n$preview"
  }
  Write-Host "Installed application tree matches package source ($($sourceManifest.Count) files)." -ForegroundColor Green
}

function Restore-Backup {
  if (-not (Test-Path $backupRoot -PathType Container)) { return }
  Write-Host "Restoring pre-v0.20 code and database..." -ForegroundColor Yellow
  foreach ($name in $ownedDirectories) {
    $current = Join-Path $TargetRoot $name
    if (Test-Path $current) { Remove-Item -LiteralPath $current -Recurse -Force -ErrorAction SilentlyContinue }
    $saved = Join-Path $backupRoot $name
    if (Test-Path $saved) { Copy-Item -LiteralPath $saved -Destination $current -Recurse -Force }
  }
  foreach ($name in $ownedFiles) {
    $current = Join-Path $TargetRoot $name
    if (Test-Path $current) { Remove-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue }
    $saved = Join-Path $backupRoot $name
    if (Test-Path $saved) { Copy-Item -LiteralPath $saved -Destination $current -Force }
  }
  $savedDb = Join-Path $backupRoot "instance\eason_one.db"
  if (Test-Path $savedDb -PathType Leaf) {
    New-Item -ItemType Directory -Force -Path (Join-Path $TargetRoot "instance") | Out-Null
    Remove-Item -LiteralPath "$dbPath-wal" -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath "$dbPath-shm" -Force -ErrorAction SilentlyContinue
    Copy-Item -LiteralPath $savedDb -Destination $dbPath -Force
  }
}

Write-Host "Eason One v0.20.0 Company Core Rebuild installer" -ForegroundColor Cyan
Write-Host "Source: $SourceRoot"
Write-Host "Target: $TargetRoot"
Write-Host "PowerShell: $($PSVersionTable.PSVersion)"

if (-not (Test-Path $python -PathType Leaf)) {
  throw "Existing Eason One .venv not found: $python. This installer intentionally does not create or replace the environment."
}
if (-not (Test-Path $dbPath -PathType Leaf)) {
  throw "Live Eason One database not found: $dbPath"
}
if (-not (Test-Path (Join-Path $SourceRoot "scripts\audit_v020_core.py") -PathType Leaf)) {
  throw "SourceRoot is not the v0.20 release root: $SourceRoot"
}

New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
try {
  Write-Host "[1/7] Source structural preflight..."
  Push-Location $SourceRoot
  try {
    & $python "scripts\audit_v020_core.py"
    if ($LASTEXITCODE -ne 0) { throw "Source v0.20 core structural audit failed" }
    & $python "scripts\audit_v020_governance.py"
    if ($LASTEXITCODE -ne 0) { throw "Source v0.20 Governance structural audit failed" }
  } finally { Pop-Location }

  Write-Host "[2/7] Stopping Headquarters and creating rollback checkpoint..."
  $stopScript = Join-Path $TargetRoot "scripts\stop-headquarters.ps1"
  if (Test-Path $stopScript -PathType Leaf) {
    & $stopScript -TargetRoot $TargetRoot
  }
  foreach ($name in $ownedDirectories) {
    $current = Join-Path $TargetRoot $name
    if (Test-Path $current) { Copy-Item -LiteralPath $current -Destination (Join-Path $backupRoot $name) -Recurse -Force }
  }
  foreach ($name in $ownedFiles) {
    $current = Join-Path $TargetRoot $name
    if (Test-Path $current -PathType Leaf) { Copy-Item -LiteralPath $current -Destination (Join-Path $backupRoot $name) -Force }
  }
  New-Item -ItemType Directory -Force -Path (Join-Path $backupRoot "instance") | Out-Null
  $backupDb = Join-Path $backupRoot "instance\eason_one.db"
  & $python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $dbPath $backupDb
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path $backupDb -PathType Leaf)) { throw "Consistent SQLite backup failed" }

  Write-Host "[3/7] Installing application-owned v0.20 files..."
  foreach ($name in $ownedDirectories) {
    $source = Join-Path $SourceRoot $name
    if (-not (Test-Path $source)) { continue }
    $current = Join-Path $TargetRoot $name
    if (Test-Path $current) { Remove-Item -LiteralPath $current -Recurse -Force }
    Copy-Item -LiteralPath $source -Destination $current -Recurse -Force
  }
  foreach ($name in $ownedFiles) {
    $source = Join-Path $SourceRoot $name
    if (Test-Path $source -PathType Leaf) { Copy-Item -LiteralPath $source -Destination (Join-Path $TargetRoot $name) -Force }
  }

  Write-Host "[3b/7] Verifying installed source matches package byte-for-byte..."
  Assert-InstalledTreeMatchesSource

  Push-Location $TargetRoot
  try {
    Write-Host "[4/7] Verifying required Python dependencies..."
    & $python -c "import flask, flask_sqlalchemy, sqlalchemy, openai, anthropic, pytest; print('Runtime + diagnostic dependencies: OK')"
    if ($LASTEXITCODE -ne 0) { throw "Required Python dependency import failed" }

    Write-Host "[5/7] Compiling + auditing installed source..."
    & $python -m compileall -q eason_one scripts run.py
    if ($LASTEXITCODE -ne 0) { throw "Python compilation failed" }
    & $python "scripts\audit_v020_core.py"
    if ($LASTEXITCODE -ne 0) { throw "Installed v0.20 core structural audit failed" }
    & $python "scripts\audit_v020_governance.py"
    if ($LASTEXITCODE -ne 0) { throw "Installed v0.20 Governance structural audit failed" }

    Write-Host "[6/7] Applying explicit v0.20 database cutover with runtimes disabled..."
    & $python "scripts\migrate_v020.py" "instance\eason_one.db"
    if ($LASTEXITCODE -ne 0) { throw "v0.20 database migration failed" }

    Write-Host "[7/7] Focused v0.20 invariant + migration diagnostics..."
    $diagRun = Join-Path $TargetRoot "instance\diagnostics\v020\install-$stamp-$PID"
    $pytestTemp = Join-Path $diagRun "pytest-temp"
    $osTemp = Join-Path $diagRun "os-temp"
    New-Item -ItemType Directory -Force -Path $pytestTemp, $osTemp | Out-Null
    $oldTemp = $env:TEMP
    $oldTmp = $env:TMP
    try {
      $env:TEMP = $osTemp
      $env:TMP = $osTemp
      Write-Host "      pytest temp: $pytestTemp"
      & $python -m pytest -q "--basetemp=$pytestTemp" -p no:cacheprovider tests\test_v018_core_cutover.py tests\test_v020_core_rebuild.py tests\test_v020_governance_floor.py tests\test_v020_migration.py
      if ($LASTEXITCODE -ne 0) { throw "v0.18/v0.20 Core + Governance focused diagnostics failed" }
    }
    finally {
      $env:TEMP = $oldTemp
      $env:TMP = $oldTmp
    }
  }
  finally { Pop-Location }

  Set-Content -LiteralPath (Join-Path $TargetRoot ".v020-backup-path.txt") -Value $backupRoot -Encoding UTF8
  Write-Host "Installed Eason One v0.20.0 Company Core Rebuild." -ForegroundColor Green
  Write-Host "Rollback checkpoint: $backupRoot"
  Write-Host "Next engineering check: .\scripts\verify-v020.ps1"
  Write-Host "Then run the required real Founder Product gate in docs\history\releases\V020_CORE_REBUILD_HANDOFF.md" -ForegroundColor Yellow
}
catch {
  Write-Host "Installation failed: $($_.Exception.Message)" -ForegroundColor Red
  try { Restore-Backup } catch { Write-Host "Automatic rollback failed. Backup remains at: $backupRoot" -ForegroundColor Red }
  throw
}
