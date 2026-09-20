param(
  [string]$DeploymentRoot = 'D:\school\eason-one',
  [string]$EngineeringRoot = 'D:\school\eason-one-dev',
  [string]$Branch = 'codex/s12-engineering'
)
$ErrorActionPreference = 'Stop'
if ((Resolve-Path $DeploymentRoot).Path -ne 'D:\school\eason-one') { throw 'Deployment root must remain D:\school\eason-one.' }
if (Test-Path -LiteralPath $EngineeringRoot) {
  Write-Host "ENGINEERING_TREE_ALREADY_EXISTS: $EngineeringRoot"
  exit 0
}
Push-Location $DeploymentRoot
try {
  & git show-ref --verify --quiet "refs/heads/$Branch"
  if ($LASTEXITCODE -eq 0) { & git worktree add $EngineeringRoot $Branch }
  else { & git worktree add -b $Branch $EngineeringRoot HEAD }
  if ($LASTEXITCODE -ne 0) { throw 'git worktree creation failed.' }
} finally { Pop-Location }

# Overlay the confirmed deployed source, including intentionally uncommitted
# application history, while excluding secrets, databases, runtimes and receipts.
& robocopy $DeploymentRoot $EngineeringRoot /E /XD .git .python313 .venv node_modules instance dist .eason-one-update-state .pytest_cache /XF .env .env.* *.db *.sqlite *.sqlite3 *.pyc | Out-Null
if ($LASTEXITCODE -gt 7) { throw "Engineering source overlay failed: robocopy $LASTEXITCODE" }
Set-Content -Encoding UTF8 -LiteralPath (Join-Path $EngineeringRoot '.deployment-origin.json') -Value (@{
  deployment_root=$DeploymentRoot
  confirmed_release='ENGINE-CUMULATIVE-20260909-S11-FROM-S10'
  created_at=(Get-Date).ToString('o')
  secrets_copied=$false
  production_db_copied=$false
} | ConvertTo-Json)
Write-Host "ENGINEERING_TREE_READY: $EngineeringRoot"
