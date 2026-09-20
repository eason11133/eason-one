param(
    [int]$Port = 5000,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$launcher = Join-Path $repoRoot "scripts\headquarters_launcher.py"
$browserJob = $null

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Repository environment is missing. Run .\scripts\setup-dev.ps1 first."
}
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Headquarters launcher is missing: $launcher"
}

Push-Location $repoRoot
try {
    & $venvPython -c "import eason_one; from eason_one.release_snapshot import ENGINEERING_SNAPSHOT_ID; print('Eason One package import: OK'); print('Engineering snapshot:', ENGINEERING_SNAPSHOT_ID); import hashlib, pathlib; p=pathlib.Path('eason_one/services/project_company.py'); print('project_company.py SHA256:', hashlib.sha256(p.read_bytes()).hexdigest())"
    if ($LASTEXITCODE -ne 0) { throw "Eason One package could not be imported." }

    $existing = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 0) {
        $pids = ($existing | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
        throw "Port $Port is already in use (PID: $pids). Run .\scripts\stop-headquarters.ps1 first."
    }

    $hqUrl = "http://127.0.0.1:$Port/headquarters"
    Write-Host ""
    Write-Host "Eason One Headquarters v0.20.0" -ForegroundColor Cyan
    Write-Host "Headquarters : $hqUrl"
    Write-Host "Missions    : http://127.0.0.1:$Port/headquarters/missions"
    Write-Host "Results     : http://127.0.0.1:$Port/headquarters/results"
    Write-Host "Research    : http://127.0.0.1:$Port/headquarters/research"
    Write-Host "Attention   : http://127.0.0.1:$Port/headquarters/attention"
    Write-Host "Memory      : http://127.0.0.1:$Port/headquarters/memory"
    Write-Host "Finance     : http://127.0.0.1:$Port/headquarters/finance"
    Write-Host "System      : http://127.0.0.1:$Port/headquarters/system"
    Write-Host "Mobile CEO  : http://127.0.0.1:$Port/mobile"
    Write-Host "Stop server : Ctrl+C"
    Write-Host "Fallback    : .\scripts\stop-headquarters.ps1"
    Write-Host ""

    if (-not $NoBrowser) {
        $browserJob = Start-Job -ScriptBlock {
            param($Url)
            Start-Sleep -Seconds 2
            Start-Process $Url
        } -ArgumentList $hqUrl
    }

    # Do NOT use Start-Process -NoNewWindow + Wait-Process here.  On Windows
    # that leaves PowerShell, Flask and Werkzeug competing for the same console
    # Ctrl+C event.  A tiny Python owner launches Flask in a separate Windows
    # process group and catches Ctrl+C itself, then shuts that process tree down.
    & $venvPython $launcher --port $Port
    $serverExit = $LASTEXITCODE

    # 130 is the normal Ctrl+C exit code from headquarters_launcher.py.
    if ($serverExit -ne 0 -and $serverExit -ne 130) {
        throw "Eason One server exited with code $serverExit."
    }
}
finally {
    if ($null -ne $browserJob) {
        try { Stop-Job -Job $browserJob -ErrorAction SilentlyContinue } catch {}
        try { Remove-Job -Job $browserJob -Force -ErrorAction SilentlyContinue } catch {}
    }
    Pop-Location
}
