$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython)) {
    $python313 = $null
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $version = & py -3.13 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
            if ($version -eq "3.13") { $python313 = @("py", "-3.13") }
        } catch {}
    }
    if (-not $python313 -and (Get-Command python -ErrorAction SilentlyContinue)) {
        $version = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ($version -eq "3.13") { $python313 = @("python") }
    }
    if (-not $python313) {
        throw "Python 3.13 is required. Install it, then run scripts\setup-dev.ps1 again."
    }
    if ($python313.Count -eq 2) {
        & $python313[0] $python313[1] -m venv (Join-Path $repoRoot ".venv")
    } else {
        & $python313[0] -m venv (Join-Path $repoRoot ".venv")
    }
}

& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -e "${repoRoot}[test]"
Write-Host "Development environment ready. Run scripts\run-dev.ps1."
