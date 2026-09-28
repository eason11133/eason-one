param(
  [string]$TargetRoot = "D:\school\eason-one",
  [string]$BackupRoot = ""
)
$ErrorActionPreference = "Stop"
$TargetRoot = [System.IO.Path]::GetFullPath($TargetRoot)
if (-not $BackupRoot) {
  $pointer = Join-Path $TargetRoot ".v018-backup-path.txt"
  if (Test-Path $pointer) { $BackupRoot = (Get-Content $pointer -Raw).Trim() }
}
if (-not $BackupRoot -or -not (Test-Path $BackupRoot)) { throw "v0.18 backup directory was not found." }
$ownedDirectories = @("eason_one", "docs", "scripts", "tests", ".github")
$ownedFiles = @("run.py", "pyproject.toml", "README.md", ".gitignore", "INSTALL_V018_POWERSHELL.txt")
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
if (Test-Path $savedDb) { Copy-Item -LiteralPath $savedDb -Destination (Join-Path $TargetRoot "instance\eason_one.db") -Force }
Write-Host "Rolled back code and database from: $BackupRoot" -ForegroundColor Yellow
