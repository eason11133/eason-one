$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$testRoot = Join-Path $repoRoot ".eason-one-test-temp"
$baseTemp = Join-Path $testRoot "pytest"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Repository environment is missing. Run .\scripts\setup-dev.ps1 first."
}

Push-Location $repoRoot
try {
    Remove-Item $testRoot -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $baseTemp -Force | Out-Null

    Write-Host "Checking Python imports..." -ForegroundColor Cyan
    & $venvPython -m compileall -q eason_one
    if ($LASTEXITCODE -ne 0) { throw "Python compile check failed." }

    Write-Host "Running Headquarters V0.2 and governance regression tests..." -ForegroundColor Cyan
    & $venvPython -m pytest -q `
        --basetemp $baseTemp `
        -p no:cacheprovider `
        tests/test_headquarters_v1.py `
        tests/test_cross_surface_coherence.py `
        tests/test_ceo_adaptive_stage.py `
        tests/acceptance/test_slice0091_structure.py
    if ($LASTEXITCODE -ne 0) { throw "Headquarters verification failed." }

    Write-Host "Headquarters V0.2 verification passed." -ForegroundColor Green
} finally {
    Pop-Location
}
