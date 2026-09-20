param([string]$TargetRoot = "D:\school\eason-one")
$ErrorActionPreference = "Stop"
$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot)
$python = Join-Path $TargetRoot ".venv\Scripts\python.exe"
$dbPath = Join-Path $TargetRoot "instance\eason_one.db"
if (-not (Test-Path $python -PathType Leaf)) { throw "Existing Eason One virtual environment not found: $python" }
if (-not (Test-Path $dbPath -PathType Leaf)) { throw "Eason One database not found: $dbPath" }

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$diagRoot = Join-Path $TargetRoot "instance\diagnostics\v020\verify-$stamp-$PID"
$pytestTemp = Join-Path $diagRoot "pytest-temp"
$osTemp = Join-Path $diagRoot "os-temp"
New-Item -ItemType Directory -Force -Path $pytestTemp, $osTemp | Out-Null
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
$env:TEMP = $osTemp
$env:TMP = $osTemp

Push-Location $TargetRoot
try {
  Write-Host "[1/5] v0.20 Core + Governance structural authority audits" -ForegroundColor Cyan
  & $python "scripts\audit_v020_core.py"
  if ($LASTEXITCODE -ne 0) { throw "v0.20 core structural audit failed" }
  & $python "scripts\audit_v020_governance.py"
  if ($LASTEXITCODE -ne 0) { throw "v0.20 Governance structural audit failed" }

  Write-Host "[2/5] Python compilation" -ForegroundColor Cyan
  & $python -m compileall -q eason_one scripts run.py
  if ($LASTEXITCODE -ne 0) { throw "Python compilation failed" }

  Write-Host "[3/5] Focused v0.20 invariant + migration diagnostics" -ForegroundColor Cyan
  Write-Host "      pytest temp: $pytestTemp" -ForegroundColor DarkGray
  & $python -m pytest -q "--basetemp=$pytestTemp" -p no:cacheprovider tests\test_v018_core_cutover.py tests\test_v020_core_rebuild.py tests\test_v020_governance_floor.py tests\test_v020_migration.py
  if ($LASTEXITCODE -ne 0) { throw "v0.18/v0.20 Core + Governance focused diagnostics failed" }

  Write-Host "[4/5] Idempotent explicit migration report (runtimes disabled)" -ForegroundColor Cyan
  & $python "scripts\migrate_v020.py" "instance\eason_one.db"
  if ($LASTEXITCODE -ne 0) { throw "v0.20 database migration verification failed" }

  Write-Host "[5/5] Runtime-off application import / health route smoke" -ForegroundColor Cyan
  & $python -c "from eason_one import create_app; a=create_app({'TESTING':True,'AUTO_START_COMPANY_RUNTIME':False,'AUTO_START_OPERATION_RUNTIME':False}); c=a.test_client(); r=c.get('/api/healthz'); print(r.status_code, r.get_json()); raise SystemExit(0 if r.status_code==200 and (r.get_json() or {}).get('ok') is True else 1)"
  if ($LASTEXITCODE -ne 0) { throw "Runtime-off application smoke failed" }

  Write-Host "Engineering diagnostics completed." -ForegroundColor Green
  Write-Host "IMPORTANT: this is not release acceptance. Run the real Founder Project gate in V020_CORE_REBUILD_HANDOFF.md." -ForegroundColor Yellow
}
finally {
  Pop-Location
  $env:TEMP = $oldTemp
  $env:TMP = $oldTmp
}
