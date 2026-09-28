# TEST CONTRACT INVENTORY — S12 actual-S11 R3

- CURRENT_REGRESSION modules: 11
- MIGRATION_ONLY modules: 1
- RETIRED_CONTRACT modules: 65
- TOTAL classified preserved modules: 77

## CURRENT_REGRESSION
- `test_architecture_reliability_release.py` — 7 static test functions
- `test_ceo_operating_system.py` — 67 static test functions
- `test_healthz.py` — 2 static test functions
- `test_meeting_restart_recovery.py` — 3 static test functions
- `test_release_contract_gate.py` — 5 static test functions
- `test_v020_core_rebuild.py` — 27 static test functions
- `test_v020_current_regressions.py` — 192 static test functions
- `test_v020_governance_floor.py` — 36 static test functions
- `test_v020_governance_sweep.py` — 15 static test functions
- `test_v020_migration.py` — 1 static test functions
- `test_v020_multi_employee_company.py` — 18 static test functions

## MIGRATION_ONLY
- `test_v0107_migration.py`

## RETIRED_CONTRACT
- `acceptance/test_founder_ui_v1.py`
- `acceptance/test_slice0091_structure.py`
- `acceptance/test_slice0092_structure.py`
- `acceptance/test_slice0101_ceo_nervous_system.py`
- `acceptance/test_slice0102_goal_completion.py`
- `acceptance/test_slice010_ceo_company.py`
- `test_ceo_adaptive_stage.py`
- `test_ceo_office_v1.py`
- `test_company_core_vnext_batch_a.py`
- `test_core.py`
- `test_cross_surface_coherence.py`
- `test_dogfood_repair001.py`
- `test_final_v1_dogfood.py`
- `test_founder_truth_command_view.py`
- `test_founder_ui_compliance.py`
- `test_hardening.py`
- `test_headquarters_v07.py`
- `test_headquarters_v08.py`
- `test_headquarters_v09.py`
- `test_headquarters_v1.py`
- `test_patch0041.py`
- `test_patch0051.py`
- `test_patch0061a.py`
- `test_patch0062.py`
- `test_patch0071.py`
- `test_patch0072.py`
- `test_patch0072a.py`
- `test_patch0073.py`
- `test_persistent_employee_memory.py`
- `test_slice003.py`
- `test_slice004.py`
- `test_slice005.py`
- `test_slice006.py`
- `test_slice007.py`
- `test_slice009.py`
- `test_slice010.py`
- `test_slice0101.py`
- `test_slice0103.py`
- `test_slice011.py`
- `test_v01010_truthful_engineering.py`
- `test_v01011_wsl_codex.py`
- `test_v01012_live_execution.py`
- `test_v0103_hq_runtime.py`
- `test_v0105_audit_costs.py`
- `test_v0106_operational_runtime.py`
- `test_v0108_runtime_ui.py`
- `test_v0109_engineering_runtime.py`
- `test_v01102_nonblocking_readiness.py`
- `test_v01103_env_loading.py`
- `test_v01104_ceo_dialogue_persistence.py`
- `test_v01105_main_flow.py`
- `test_v0110_stabilization.py`
- `test_v0112_main_flow_meeting.py`
- `test_v0120_architecture_reset.py`
- `test_v0121_founder_delegation.py`
- `test_v0122_founder_state_truth.py`
- `test_v0131_multi_agent_work.py`
- `test_v013_project_first.py`
- `test_v014_sparse_founder_ui.py`
- `test_v0152_read_path_performance.py`
- `test_v015_founder_experience_cutover.py`
- `test_v015_founder_surface.py`
- `test_v015_runtime_spine.py`
- `test_v016_working_company.py`
- `test_v018_core_cutover.py`

## REQUIRED_CURRENT_INVARIANTS
- `agent_run_immutable_snapshots` → `test_v020_current_regressions.py::test_current_agent_run_provider_price_snapshots_are_immutable`
- `budget_preflight` → `test_v020_current_regressions.py::test_current_budget_preflight_blocks_before_provider`
- `codex_exact_scope` → `test_architecture_reliability_release.py::test_codex_boundary_freezes_exact_contract_path_and_rejects_extra_delta`
- `codex_isolated_full_delta` → `test_architecture_reliability_release.py::test_isolated_codex_full_delta_rejects_every_extra_path_with_zero_copyback`
- `cross_surface_work_truth` → `test_v020_multi_employee_company.py::test_project_surface_projects_work_topology_not_task_truth`
- `durable_restart` → `test_v020_current_regressions.py::test_current_durable_restart_preserves_work_artifact_event_truth`
- `exact_cost` → `test_v020_current_regressions.py::test_current_exact_cost_uses_provider_usage`
- `hidden_sdk_retries_disabled` → `test_v020_current_regressions.py::test_current_sdk_clients_disable_hidden_retries`
- `no_double_billing` → `test_v020_current_regressions.py::test_current_cost_recording_is_idempotent`
- `no_provider_recall_after_persisted_success` → `test_v020_current_regressions.py::test_current_restart_recovers_persisted_success_without_provider_recall`
- `output_truncation_retry_semantics` → `test_v020_current_regressions.py::test_current_output_truncation_is_explicit_and_cost_preserved`
- `persistent_employee_experience` → `test_v020_current_regressions.py::test_current_accepted_work_becomes_persistent_employee_experience`
- `provider_at_most_once` → `test_v020_current_regressions.py::test_current_provider_side_effect_claim_is_at_most_once`
- `result_ready_without_founder_orchestration` → `test_v020_current_regressions.py::test_current_runtime_can_reach_result_ready_without_founder_orchestration`
- `work_dependency_over_stale_task` → `test_v020_current_regressions.py::test_current_dependency_scheduler_uses_workdependency_not_stale_task`

## Notes
- `test_founder_product_v1.py` is absent on the actual S11 product line and is not classified as a preserved module.
- Its still-current pause / Project truth invariants remain covered by current regression modules.
- `test_ceo_operating_system.py` remains CURRENT_REGRESSION.
