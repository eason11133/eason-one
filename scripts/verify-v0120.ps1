param(
  [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
  [switch]$SkipFullPytest
)
& (Join-Path $PSScriptRoot "verify-v0121.ps1") -ProjectRoot $ProjectRoot -SkipFullPytest:$SkipFullPytest
exit $LASTEXITCODE
