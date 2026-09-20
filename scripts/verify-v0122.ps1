param(
  [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
  [switch]$SkipFullPytest
)

$ErrorActionPreference = "Stop"
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python -PathType Leaf)) { $python = "python" }

$tempBase = Join-Path $env:LOCALAPPDATA "EasonOne\pytest-v0122"
$runTemp = Join-Path $tempBase ("run-" + $PID + "-" + (Get-Date -Format "yyyyMMddHHmmss"))
New-Item -ItemType Directory -Force -Path $runTemp | Out-Null

Push-Location $ProjectRoot
try {
  Write-Host "[1/5] Compiling Python..."
  & $python -m compileall -q eason_one scripts tests run.py
  if ($LASTEXITCODE -ne 0) { throw "Python compilation failed with exit code $LASTEXITCODE" }

  Write-Host "[2/5] Checking SQLite integrity..."
  & $python -c "import sqlite3; p=r'instance\eason_one.db'; c=sqlite3.connect(p); assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; assert not c.execute('PRAGMA foreign_key_check').fetchall(); print('SQLite integrity and foreign keys passed.')"
  if ($LASTEXITCODE -ne 0) { throw "SQLite integrity check failed with exit code $LASTEXITCODE" }

  Write-Host "[3/5] Running focused V0.12.2 Founder-state truth tests..."
  & $python -m pytest -q -p no:cacheprovider --basetemp $runTemp `
    tests\test_v0120_architecture_reset.py `
    tests\test_v0121_founder_delegation.py `
    tests\test_v0122_founder_state_truth.py `
    tests\test_final_v1_dogfood.py::test_compact_founder_schema_limits_are_frozen
  if ($LASTEXITCODE -ne 0) { throw "Focused pytest failed with exit code $LASTEXITCODE" }

  if (-not $SkipFullPytest) {
    Write-Host "[4/5] Running full pytest without the broken global pytest cache..."
    $fullTemp = Join-Path $tempBase ("full-" + $PID + "-" + (Get-Date -Format "yyyyMMddHHmmss"))
    New-Item -ItemType Directory -Force -Path $fullTemp | Out-Null
    & $python -m pytest -q -p no:cacheprovider --basetemp $fullTemp
    if ($LASTEXITCODE -ne 0) { throw "Full pytest failed with exit code $LASTEXITCODE" }
  }
  else {
    Write-Host "[4/5] Full pytest skipped by installer; focused regression suite completed."
  }

  Write-Host "[5/5] Verification complete."
  Write-Host "Eason One V0.12.2 Founder-state truth verification passed." -ForegroundColor Green
}
finally {
  Pop-Location
}
