param([string]$TargetRoot = "D:\school\eason-one")
$ErrorActionPreference = "Stop"
$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot)
$python = Join-Path $TargetRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python -PathType Leaf)) { throw "Existing Eason One virtual environment not found: $python" }
Push-Location $TargetRoot
try {
  Write-Host "[1/3] Structural Core Cutover audit" -ForegroundColor Cyan
  & $python "scripts\audit_v018_core.py"
  if ($LASTEXITCODE -ne 0) { throw "v0.18 structural audit failed" }

  Write-Host "[2/3] Python compilation" -ForegroundColor Cyan
  & $python -m compileall -q eason_one scripts run.py
  if ($LASTEXITCODE -ne 0) { throw "Python compilation failed" }

  Write-Host "[3/3] Database cutover report (runtime stays off)" -ForegroundColor Cyan
  & $python "scripts\migrate_v018.py" "instance\eason_one.db"
  if ($LASTEXITCODE -ne 0) { throw "Database cutover verification failed" }

  Write-Host "Engineering verification completed. Live Founder acceptance is still required." -ForegroundColor Green
}
finally { Pop-Location }
