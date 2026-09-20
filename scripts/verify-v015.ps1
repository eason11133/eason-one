$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

$baseTemp = Join-Path $repo ".pytest-v015-tmp"
Remove-Item $baseTemp -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $baseTemp | Out-Null

& $python -m pytest -q `
  --basetemp="$baseTemp" `
  -p no:cacheprovider `
  tests\test_v015_founder_surface.py `
  tests\test_v013_project_first.py::test_v013_host_verification_rejects_wrong_exact_replacement `
  tests\test_v013_project_first.py::test_v013_host_verification_accepts_exact_replacement `
  tests\test_v013_project_first.py::test_v013_codex_claim_without_host_verification_is_not_validated_or_acceptable `
  tests\test_v0122_founder_state_truth.py::test_execution_route_schema_cannot_return_advisory_or_null_operation `
  tests\test_v0122_founder_state_truth.py::test_run80_regression_is_failed_semantically_instead_of_validation_passed `
  tests\test_v0122_founder_state_truth.py::test_delegated_work_creates_actionable_operation_with_founder_cap `
  tests\test_v0122_founder_state_truth.py::test_system_validation_cannot_become_founder_current_mission `
  tests\test_v0122_founder_state_truth.py::test_no_worker_means_no_working_task_or_employee `
  tests\test_v0122_founder_state_truth.py::test_every_needs_founder_state_has_persisted_reason_choices_and_actions `
  tests\test_v0122_founder_state_truth.py::test_successful_run_without_actual_result_is_not_a_validated_artifact

if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host "v0.15 verification passed." -ForegroundColor Green
