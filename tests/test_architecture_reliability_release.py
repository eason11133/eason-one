import pytest
from datetime import datetime, timezone
from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import Employee, Operation, Project, Task, Work


def _operation_project():
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name="Architecture acceptance", objective="Prove one bounded result",
        status="ACTIVE", environment="LIVE", origin="TEST", priority="HIGH",
        owner_employee_id=ceo.id, real_budget_limit=Decimal("3"),
    )
    db.session.add(project); db.session.flush()
    operation = Operation(
        title="Architecture acceptance", objective=project.objective,
        project_id=project.id, proposed_by_employee_id=ceo.id,
        status="RUNNING", kernel_status="RUNNING", current_stage="COMPANY_KERNEL_V020",
        plan_json={"tasks": []}, approved_budget_twd=Decimal("3"),
        hard_cost_cap_twd=Decimal("3"), max_calls=8, max_revisions=1,
        max_messages=8, max_elapsed_seconds=600,
        approved_at=datetime.now(timezone.utc),
        memory_json={},
    )
    db.session.add(operation); db.session.flush()
    return ceo, project, operation


def test_review_output_envelope_uses_full_model_config_without_hidden_ceiling(ctx):
    from eason_one.services.work_execution import review_output_token_limit

    critic = Employee.query.filter_by(slug="critic").one()
    critic.current_model.max_output_tokens = 4096
    assert review_output_token_limit(critic.current_model, [{"id": f"C{i}"} for i in range(3)]) == 4096
    assert review_output_token_limit(critic.current_model, []) == 4096
    assert review_output_token_limit(critic.current_model, [{"id": str(i)} for i in range(20)]) == 4096


def test_review_output_envelope_replaces_historical_1100_cap(ctx):
    """Release-contract compatibility node for the stronger no-hidden-ceiling invariant."""
    test_review_output_envelope_uses_full_model_config_without_hidden_ceiling(ctx)


def test_codex_boundary_freezes_exact_contract_path_and_rejects_extra_delta(ctx):
    from eason_one.services.codex_connector import (
        ensure_execution_boundary, freeze_write_scope, _allowed_delta_paths,
    )

    ceo, project, operation = _operation_project()
    engineer = Employee.query.filter_by(slug="engineer").one()
    work = Work(
        project_id=project.id, operation_id=operation.id,
        title="Create exact brief", purpose="Create trial_company_output/first_company_trial.html only",
        expected_output="trial_company_output/first_company_trial.html",
        acceptance_criteria="Only trial_company_output/first_company_trial.html may be added.",
        state="READY", work_type="DELIVERY", priority="HIGH",
        created_by_employee_id=ceo.id, runtime_control_json={
            "codex_write_scope": freeze_write_scope(
                {"version": "CODEX_WRITE_SCOPE_V1", "paths": [
                    "trial_company_output/first_company_trial.html"
                ]},
                source="APPROVED_OPERATION_PLAN", authority_ref=f"operation:{operation.id}:plan",
            ),
        },
    )
    db.session.add(work); db.session.flush()
    task = Task(
        project_id=project.id, operation_id=operation.id, work_id=work.id,
        title=work.title, objective=work.purpose, status="ASSIGNED", priority="HIGH",
        created_by_employee_id=ceo.id, assigned_employee_id=engineer.id,
        required_output=work.expected_output, acceptance_criteria=work.acceptance_criteria,
    )
    db.session.add(task); db.session.flush()

    boundary = ensure_execution_boundary(work, task)
    assert boundary["allowed_paths"] == ["trial_company_output/first_company_trial.html"]
    assert boundary["max_changed_files"] == 1
    assert _allowed_delta_paths(boundary, [
        "trial_company_output/first_company_trial.html", "eason_one/routes.py",
    ]) == ["eason_one/routes.py"]


def test_prohibition_prose_never_grants_codex_write_authority(ctx):
    from eason_one.services.codex_connector import ensure_execution_boundary

    ceo, project, operation = _operation_project()
    engineer = Employee.query.filter_by(slug="engineer").one()
    work = Work(
        project_id=project.id, operation_id=operation.id,
        title="Prohibition only", purpose="Do not modify eason_one/routes.py",
        expected_output="Never edit eason_one/routes.py",
        acceptance_criteria="eason_one/routes.py must remain unchanged.",
        state="READY", work_type="DELIVERY", priority="HIGH",
        created_by_employee_id=ceo.id, runtime_control_json={},
    )
    db.session.add(work); db.session.flush()
    task = Task(
        project_id=project.id, operation_id=operation.id, work_id=work.id,
        title=work.title, objective=work.purpose, status="ASSIGNED", priority="HIGH",
        created_by_employee_id=ceo.id, assigned_employee_id=engineer.id,
        required_output=work.expected_output, acceptance_criteria=work.acceptance_criteria,
    )
    db.session.add(task); db.session.flush()

    try:
        ensure_execution_boundary(work, task)
    except ValueError as exc:
        assert str(exc) == "CODEX_STRUCTURED_WRITE_SCOPE_MISSING"
    else:
        raise AssertionError("Prohibition prose incorrectly granted write authority")


def test_isolated_codex_delta_is_all_or_nothing_and_excludes_protected_truth(tmp_path):
    from eason_one.services.codex_connector import (
        _copy_back_approved_paths, _create_isolated_workspace,
        _isolated_workspace_delta, _snapshot_repository,
    )

    source = tmp_path / "live"
    source.mkdir(); (source / ".git").mkdir(); (source / "instance").mkdir()
    (source / ".env").write_text("SECRET=never-copy", encoding="utf-8")
    (source / ".env.local").write_text("SECRET=also-never-copy", encoding="utf-8")
    (source / "live.db").write_bytes(b"alternate-live-db")
    (source / "instance" / "eason_one.db").write_bytes(b"live-db")
    (source / "dirty.txt").write_text("founder dirty truth", encoding="utf-8")
    (source / "eason_one").mkdir(); (source / "eason_one" / "routes.py").write_text("original", encoding="utf-8")
    before = _snapshot_repository(source)
    root, isolated = _create_isolated_workspace(before)
    try:
        assert not (isolated / ".git").exists()
        assert not (isolated / ".env").exists()
        assert not (isolated / ".env.local").exists()
        assert not (isolated / "live.db").exists()
        assert not (isolated / "instance").exists()
        allowed = "trial_company_output/first_company_trial.html"
        target = isolated / allowed
        target.parent.mkdir(parents=True); target.write_text("approved", encoding="utf-8")
        (isolated / "eason_one" / "routes.py").write_text("forbidden", encoding="utf-8")
        changed = _isolated_workspace_delta(isolated, before)
        boundary = {"allowed_paths": [allowed]}
        rejected = _copy_back_approved_paths(source, isolated, before, changed, boundary)
        assert rejected == {
            "applied": False, "reason": "EXTRA_CHANGED_PATH",
            "outside": ["eason_one/routes.py"], "copied": [],
        }
        assert not (source / allowed).exists()
        assert (source / "eason_one" / "routes.py").read_text(encoding="utf-8") == "original"
        assert (source / "dirty.txt").read_text(encoding="utf-8") == "founder dirty truth"

        (isolated / "eason_one" / "routes.py").write_text("original", encoding="utf-8")
        changed = _isolated_workspace_delta(isolated, before)
        accepted = _copy_back_approved_paths(source, isolated, before, changed, boundary)
        assert accepted["applied"] is True
        assert accepted["copied"] == [allowed]
        assert (source / allowed).read_text(encoding="utf-8") == "approved"
        assert (source / ".env").read_text(encoding="utf-8") == "SECRET=never-copy"
        assert (source / "instance" / "eason_one.db").read_bytes() == b"live-db"
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_unchanged_authority_exhaustion_becomes_quiescent_and_cannot_starve_siblings(ctx, monkeypatch):
    from eason_one.services import company_kernel, project_outcome

    ceo, project, operation = _operation_project()
    management = Work(
        project_id=project.id, operation_id=operation.id,
        title="Manage", purpose="Manage", state="EXECUTING",
        work_type="MANAGEMENT", priority="HIGH", created_by_employee_id=ceo.id,
        runtime_control_json={},
    )
    db.session.add(management); db.session.commit()
    evaluation = {
        "project_id": project.id, "overall_status": "INSUFFICIENT_EVIDENCE",
        "contract_hash": "contract", "authority_hash": "authority",
        "accepted_work_ids": [], "criteria": [],
    }
    monkeypatch.setattr(project_outcome, "evaluate", lambda value: dict(evaluation))
    monkeypatch.setattr(project_outcome, "refresh_deterministic_http_evidence", lambda value: {"new_proofs": []})

    first = company_kernel._reconcile_authority_exhausted_outcome(project, operation, management)
    assert first["status"] == "AUTHORITY_EXHAUSTED"
    assert project.status == "BLOCKED"
    second = company_kernel._reconcile_authority_exhausted_outcome(project, operation, management)
    assert second is None


@pytest.mark.parametrize("forbidden", [
    ".git/HEAD",
    "instance/eason_one.db",
    ".env",
    "eason_one/routes.py",
    "unexpected-output.txt",
])
def test_isolated_codex_full_delta_rejects_every_extra_path_with_zero_copyback(tmp_path, forbidden):
    from eason_one.services.codex_connector import (
        _copy_back_approved_paths, _create_isolated_workspace,
        _isolated_workspace_delta, _snapshot_repository,
    )

    source = tmp_path / "live"
    source.mkdir()
    (source / "eason_one").mkdir()
    (source / "eason_one" / "routes.py").write_text("live source", encoding="utf-8")
    before = _snapshot_repository(source)
    root, isolated = _create_isolated_workspace(before)
    try:
        allowed = "trial_company_output/first_company_trial.html"
        approved = isolated / allowed
        approved.parent.mkdir(parents=True, exist_ok=True)
        approved.write_text("approved html", encoding="utf-8")

        extra = isolated / forbidden
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes(b"forbidden isolated delta")

        changed = _isolated_workspace_delta(isolated, before)
        assert allowed in changed
        assert forbidden in changed
        result = _copy_back_approved_paths(
            source, isolated, before, changed, {"allowed_paths": [allowed]}
        )
        assert result["applied"] is False
        assert result["reason"] == "EXTRA_CHANGED_PATH"
        assert forbidden in result["outside"]
        assert result["copied"] == []
        assert not (source / allowed).exists()
        assert (source / "eason_one" / "routes.py").read_text(encoding="utf-8") == "live source"
        assert not (source / ".git" / "HEAD").exists()
        assert not (source / "instance" / "eason_one.db").exists()
        assert not (source / ".env").exists()
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
