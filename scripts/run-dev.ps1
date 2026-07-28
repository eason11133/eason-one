$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Repository environment is missing. Run scripts\setup-dev.ps1 first."
}

& $venvPython -c "import openai, anthropic"
if ($LASTEXITCODE -ne 0) {
    throw "Provider dependencies are incomplete. Run scripts\setup-dev.ps1."
}

Push-Location $repoRoot
try {
    & $venvPython -m flask --app run.py run @args
} finally {
    Pop-Location
}
