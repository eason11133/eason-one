param(
  [string]$TargetRoot = "D:\school\eason-one",
  [string]$BackupRoot = ""
)
$ErrorActionPreference = "Stop"
$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot)
if (-not $BackupRoot) {
  $pointer = Join-Path $TargetRoot ".v020-backup-path.txt"
  if (Test-Path $pointer -PathType Leaf) { $BackupRoot = (Get-Content -LiteralPath $pointer -Raw).Trim() }
}
if (-not $BackupRoot -or -not (Test-Path $BackupRoot -PathType Container)) {
  throw "v0.20 rollback checkpoint was not found."
}

$ownedDirectories = @("eason_one", "docs", "scripts", "tests", ".github")
$ownedFiles = @(
  "run.py", "pyproject.toml", "README.md", ".gitignore",
  "INSTALL_V020_POWERSHELL.txt"
)

Write-Host "Rolling Eason One back from: $BackupRoot" -ForegroundColor Yellow
foreach ($name in $ownedDirectories) {
  $current = Join-Path $TargetRoot $name
  if (Test-Path $current) { Remove-Item -LiteralPath $current -Recurse -Force }
  $saved = Join-Path $BackupRoot $name
  if (Test-Path $saved) { Copy-Item -LiteralPath $saved -Destination $current -Recurse -Force }
}
foreach ($name in $ownedFiles) {
  $current = Join-Path $TargetRoot $name
  if (Test-Path $current) { Remove-Item -LiteralPath $current -Force }
  $saved = Join-Path $BackupRoot $name
  if (Test-Path $saved) { Copy-Item -LiteralPath $saved -Destination $current -Force }
}
$savedDb = Join-Path $BackupRoot "instance\eason_one.db"
if (Test-Path $savedDb -PathType Leaf) {
  New-Item -ItemType Directory -Force -Path (Join-Path $TargetRoot "instance") | Out-Null
  $liveDb = Join-Path $TargetRoot "instance\eason_one.db"
  Remove-Item -LiteralPath "$liveDb-wal" -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath "$liveDb-shm" -Force -ErrorAction SilentlyContinue
  Copy-Item -LiteralPath $savedDb -Destination $liveDb -Force
}
Write-Host "Rollback complete. .env and .venv were left untouched." -ForegroundColor Green
