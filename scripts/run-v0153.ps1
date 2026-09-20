$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
Write-Host "Eason One v0.15.3 starting..." -ForegroundColor Cyan
Write-Host "Open http://127.0.0.1:5000/headquarters" -ForegroundColor Green
& $python run.py
