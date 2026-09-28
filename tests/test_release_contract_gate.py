import ast
from pathlib import Path

from tests.release_contract import (
    ALL_CLASSIFIED,
    CURRENT_REGRESSION,
    MIGRATION_ONLY,
    REQUIRED_CURRENT_INVARIANTS,
    RETIRED_CONTRACT,
    RETIRED_SUPERSEDED_BY,
    classification,
    target_module,
)


def _tests_root() -> Path:
    return Path(__file__).resolve().parent


def _test_modules():
    root = _tests_root()
    rows = {path.relative_to(root).as_posix() for path in root.glob("test_*.py")}
    rows.update(path.relative_to(root).as_posix() for path in (root / "acceptance").glob("test_*.py"))
    return rows


def _functions(module_path: str) -> set[str]:
    tree = ast.parse((_tests_root() / module_path).read_text(encoding="utf-8"))
    return {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _assert_current_node_exists(node_id: str) -> None:
    module, sep, function = node_id.partition("::")
    assert sep and function, f"superseding target must be an exact pytest node id: {node_id}"
    assert module in CURRENT_REGRESSION, f"superseding target is not release-gated: {node_id}"
    assert function in _functions(module), f"superseding test does not exist: {node_id}"


def test_every_preserved_test_module_has_exactly_one_semantic_classification():
    modules = _test_modules()
    assert modules == ALL_CLASSIFIED
    assert not (CURRENT_REGRESSION & RETIRED_CONTRACT)
    assert not (CURRENT_REGRESSION & MIGRATION_ONLY)
    assert not (RETIRED_CONTRACT & MIGRATION_ONLY)
    assert {classification(path) for path in modules} == {
        "CURRENT_REGRESSION", "RETIRED_CONTRACT", "MIGRATION_ONLY",
    }


def test_retired_contracts_name_exact_release_gated_superseding_tests():
    assert set(RETIRED_SUPERSEDED_BY) == RETIRED_CONTRACT
    for retired, targets in RETIRED_SUPERSEDED_BY.items():
        assert targets, f"retired module lacks a superseding current invariant: {retired}"
        for target in targets:
            _assert_current_node_exists(target)


def test_required_runtime_invariants_are_release_gated_by_exact_nodes():
    required = {
        "durable_restart",
        "no_provider_recall_after_persisted_success",
        "work_dependency_over_stale_task",
        "result_ready_without_founder_orchestration",
        "provider_at_most_once",
        "hidden_sdk_retries_disabled",
        "output_truncation_retry_semantics",
        "exact_cost",
        "no_double_billing",
        "budget_preflight",
        "agent_run_immutable_snapshots",
        "persistent_employee_experience",
        "cross_surface_work_truth",
        "codex_exact_scope",
        "codex_isolated_full_delta",
    }
    assert set(REQUIRED_CURRENT_INVARIANTS) == required
    for node_id in REQUIRED_CURRENT_INVARIANTS.values():
        _assert_current_node_exists(node_id)


def test_second_review_modules_are_not_silently_retired_without_current_ports():
    expected = {
        "test_company_core_vnext_batch_a.py": "test_v020_current_regressions.py",
        "test_patch0041.py": "test_v020_current_regressions.py",
        "test_patch0061a.py": "test_v020_current_regressions.py",
        "test_patch0071.py": "test_v020_current_regressions.py",
        "test_patch0072.py": "test_v020_current_regressions.py",
        "test_patch0072a.py": "test_v020_current_regressions.py",
        "test_hardening.py": "test_v020_current_regressions.py",
        "test_persistent_employee_memory.py": "test_v020_current_regressions.py",
        "test_cross_surface_coherence.py": "test_v020_multi_employee_company.py",
    }
    for retired, required_module in expected.items():
        targets = RETIRED_SUPERSEDED_BY[retired]
        assert any(target_module(node) == required_module for node in targets), (retired, targets)


def test_contract_inventory_document_covers_every_retired_module_and_required_invariant():
    inventory = (_tests_root().parent / "docs" / "history" / "releases" / "TEST_CONTRACT_INVENTORY.md").read_text(encoding="utf-8")
    for retired in RETIRED_CONTRACT:
        assert f"`{retired}`" in inventory
    for invariant in REQUIRED_CURRENT_INVARIANTS:
        assert f"`{invariant}`" in inventory
