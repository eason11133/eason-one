param([string]$TargetRoot = 'D:\school\eason-one')
$ErrorActionPreference = 'Stop'
$s10 = 'ENGINE-CUMULATIVE-20260909-S10-FROM-S09'
$s11 = 'ENGINE-CUMULATIVE-20260909-S11-FROM-S10'
$package = Join-Path $TargetRoot "dist/$s11"
$stateRoot = Join-Path $TargetRoot '.eason-one-update-state'
$paths = @(
  'eason_one/services/execution.py','eason_one/services/external_effects.py',
  'eason_one/services/runtime_recovery.py','eason_one/services/company_truth.py',
  'eason_one/templates/hq_project.html','scripts/audit_v020_core.py',
  'scripts/audit_v020_governance.py','tests/test_v020_current_regressions.py'
)
function Hash([string]$Path) {
  (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}
function ExactSide([string]$Side) {
  foreach ($path in $paths) {
    $live = Join-Path $TargetRoot $path
    $image = Join-Path $package ("$Side/" + $path)
    if (!(Test-Path -LiteralPath $live) -or !(Test-Path -LiteralPath $image) -or (Hash $live) -ne (Hash $image)) { return $false }
  }
  return $true
}
function ConfirmedLine {
  $s11Receipt = Join-Path $stateRoot "$s11.json"
  if (Test-Path -LiteralPath $s11Receipt) {
    $receipt = Get-Content -Raw -LiteralPath $s11Receipt | ConvertFrom-Json
    if ($receipt.release -eq $s11 -and $receipt.status -eq 'APPLIED' -and (ExactSide 'payload')) { return $s11 }
  }
  $s10Receipt = Join-Path $stateRoot "$s10.json"
  if (Test-Path -LiteralPath $s10Receipt) {
    $receipt = Get-Content -Raw -LiteralPath $s10Receipt | ConvertFrom-Json
    if ($receipt.release -eq $s10 -and $receipt.status -eq 'APPLIED' -and (ExactSide 'baseline')) { return $s10 }
  }
  return 'UNCONFIRMED_OR_MIXED_SOURCE'
}

$s10ReceiptPath = Join-Path $stateRoot "$s10.json"
if (!(Test-Path -LiteralPath $s10ReceiptPath)) { throw "Confirmed S10 receipt missing: $s10ReceiptPath" }
$s10Receipt = Get-Content -Raw -LiteralPath $s10ReceiptPath | ConvertFrom-Json
if ($s10Receipt.release -ne $s10 -or $s10Receipt.status -ne 'APPLIED') { throw 'S10 receipt is not exactly APPLIED.' }
if (!(Test-Path -LiteralPath (Join-Path $package 'Install.ps1'))) { throw "Expanded S11 package missing: $package" }

# Zero-overwrite precondition: every affected live file must be the exact known
# S11 engineering payload before this bridge is allowed to restore S10 images.
if (!(ExactSide 'payload')) { throw 'STOPPED_ZERO_OVERWRITE: live source is not the exact known S11 engineering payload.' }

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$engineeringBackup = Join-Path $stateRoot "$s11.engineering-before-bridge-$stamp"
New-Item -ItemType Directory -Force -Path $engineeringBackup | Out-Null
foreach ($path in $paths) {
  $destination = Join-Path $engineeringBackup $path
  New-Item -ItemType Directory -Force -Path (Split-Path $destination) | Out-Null
  Copy-Item -LiteralPath (Join-Path $TargetRoot $path) -Destination $destination
}
foreach ($path in $paths) {
  if ((Hash (Join-Path $engineeringBackup $path)) -ne (Hash (Join-Path $package ("payload/" + $path)))) {
    throw "Engineering backup verification failed: $path"
  }
}

try {
  foreach ($path in $paths) {
    Copy-Item -LiteralPath (Join-Path $package ("baseline/" + $path)) -Destination (Join-Path $TargetRoot $path) -Force
  }
  if (!(ExactSide 'baseline')) { throw 'Exact S10 restoration verification failed.' }

  $testTemp = Join-Path $stateRoot "pytest-$s11-$stamp"
  New-Item -ItemType Directory -Force -Path $testTemp | Out-Null
  $oldTemp = $env:TEMP; $oldTmp = $env:TMP; $oldPytest = $env:PYTEST_ADDOPTS
  try {
    $env:TEMP = $testTemp
    $env:TMP = $testTemp
    $env:PYTEST_ADDOPTS = "--basetemp=$testTemp/basetemp -p no:cacheprovider"
    & (Join-Path $package 'Install.ps1') -TargetRoot $TargetRoot
    if ($LASTEXITCODE -ne 0) { throw "S11 installer exited $LASTEXITCODE" }
  } finally {
    $env:TEMP = $oldTemp; $env:TMP = $oldTmp; $env:PYTEST_ADDOPTS = $oldPytest
  }

  if (!(ExactSide 'payload')) { throw 'Post-install S11 payload verification failed.' }
  $receiptPath = Join-Path $stateRoot "$s11.json"
  if (!(Test-Path -LiteralPath $receiptPath)) { throw 'S11 APPLIED receipt missing after installer.' }
  $receipt = Get-Content -Raw -LiteralPath $receiptPath | ConvertFrom-Json
  if ($receipt.release -ne $s11 -or $receipt.status -ne 'APPLIED') { throw 'S11 receipt is not exactly APPLIED.' }

  & (Join-Path $TargetRoot 'scripts/Initialize-EngineeringWorktree.ps1') -DeploymentRoot $TargetRoot
  Write-Host "SUCCESS_EXACT_S11_INSTALLED: $s11"
  Write-Host "Engineering preservation: $engineeringBackup"
} catch {
  Write-Host "BRIDGE_FAILED_CONFIRMED_BASELINE: $(ConfirmedLine)"
  Write-Host "Engineering preservation remains at: $engineeringBackup"
  throw
}
