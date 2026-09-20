param(
  [string]$DeploymentRoot = 'D:\school\eason-one',
  [string]$DevRoot = 'D:\school\eason-one-dev'
)
$ErrorActionPreference = 'Stop'
$release = 'ENGINE-CUMULATIVE-20260909-S11-FROM-S10'
$baselineRelease = 'ENGINE-CUMULATIVE-20260909-S10-FROM-S09'
$package = Join-Path $DeploymentRoot "dist/$release"
$stateRoot = Join-Path $DeploymentRoot '.eason-one-update-state'
$changed = @(
  'eason_one/services/execution.py',
  'eason_one/services/external_effects.py',
  'eason_one/services/runtime_recovery.py',
  'eason_one/services/company_truth.py',
  'eason_one/templates/hq_project.html',
  'scripts/audit_v020_core.py',
  'scripts/audit_v020_governance.py',
  'tests/test_v020_current_regressions.py'
)
$added = @('scripts/Invoke-SafePytest.ps1')

$receiptPath = Join-Path $stateRoot "$baselineRelease.json"
if (!(Test-Path -LiteralPath $receiptPath)) { throw 'S10 receipt is missing.' }
$receipt = Get-Content -Raw -LiteralPath $receiptPath | ConvertFrom-Json
if ($receipt.release -ne $baselineRelease -or $receipt.status -ne 'APPLIED') {
  throw 'S10 receipt is not exactly APPLIED.'
}
foreach ($path in $changed) {
  $deploymentFile = Join-Path $DeploymentRoot $path
  $baselineFile = Join-Path $package ("baseline/" + $path)
  if (!(Test-Path -LiteralPath $deploymentFile) -or !(Test-Path -LiteralPath $baselineFile)) {
    throw "S10 baseline file missing: $path"
  }
  if ((Get-FileHash $deploymentFile).Hash -ne (Get-FileHash $baselineFile).Hash) {
    throw "S10 baseline hash mismatch: $path"
  }
  if (!(Test-Path -LiteralPath (Join-Path $DevRoot $path))) { throw "Dev source missing: $path" }
}
foreach ($path in $added) {
  if (Test-Path -LiteralPath (Join-Path $DeploymentRoot $path)) { throw "Added file already exists: $path" }
  if (!(Test-Path -LiteralPath (Join-Path $DevRoot $path))) { throw "Dev source missing: $path" }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path $stateRoot "$release.promotion-backup-$stamp"
New-Item -ItemType Directory -Force -Path $backup | Out-Null
foreach ($path in $changed) {
  $backupFile = Join-Path $backup $path
  New-Item -ItemType Directory -Force -Path (Split-Path $backupFile) | Out-Null
  Copy-Item -LiteralPath (Join-Path $DeploymentRoot $path) -Destination $backupFile
}

try {
  foreach ($path in @($changed + $added)) {
    $target = Join-Path $DeploymentRoot $path
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Copy-Item -LiteralPath (Join-Path $DevRoot $path) -Destination $target -Force
  }
  $testRoot = Join-Path $stateRoot "pytest-post-promotion-$stamp"
  New-Item -ItemType Directory -Force -Path $testRoot | Out-Null
  Push-Location $DeploymentRoot
  try {
    $python = Join-Path $DeploymentRoot '.python313/python.exe'
    & $python scripts/audit_v020_core.py
    if ($LASTEXITCODE -ne 0) { throw 'Core audit failed.' }
    & $python scripts/audit_v020_governance.py
    if ($LASTEXITCODE -ne 0) { throw 'Governance audit failed.' }
    & '.\scripts\Invoke-SafePytest.ps1' -BaseRoot $testRoot -PytestArgs @(
      '-q', 'tests\test_v020_current_regressions.py', '-k',
      'provider_response_checkpoint or successful_model_execution_is_never_retry_authority or project_pause_preserves_truth or paused_project_defers_interrupted_meeting or output_truncation_is_explicit'
    )
    if ($LASTEXITCODE -ne 0) { throw 'Post-promotion regression failed.' }
  } finally {
    Pop-Location
  }
  $records = @(foreach ($path in @($changed + $added)) {
    @{path=$path;sha256=(Get-FileHash (Join-Path $DeploymentRoot $path) -Algorithm SHA256).Hash.ToLowerInvariant()}
  })
  @{
    release=$release
    baseline_release=$baselineRelease
    status='APPLIED'
    promotion='DEV_TO_DEPLOYMENT'
    applied_at=(Get-Date).ToString('o')
    backup_dir=$backup
    files=$records
  } | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath (Join-Path $stateRoot "$release.json")
  Write-Host "PROMOTION_APPLIED: $release"
  Write-Host "PROMOTION_BACKUP: $backup"
} catch {
  foreach ($path in $changed) {
    Copy-Item -LiteralPath (Join-Path $backup $path) -Destination (Join-Path $DeploymentRoot $path) -Force
  }
  foreach ($path in $added) {
    $target = Join-Path $DeploymentRoot $path
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
  }
  Write-Host "PROMOTION_ROLLED_BACK_TO: $baselineRelease"
  throw
}
