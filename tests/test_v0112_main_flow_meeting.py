from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, ModelConfig, Operation, Project, Task, WorkMessage
from eason_one.services.context import build_with_composition
from eason_one.services.execution_policy import select_execution_model
from eason_one.services.host_validation import validate_codex_result
from eason_one.services.meetings import _active_speakers, create as create_meeting
from eason_one.services.operation_runtime import runtime_snapshot


def _operation(title="September Project Priority Decision — Autonomous Meeting Validation"):
    ceo = Employee.query.filter_by(slug="ceo").one()
    project = Project(
        name=title, objective="Compare Project Alpha expansion and Learning Pilot",
        status="ACTIVE", priority="HIGH", environment="LIVE", origin="TEST",
        owner_employee_id=ceo.id,
    )
    db.session.add(project)
    db.session.flush()
    operation = Operation(
        title=title,
        objective="Use existing Eason One evidence and low-cost approved models only.",
        project_id=project.id,
        proposed_by_employee_id=ceo.id,
        status="RUNNING",
        plan_json={
            "executive_response": "Use only low-cost approved models. No premium model. Use existing Eason One evidence only.",
            "operation": {
                "title": title,
                "objective": "Compare Project Alpha expansion and Learning Pilot",
                "completion_criteria": ["Use existing evidence only"],
                "meeting_config": {
                    "participant_employee_ids": [2, 3],
                },
            },
        },
        approved_budget_twd=Decimal("20"),
        actual_cost_twd=Decimal("0"),
        memory_json={"mission_kind": "REAL_WORK"},
    )
    db.session.add(operation)
    db.session.flush()
    return operation, project


def test_application_source_has_no_legacy_query_get_calls():
    root = Path(__file__).parents[1] / "eason_one"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if ".query.get(" in text or ".query.get_or_404(" in text:
            offenders.append(str(path.relative_to(root.parent)))
    assert offenders == []


def test_task_context_retrieves_relevant_persisted_ceo_evidence(ctx):
    operation, project = _operation()
    researcher = Employee.query.filter_by(slug="researcher").one()
    task = Task(
        project_id=project.id, operation_id=operation.id,
        title="Existing-evidence comparison",
        objective="Compare Project Alpha expansion and the Learning Pilot using existing Eason One evidence.",
        status="ASSIGNED", priority="HIGH", assigned_employee_id=researcher.id,
        reviewer_employee_id=Employee.query.filter_by(slug="research-director").one().id,
        required_output="Operation result", acceptance_criteria="Existing evidence only",
    )
    db.session.add(task)
    db.session.add(WorkMessage(
        message_type="CEO_TO_FOUNDER",
        content=(
            "Project Alpha is the stronger 30-day revenue candidate because it builds on an existing "
            "customer and product. Learning Pilot has greater long-term platform upside "
            "but less payment evidence and a less bounded MVP."
        ),
    ))
    db.session.commit()

    context, composition = build_with_composition(researcher, project, task)

    assert "RETRIEVED EXISTING EASON ONE EVIDENCE" in context
    assert "Project Alpha is the stronger 30-day revenue candidate" in context
    assert composition["evidence_retrieval"]["relevant_items"] >= 1


def test_low_cost_no_premium_policy_overrides_sonnet_and_mock(app, ctx, monkeypatch):
    operation, _ = _operation()
    openai = ModelConfig(
        label="OpenAI Economy", provider_key="openai", model_name="gpt-test-economy",
        input_price_per_million=Decimal("1"), output_price_per_million=Decimal("2"),
        currency="TWD", active=True, archived=False,
    )
    db.session.add(openai)
    db.session.commit()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    previous = app.config["TESTING"]
    app.config["TESTING"] = False
    try:
        researcher = Employee.query.filter_by(slug="researcher").one()
        director = Employee.query.filter_by(slug="research-director").one()
        researcher_model = select_execution_model(researcher, operation, "TASK_EXECUTION")
        director_model = select_execution_model(director, operation, "TASK_EXECUTION")
    finally:
        app.config["TESTING"] = previous

    assert researcher_model.provider_key == "openai"
    assert "sonnet" not in researcher_model.model_name.lower()
    assert director_model.provider_key == "openai"
    assert director_model.provider_key != "mock"


def test_autonomous_meeting_routes_specialists_not_ceo_chair(ctx):
    operation, project = _operation()
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    director = Employee.query.filter_by(slug="research-director").one()
    meeting = create_meeting(
        title="Priority decision", purpose="Reconcile specialist views",
        agenda="Choose Project Alpha or Learning Pilot", chair=ceo,
        participants=[researcher, director], project=project,
        max_rounds=1, token_limit=6000, real_cost_limit_twd=5,
        profile="ECONOMY", max_speakers_per_round=2, contribution_output_cap=500,
    )
    meeting.operation_id = operation.id
    db.session.commit()

    assert {employee.slug for employee in _active_speakers(meeting)} == {
        "researcher", "research-director"
    }


def test_host_validation_uses_windows_venv_and_reports_success(ctx, tmp_path, monkeypatch):
    repo = tmp_path
    python = repo / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_text("stub", encoding="utf-8")
    task = SimpleNamespace(
        title="Remove Query.get LegacyAPIWarning",
        objective="Run the 49-test V0.11 focused suite",
        acceptance_criteria="49 tests pass and zero LegacyAPIWarning",
    )
    monkeypatch.setenv("EASON_ONE_TEST_HOST_VALIDATION", "1")
    monkeypatch.setattr("eason_one.services.host_validation._is_windows_host", lambda: True)
    monkeypatch.setattr(
        "eason_one.services.host_validation.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="49 passed", stderr=""),
    )

    result = validate_codex_result(task, repo, {"tests": [], "acceptance": []})

    assert result["attempted"] is True
    assert result["success"] is True
    assert result["test"]["status"] == "PASSED"
    assert "49 passed" in result["test"]["detail"]


def test_terminal_runtime_preserves_latest_employee_run_and_live_history(ctx):
    operation, project = _operation("Terminal history")
    operation.status = "WAITING_FOR_FOUNDER"
    operation.memory_json = {
        "runtime": {
            "live_execution": {
                "run_id": 999,
                "state": "FAILED",
                "phase": "FAILED",
                "last_progress_phase": "RUNNING_TESTS",
                "events": [{"kind": "TEST", "detail": "Windows validation ran"}],
                "counters": {"commands_started": 3, "commands_completed": 3},
            }
        }
    }
    engineer = Employee.query.filter_by(slug="engineer").one()
    model = engineer.current_model
    run = AgentRun(
        employee_id=engineer.id, project_id=project.id, operation_id=operation.id,
        model_config_id=model.id, purpose="TASK_EXECUTION", user_request="Test",
        system_prompt_snapshot="test", context_snapshot="test", status="FAILED",
        provider_key_snapshot=model.provider_key, model_name_snapshot=model.model_name,
        input_price_snapshot=0, output_price_snapshot=0,
        currency_snapshot="TWD", currency="TWD", failure_reason="HOST_VALIDATION_FAILED",
    )
    db.session.add(run)
    db.session.commit()

    snapshot = runtime_snapshot(operation)

    assert snapshot["execution_run"] is None
    assert snapshot["latest_execution_run"]["id"] == run.id
    assert snapshot["live_execution"]["events"][0]["detail"] == "Windows validation ran"
    assert snapshot["live_execution"]["last_progress_phase"] == "RUNNING_TESTS"


def test_real_autonomous_meeting_runs_tasks_room_minutes_and_final_report(ctx):
    from eason_one.models import Meeting
    from eason_one.services import operations

    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    director = Employee.query.filter_by(slug="research-director").one()
    plan = {
        "mode": "OPERATION_PLAN",
        "executive_response": (
            "Use low-cost approved models and existing Eason One evidence only. "
            "Automatically create one governed Meeting before the final report."
        ),
        "operation": {
            "title": "September Project Priority Decision — Autonomous Meeting E2E",
            "objective": "Choose Project Alpha expansion or Learning Pilot using existing evidence.",
            "project_id": None,
            "budget_twd": 20.0,
            "tasks": [
                {
                    "title": "Existing-evidence comparison",
                    "objective": "Compare Project Alpha expansion and Learning Pilot.",
                    "assignee_employee_id": researcher.id,
                    "reviewer_employee_id": director.id,
                    "acceptance_criteria": ["Produce a concise evidence comparison."],
                },
                {
                    "title": "Independent challenge",
                    "objective": "Challenge the comparison and preserve the strongest dissent.",
                    "assignee_employee_id": director.id,
                    "reviewer_employee_id": researcher.id,
                    "acceptance_criteria": ["Record a meaningful independent challenge."],
                },
            ],
            "meeting_policy": "AUTO",
            "meeting_config": {
                "trigger": "BEFORE_FINAL_REPORT",
                "participant_employee_ids": [researcher.id, director.id],
                "max_rounds": 1,
                "max_speakers_per_round": 2,
                "contribution_output_cap": 500,
                "token_limit": 6000,
                "budget_twd": 5.0,
                "retry_limit": 1,
            },
            "completion_criteria": ["Deliver one concise Founder recommendation."],
        },
    }
    operation = operations.propose_operation(ceo, plan)
    assert operation.plan_json["operation"]["meeting_config"]["token_limit"] == 10000
    operations.approve(operation)

    for sequence in range(30):
        if operation.status != "RUNNING":
            break
        operations.next_step(operation, f"v0112-meeting-e2e-{sequence}")
        db.session.refresh(operation)

    meeting = Meeting.query.filter_by(operation_id=operation.id).one()
    assert operation.status == "COMPLETED"
    assert meeting.status == "ENDED"
    assert meeting.current_round == 1
    assert meeting.minutes_json
    assert set(meeting.minutes_json["participants_who_spoke"]) == {
        researcher.name, director.name,
    }
    assert AgentRun.query.filter_by(
        operation_id=operation.id, purpose="MEETING_CONTRIBUTION", status="SUCCEEDED"
    ).count() == 2
    assert operation.founder_report_json["result"]


def test_real_mission_never_exposes_validation_archive_control(ctx):
    operation, _ = _operation("Real Mission archive control")
    operation.status = "WAITING_FOR_FOUNDER"
    db.session.commit()

    snapshot = runtime_snapshot(operation)

    assert snapshot["can_archive"] is False
