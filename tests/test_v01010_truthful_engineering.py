from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, EmployeeModelHistory, ModelConfig, Operation, Project, Task
from eason_one.services.engineering_runtime import prepare_engineering_repair
from eason_one.services.headquarters import employee_snapshot
from eason_one.services.operation_runtime import run_until_gate, runtime_snapshot


def _operation_with_repair():
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineer = Employee.query.filter_by(slug="engineer").one()
    critic = Employee.query.filter_by(slug="critic").one()
    project = Project(
        name="Repair Mission", objective="Test Engineer Codex recovery",
        status="ACTIVE", environment="LIVE", owner_employee_id=ceo.id,
    )
    db.session.add(project); db.session.flush()
    plan = {"mode": "OPERATION_PLAN", "executive_response": "repair", "operation": {
        "title": "Repair Mission", "objective": "Test repair", "project_id": project.id,
        "budget_twd": 5, "tasks": [
            {"title": "Engineer baseline", "objective": "Inspect read-only evidence",
             "assignee_employee_id": engineer.id, "reviewer_employee_id": engineer.id,
             "acceptance_criteria": ["Evidence"]},
            {"title": "Critic review", "objective": "Review evidence",
             "assignee_employee_id": critic.id, "reviewer_employee_id": engineer.id,
             "acceptance_criteria": ["Review"]},
        ], "meeting_policy": "NEVER",
        "meeting_config": {"trigger": "NEVER", "participant_employee_ids": [],
            "max_rounds": 1, "max_speakers_per_round": 1,
            "contribution_output_cap": 256, "token_limit": 6000,
            "budget_twd": 0, "retry_limit": 0},
        "completion_criteria": ["Engineer evidence exists"],
    }}
    operation = Operation(
        title="Repair Mission", objective="Test repair", project_id=project.id,
        proposed_by_employee_id=ceo.id, status="PAUSED", plan_json=plan,
        approved_budget_twd=Decimal("5"), actual_cost_twd=0,
        founder_report_json={"decision_kind": "ENGINEERING_RUNTIME_REPAIR"},
        memory_json={}, waiting_reason="Repair",
    )
    db.session.add(operation); db.session.flush()
    engineer_task = Task(
        project_id=project.id, operation_id=operation.id,
        title="Engineer baseline", objective="Inspect read-only evidence",
        status="ASSIGNED", assigned_employee_id=engineer.id,
        reviewer_employee_id=engineer.id, acceptance_criteria="Evidence",
        required_output="Codex evidence",
    )
    critic_task = Task(
        project_id=project.id, operation_id=operation.id,
        title="Critic review", objective="Review evidence",
        status="WORKING", assigned_employee_id=critic.id,
        reviewer_employee_id=engineer.id, acceptance_criteria="Review",
        required_output="Review result",
    )
    db.session.add_all([engineer_task, critic_task]); db.session.commit()
    return operation, engineer_task, critic_task


def test_repair_runs_one_engineer_codex_step_then_pauses(ctx):
    operation, engineer_task, critic_task = _operation_with_repair()
    prepare_engineering_repair(operation, activate=True)
    result = run_until_gate(operation.id, max_steps=8)
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    engineer_task = db.session.get(Task, engineer_task.id)
    critic_task = db.session.get(Task, critic_task.id)
    runs = AgentRun.query.filter_by(operation_id=operation.id).order_by(AgentRun.id).all()
    assert result["state"] == "PAUSED"
    assert operation.status == "PAUSED"
    assert operation.founder_report_json["decision_kind"] == "ENGINEERING_STEP_REVIEW"
    assert engineer_task.status == "DONE"
    assert engineer_task.reviewer_employee_id is None
    assert critic_task.status == "ASSIGNED"
    assert len(runs) == 1
    assert runs[0].provider_key_snapshot == "codex"
    assert runs[0].structured_validation_status == "PASSED"
    assert not AgentRun.query.filter_by(operation_id=operation.id, provider_key_snapshot="anthropic").first()


def test_failed_historical_run_is_not_current_runtime(ctx):
    operation, engineer_task, critic_task = _operation_with_repair()
    mock = ModelConfig.query.filter_by(provider_key="mock").first()
    failed = AgentRun(
        employee_id=critic_task.assigned_employee_id, project_id=operation.project_id,
        operation_id=operation.id, task_id=critic_task.id, model_config_id=mock.id,
        purpose="TASK_EXECUTION", user_request="review", system_prompt_snapshot="x",
        context_snapshot="x", status="FAILED", failure_reason="OUTPUT_TRUNCATED",
        provider_key_snapshot="anthropic", model_name_snapshot="claude-sonnet-5",
        input_price_snapshot=0, output_price_snapshot=0, currency_snapshot="TWD",
        currency="TWD", real_cost=Decimal("0.57"),
    )
    db.session.add(failed); db.session.commit()
    prepare_engineering_repair(operation, activate=False)
    snap = runtime_snapshot(operation)
    assert snap["execution_run"] is None
    assert snap["latest_execution_run"]["id"] == failed.id
    assert snap["active_task"]["id"] == engineer_task.id
    assert snap["active_task"]["employee"] == "Engineer"


def test_invalidated_mock_is_not_success_and_codex_history_is_authoritative(ctx):
    from scripts.migrate_v01010 import migrate

    operation, engineer_task, _ = _operation_with_repair()
    engineer = Employee.query.filter_by(slug="engineer").one()
    mock = ModelConfig.query.filter_by(provider_key="mock").first()
    mock_run = AgentRun(
        employee_id=engineer.id, project_id=operation.project_id,
        operation_id=operation.id, task_id=engineer_task.id, model_config_id=mock.id,
        purpose="TASK_EXECUTION", user_request="fake", system_prompt_snapshot="x",
        context_snapshot="x", status="SUCCEEDED", parsed_output_json={"result_summary": "fake"},
        provider_key_snapshot="mock", model_name_snapshot="deterministic-mock",
        input_price_snapshot=0, output_price_snapshot=0, currency_snapshot="TWD",
        currency="TWD", real_cost=0,
    )
    db.session.add(mock_run); db.session.commit()
    migrate(); db.session.expire_all()
    mock_run = db.session.get(AgentRun, mock_run.id)
    engineer = db.session.get(Employee, engineer.id)
    snapshot = employee_snapshot(engineer)
    assert mock_run.status == "INVALIDATED"
    assert mock_run.resolution_status == "INVALID_FORMAL_MOCK"
    assert snapshot["metrics"]["successful_runs"] == 0
    audit = next(row for row in snapshot["run_audit"] if row["run"].id == mock_run.id)
    assert audit["effective_status"] == "INVALIDATED"
    assert any(event["kind"] == "RUN" and "INVALIDATED" in event["detail"] for event in snapshot["history"])
    open_history = EmployeeModelHistory.query.filter_by(employee_id=engineer.id, ended_at=None).all()
    assert len(open_history) == 1
    assert open_history[0].model_config.provider_key == "codex"
    old_mock = EmployeeModelHistory.query.join(ModelConfig).filter(
        EmployeeModelHistory.employee_id == engineer.id,
        ModelConfig.provider_key == "mock",
    ).first()
    assert old_mock is None or old_mock.ended_at is not None


def test_resume_ui_has_immediate_feedback_and_no_reload(client, ctx):
    operation, _, _ = _operation_with_repair()
    page = client.get("/headquarters").get_data(as_text=True)
    mission = client.get(f"/headquarters/missions/{operation.id}").get_data(as_text=True)
    hq_js = client.get("/static/headquarters.js").get_data(as_text=True)
    runner_js = client.get("/static/operation-runner.js").get_data(as_text=True)
    assert "RESUME ENGINEER → CODEX" in page
    assert "data-runtime-resume" in mission
    assert "STARTING CODEX" in hq_js
    assert "STARTING CODEX" in runner_js
    assert "location.reload" not in runner_js
    assert "window.location.reload" not in runner_js


def test_second_resume_after_engineer_step_cannot_launch_critic(client, ctx):
    operation, _, _ = _operation_with_repair()
    prepare_engineering_repair(operation, activate=True)
    run_until_gate(operation.id, max_steps=8)
    db.session.expire_all()
    operation = db.session.get(Operation, operation.id)
    assert operation.status == "PAUSED"
    assert operation.founder_report_json["decision_kind"] == "ENGINEERING_STEP_REVIEW"
    before = AgentRun.query.filter_by(operation_id=operation.id).count()
    response = client.post(f"/operations/{operation.id}/runtime/start")
    assert response.status_code == 409
    assert "Founder review is required" in response.get_json()["error"]
    assert AgentRun.query.filter_by(operation_id=operation.id).count() == before
    assert not AgentRun.query.filter_by(operation_id=operation.id, provider_key_snapshot="anthropic").first()
    state = runtime_snapshot(operation)
    assert state["can_resume"] is False
