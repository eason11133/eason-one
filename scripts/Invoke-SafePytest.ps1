param(
  [string]$BaseRoot,
  [Parameter(ValueFromRemainingArguments=$true)][string[]]$PytestArgs
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repo '.python313\python.exe'
if (!(Test-Path -LiteralPath $python)) {
  $python = 'D:\school\eason-one\.python313\python.exe'
}
if (!(Test-Path -LiteralPath $python)) { throw 'Python 3.13 runtime was not found.' }
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if (!$BaseRoot) { $BaseRoot = Join-Path $repo '.eason-one-test-temp\pytest' }
$runTemp = Join-Path $BaseRoot "$stamp-$PID"
New-Item -ItemType Directory -Force -Path $runTemp | Out-Null
$oldTemp = $env:TEMP
$oldTmp = $env:TMP
try {
  $env:TEMP = $runTemp
  $env:TMP = $runTemp
  Push-Location $repo
  try {
    & $python -m pytest -p no:cacheprovider "--basetemp=$runTemp\basetemp" @PytestArgs
    exit $LASTEXITCODE
  } finally { Pop-Location }
} finally {
  $env:TEMP = $oldTemp
  $env:TMP = $oldTmp
}
