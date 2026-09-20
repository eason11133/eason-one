$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }
$baseTemp = Join-Path $repo ".pytest-v0153-tmp"
Remove-Item $baseTemp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $baseTemp | Out-Null
& $python -m pytest -q `
  --basetemp="$baseTemp" `
  -p no:cacheprovider `
  tests\test_v0152_read_path_performance.py `
  tests\test_v015_founder_surface.py `
  tests\test_v013_project_first.py `
  tests\test_v014_rebuild_backbone.py
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "v0.15.3 verification passed." -ForegroundColor Green
