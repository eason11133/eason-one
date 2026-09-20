import json
from decimal import Decimal

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, Operation, Project, Task
from eason_one.providers import ProviderResult
from eason_one.schemas import CEO_EXECUTION_SCHEMA, CEO_SCHEMA
from eason_one.services import current_company, operation_runtime, operations
from eason_one.services.operation_kernel import route_command
from eason_one.services.ceo import founder_request
from eason_one.services.headquarters import (
    employee_presence,
    headquarters_snapshot,
    mission_snapshot,
)
from eason_one.services.stabilization import SYSTEM_VALIDATION, global_runtime_snapshot, operation_kind


def _provider(payload):
    class Provider:
        def complete(self, *args, **kwargs):
            return ProviderResult(
                json.dumps(payload), 120, 80,
                request_id="req-v0122", response_id="resp-v0122",
            )
    return Provider()


def _execution_payload(ceo, researcher):
    return {
        "mode": "OPERATION_PLAN",
        "executive_response": (
            "I will own this bounded improvement and return a verified result."
        ),
        "project": None,
        "project_id": None,
        "tasks": [],
        "operation": {
            "title": "Founder delegation reliability repair",
            "objective": "Repair the highest-impact delegation failure and verify it.",
            "project_id": None,
            "budget_twd": 10,
            "tasks": [{
                "title": "Repair delegation chain",
                "objective": "Produce one verified improvement without micromanagement.",
                "assignee_employee_id": researcher.id,
                "reviewer_employee_id": None,
                "acceptance_criteria": ["A verified result is delivered."],
            }],
            "meeting_policy": "NEVER",
            "meeting_config": {
                "trigger": "NEVER",
                "participant_employee_ids": [],
                "max_rounds": 1,
                "max_speakers_per_round": 1,
                "contribution_output_cap": 192,
                "token_limit": 6000,
                "budget_twd": 0,
                "retry_limit": 0,
            },
            "completion_criteria": [
                "The Founder receives a concrete verified improvement."
            ],
        },
    }


def _operation(ceo, *, title="Truthful Mission", status="RUNNING", kernel_status="RUNNING", memory=None):
    row = Operation(
        title=title,
        objective="Keep Founder-visible company state truthful.",
        proposed_by_employee_id=ceo.id,
        status=status,
        kernel_status=kernel_status,
        route_type="SINGLE_WORKER",
        current_stage="EXECUTION",
        plan_json={"operation": {"completion_criteria": ["Done"]}},
        approved_budget_twd=Decimal("10"),
        estimated_cost_twd=Decimal("1"),
        hard_cost_cap_twd=Decimal("10"),
        stage_cost_cap_twd=Decimal("10"),
        single_call_cost_cap_twd=Decimal("2"),
        max_calls=3,
        max_revisions=0,
        max_messages=6,
        max_elapsed_seconds=3600,
        memory_json=memory or {"mission_kind": "REAL_WORK"},
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_execution_route_schema_cannot_return_advisory_or_null_operation(ctx):
    assert CEO_EXECUTION_SCHEMA["schema"]["properties"]["mode"]["enum"] == ["OPERATION_PLAN"]
    assert CEO_EXECUTION_SCHEMA["schema"]["properties"]["operation"]["type"] == "object"
    assert CEO_SCHEMA["schema"]["properties"]["executive_response"]["maxLength"] == 420


def test_run80_regression_is_failed_semantically_instead_of_validation_passed(ctx, monkeypatch):
    ceo = Employee.query.filter_by(slug="ceo").one()
    advisory = {
        "mode": "ADVISORY",
        "executive_response": "The highest-impact problem is silent reliability failure.",
        "project": None,
        "project_id": None,
        "tasks": [],
        "operation": None,
    }
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _: _provider(advisory))
    request = (
        "Make Eason One more reliable for Founder delegation. You are responsible "
        "for achieving a concrete improvement, not only producing recommendations. "
        "Organize the necessary employees yourself. Hard total provider-cost cap: NT$10."
    )
    assert route_command(request)["route_type"] == "AUTO_DELEGATION"
    run, proposal = founder_request(ceo, request)
    assert proposal is None
    assert run.status == "FAILED"
    assert run.failure_reason == "STRUCTURED_OUTPUT_INVALID"
    assert run.structured_validation_status == "FAILED"
    assert "no executable Operation proposal" in run.error_text
    assert Operation.query.count() == 0


def test_delegated_work_creates_actionable_operation_with_founder_cap(ctx, monkeypatch):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    payload = _execution_payload(ceo, researcher)
    monkeypatch.setattr("eason_one.services.execution.get_provider", lambda _: _provider(payload))
    run, operation = founder_request(
        ceo,
        "Deliver one concrete verified improvement. Hard total provider-cost cap: NT$10.",
    )
    assert run.status == "SUCCEEDED"
    assert run.structured_validation_status == "PASSED"
    assert operation is not None
    assert operation.status == "PLANNED"
    assert Decimal(operation.hard_cost_cap_twd) == Decimal("10.0000")
    assert run.operation_id == operation.id
    event = operation.memory_json["founder_attention_events"][-1]
    assert event["kind"] == "OPERATION_APPROVAL"
    assert event["status"] == "PENDING"
    governance = current_company.unresolved_governance()
    assert any(item.get("operation") and item["operation"].id == operation.id for item in governance)


def test_system_validation_cannot_become_founder_current_mission(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    validation = _operation(
        ceo,
        title="September Project Priority Decision — Autonomous Meeting Validation",
        memory={"mission_kind": "REAL_WORK"},
    )
    assert operation_kind(validation) == SYSTEM_VALIDATION
    snapshot = headquarters_snapshot()
    assert snapshot["focus"] is None
    assert global_runtime_snapshot()["active"] is False


def test_no_worker_means_no_working_task_or_employee(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    operation = _operation(ceo)
    project = Project(
        name="Truth project",
        objective="Test truthful presence.",
        status="ACTIVE",
        environment="LIVE",
        origin="NEW",
        owner_employee_id=ceo.id,
    )
    db.session.add(project)
    db.session.flush()
    operation.project_id = project.id
    task = Task(
        project_id=project.id,
        operation_id=operation.id,
        title="Persisted stale task",
        objective="Must not look live without a worker.",
        status="WORKING",
        assigned_employee_id=researcher.id,
        created_by_employee_id=ceo.id,
        required_output="Verified result",
        acceptance_criteria="Done",
    )
    db.session.add(task)
    db.session.commit()

    runtime = operation_runtime.runtime_snapshot(operation)
    assert runtime["worker_alive"] is False
    assert runtime["active_task"] is None
    assert employee_presence(researcher)["state"] == "AVAILABLE"
    mission = mission_snapshot(operation)
    assert mission["mission"]["current_stage"] == "READY"
    assert mission["task_display_status"][task.id] == "READY"


def test_every_needs_founder_state_has_persisted_reason_choices_and_actions(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    operation = _operation(ceo)
    operations.wait_for_founder(
        operation,
        "Choose whether to accept the bounded repair risk.",
        decision_kind="CODEX_RISK_APPROVAL",
    )
    db.session.refresh(operation)
    event = operation.memory_json["founder_attention_events"][-1]
    assert event["status"] == "PENDING"
    assert event["reason"] == "Choose whether to accept the bounded repair risk."
    assert event["choices"] == ["APPROVE", "MODIFY", "REJECT"]
    row = next(item for item in current_company.projection()["operations"] if item["operation"].id == operation.id)
    assert row["classification"] == "WAITING_FOR_FOUNDER"


def test_successful_run_without_actual_result_is_not_a_validated_artifact(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    researcher = Employee.query.filter_by(slug="researcher").one()
    operation = _operation(ceo)
    project = Project(
        name="Artifact truth",
        objective="Prevent objective text from masquerading as a result.",
        status="ACTIVE",
        environment="LIVE",
        origin="NEW",
        owner_employee_id=ceo.id,
    )
    db.session.add(project)
    db.session.flush()
    operation.project_id = project.id
    task = Task(
        project_id=project.id,
        operation_id=operation.id,
        title="Incomplete output",
        objective="This is only the task objective, not an artifact.",
        status="WORKING",
        assigned_employee_id=researcher.id,
        created_by_employee_id=ceo.id,
        required_output="A real artifact",
        acceptance_criteria="Actual result exists",
    )
    db.session.add(task)
    db.session.flush()
    model = researcher.current_model
    run = AgentRun(
        employee_id=researcher.id,
        project_id=project.id,
        task_id=task.id,
        operation_id=operation.id,
        model_config_id=model.id,
        purpose="TASK_EXECUTION",
        user_request="Do the work",
        system_prompt_snapshot="system",
        context_snapshot="context",
        raw_output=json.dumps({}),
        parsed_output_json={},
        status="SUCCEEDED",
        structured_validation_status="PASSED",
        provider_key_snapshot=model.provider_key,
        model_name_snapshot=model.model_name,
        input_price_snapshot=model.input_price_per_million,
        output_price_snapshot=model.output_price_per_million,
        currency_snapshot=model.currency,
    )
    db.session.add(run)
    db.session.commit()
    assert mission_snapshot(operation)["artifacts"] == []

    task.status = "DONE"
    task.result_summary = "A concrete reviewable result exists."
    db.session.commit()
    artifacts = mission_snapshot(operation)["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["status"] == "UNVERIFIED RESULT"
    assert artifacts[0]["kind"] == "TASK_RESULT"
    assert artifacts[0]["verified"] is False
    assert artifacts[0]["summary"] == "A concrete reviewable result exists."

    # A real, structured provider success promotes the same completed Task result
    # without requiring it to masquerade as a file-backed Artifact.
    run.provider_key_snapshot = "openai"
    db.session.commit()
    artifacts = mission_snapshot(operation)["artifacts"]
    assert artifacts[0]["status"] == "VALIDATED RESULT"
    assert artifacts[0]["verified"] is True


def test_ceo_line_alignment_and_primary_navigation_are_regression_locked(ctx):
    css = open("eason_one/static/headquarters-v0110.css", encoding="utf-8").read()
    template = open("eason_one/templates/hq_base.html", encoding="utf-8").read()
    assert ".hq-ceo-line-button" in css
    assert "align-items:center!important" in css.replace(" ", "")
    assert "justify-content:center!important" in css.replace(" ", "")
    assert "line-height:1!important" in css.replace(" ", "")
    assert "('/headquarters/people','People','P')" in template
    assert "('/headquarters/meetings','Meetings','◎')" in template
    assert "?v=0.12.2" in template
    if "headquarters-v015-founder.css" in template:
        assert "('/headquarters/truth','Company Truth','↺')" in template
        assert "('/headquarters/finance','Finance','₵')" in template
        assert "('/headquarters/ceo-office','CEO Office'" not in template
    elif "headquarters-v014.css" in template:
        assert "('/headquarters/ceo-office','CEO Office','◈')" in template
