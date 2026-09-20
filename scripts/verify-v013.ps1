param(
  [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
  [switch]$FullPytest
)

$ErrorActionPreference = "Stop"
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python -PathType Leaf)) { throw "Preserved .venv Python not found: $python" }

$tempBase = Join-Path $env:LOCALAPPDATA "EasonOne\pytest-v013"
$runTemp = Join-Path $tempBase ("focused-" + $PID + "-" + (Get-Date -Format "yyyyMMddHHmmss"))
New-Item -ItemType Directory -Force -Path $runTemp | Out-Null

Push-Location $ProjectRoot
try {
  Write-Host "[1/6] Compiling Python..."
  & $python -m compileall -q eason_one scripts tests run.py
  if ($LASTEXITCODE -ne 0) { throw "Python compilation failed with exit code $LASTEXITCODE" }

  Write-Host "[2/6] Parsing Founder-facing Jinja templates..."
  & $python -c "from pathlib import Path; from jinja2 import Environment; e=Environment(); fs=['eason_one/templates/hq_base.html','eason_one/templates/headquarters.html','eason_one/templates/hq_projects.html','eason_one/templates/hq_project.html','eason_one/templates/hq_results.html','eason_one/templates/hq_missions.html','eason_one/templates/hq_memory.html','eason_one/templates/hq_finance.html','eason_one/templates/hq_system.html','eason_one/templates/hq_people.html','eason_one/templates/hq_meetings.html']; [e.parse(Path(f).read_text(encoding='utf-8')) for f in fs]; print('Jinja templates parsed:',len(fs))"
  if ($LASTEXITCODE -ne 0) { throw "Jinja template parsing failed with exit code $LASTEXITCODE" }

  Write-Host "[3/6] Checking SQLite integrity without modifying data..."
  $dbPath = Join-Path $ProjectRoot "instance\eason_one.db"
  if (Test-Path $dbPath -PathType Leaf) {
    & $python -c "import sqlite3; p=r'$dbPath'; c=sqlite3.connect(p); assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; assert not c.execute('PRAGMA foreign_key_check').fetchall(); c.close(); print('SQLite integrity and foreign keys passed.')"
    if ($LASTEXITCODE -ne 0) { throw "SQLite integrity check failed with exit code $LASTEXITCODE" }
  } else {
    Write-Host "No existing database found; integrity check skipped."
  }

  Write-Host "[4/6] Running zero-provider Project-first + Founder-state regression suite..."
  & $python -m pytest -q -p no:cacheprovider --basetemp $runTemp `
    tests\test_v0120_architecture_reset.py `
    tests\test_v0121_founder_delegation.py `
    tests\test_v0122_founder_state_truth.py `
    tests\test_v013_project_first.py `
    tests\test_founder_ui_compliance.py `
    tests\test_final_v1_dogfood.py::test_compact_founder_schema_limits_are_frozen
  if ($LASTEXITCODE -ne 0) { throw "Focused pytest failed with exit code $LASTEXITCODE" }

  Write-Host "[5/6] Verifying protected People/Meetings surfaces and Project-first navigation..."
  $hqBasePath = Join-Path $ProjectRoot "eason_one\templates\hq_base.html"
  $peoplePath = Join-Path $ProjectRoot "eason_one\templates\hq_people.html"
  $meetingsPath = Join-Path $ProjectRoot "eason_one\templates\hq_meetings.html"
  if (-not (Test-Path $hqBasePath -PathType Leaf)) { throw "HQ base template missing: $hqBasePath" }
  if (-not (Test-Path $peoplePath -PathType Leaf)) { throw "Protected People surface missing: $peoplePath" }
  if (-not (Test-Path $meetingsPath -PathType Leaf)) { throw "Protected Meetings surface missing: $meetingsPath" }
  $hqBase = Get-Content -LiteralPath $hqBasePath -Raw
  $requiredNavigation = @(
    "/headquarters/projects','Projects'",
    "/headquarters/people','People'",
    "/headquarters/meetings','Meetings'"
  )
  foreach ($snippet in $requiredNavigation) {
    if (-not $hqBase.Contains($snippet)) { throw "Required Founder navigation missing: $snippet" }
  }
  Write-Host "Protected People/Meetings surfaces and Project-first navigation are present."

  if ($FullPytest) {
    Write-Host "[6/6] Running optional full pytest..."
    $fullTemp = Join-Path $tempBase ("full-" + $PID + "-" + (Get-Date -Format "yyyyMMddHHmmss"))
    New-Item -ItemType Directory -Force -Path $fullTemp | Out-Null
    & $python -m pytest -q -p no:cacheprovider --basetemp $fullTemp
    if ($LASTEXITCODE -ne 0) { throw "Full pytest failed with exit code $LASTEXITCODE" }
  } else {
    Write-Host "[6/6] Full legacy pytest intentionally not used as the install gate because V0.13 replaces the old HQ information architecture."
  }

  Write-Host "Eason One V0.13 Project-first Working Company verification passed." -ForegroundColor Green
}
finally {
  Pop-Location
}
