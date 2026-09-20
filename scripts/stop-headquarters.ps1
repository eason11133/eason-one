param(
    [int]$Port = 5000,
    [string]$TargetRoot = "D:\school\eason-one",
    [switch]$ForceAnyProcess
)

$ErrorActionPreference = "Stop"
$listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -eq 0) {
    Write-Host "Eason One is not listening on port $Port." -ForegroundColor Green
    exit 0
}

$pids = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
foreach ($pidValue in $pids) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction SilentlyContinue
    $commandLine = if ($null -ne $proc) { [string]$proc.CommandLine } else { "" }
    $executablePath = if ($null -ne $proc) { [string]$proc.ExecutablePath } else { "" }
    $expectedPython = Join-Path $TargetRoot ".venv\Scripts\python.exe"
    $looksLikeEason = (
        $commandLine -match "flask" -and
        $commandLine -match "run\.py" -and
        (
            $executablePath -ieq $expectedPython -or
            $commandLine -match [regex]::Escape($TargetRoot)
        )
    )

    if (-not $looksLikeEason -and -not $ForceAnyProcess) {
        throw "PID $pidValue owns port $Port but was not confidently identified as Eason One. Refusing to kill it. CommandLine: $commandLine"
    }

    Write-Host "Stopping Eason One process tree on port $Port (PID $pidValue)..." -ForegroundColor Yellow
    & taskkill.exe /PID $pidValue /T /F
    if ($LASTEXITCODE -ne 0) {
        throw "taskkill failed for PID $pidValue (exit code $LASTEXITCODE)."
    }
}

Start-Sleep -Milliseconds 250
$remaining = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
if ($remaining.Count -gt 0) {
    $remainingPids = ($remaining | Select-Object -ExpandProperty OwningProcess -Unique) -join ", "
    throw "Port $Port is still listening after shutdown attempt (PID: $remainingPids)."
}

Write-Host "Eason One is stopped; port $Port is free." -ForegroundColor Green
