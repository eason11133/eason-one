import json
from decimal import Decimal

import pytest

from eason_one.extensions import db
from eason_one.models import (
    AgentRun, CostEvent, Department, Employee, HiringRequest, KnowledgeItem, Meeting,
    MeetingMessage, MeetingParticipant, OperationStep, Task,
)
from eason_one.providers import MockProvider, ProviderResult
from eason_one.services import ceo_context, operations, workforce
from eason_one.services.brain import add_knowledge
from eason_one.services.ceo import founder_request
from eason_one.services.meetings import create as create_meeting


def _approved_operation():
    ceo = Employee.query.filter_by(slug="ceo").one()
    _, operation = founder_request(
        ceo, "Prepare the first qualified-prospect validation plan"
    )
    return operations.approve(operation)


def test_ceo_context_has_bounded_memory_without_raw_transcript(ctx):
    operation = _approved_operation()
    ceo = Employee.query.filter_by(slug="ceo").one()
    run = AgentRun(
        employee_id=ceo.id, project_id=operation.project_id,
        operation_id=operation.id, model_config_id=ceo.current_model.id,
        purpose="CEO_FOUNDER_REQUEST", user_request="Remember Learning Evidence",
        system_prompt_snapshot="audit prompt", context_snapshot="raw audit secret",
        parsed_output_json={"mode": "ADVISORY",
                            "executive_response": "Use the qualified probe."},
        status="SUCCEEDED", provider_key_snapshot="mock",
        model_name_snapshot="deterministic-mock", input_price_snapshot=0,
        output_price_snapshot=0, currency_snapshot="TWD",
    )
    db.session.add(run)
    add_knowledge(
        "DECISION", "Probe decision", "Use prepaid qualified prospects.",
        project_id=operation.project_id, founder_approved=True,
        rationale="Avoid false-positive demand.",
    )
    meeting = create_meeting(
        "Learning Evidence Review", "Resolve evidence", "Choose next action",
        ceo, [ceo], operation.project, max_rounds=1,
        real_cost_limit_twd=Decimal("1"),
    )
    meeting.operation_id = operation.id
    meeting.status = "ENDED"
    meeting.minutes_json = {
        "positions": [{"position": "Run a prepaid probe", "actions": ["Recruit"]}],
        "responses": [{"relation": "QUALIFY", "core_point": "Freeze scope",
                       "controls": ["No consulting"], "risk": "Contamination"}],
        "agreement": ["Use qualified prospects"],
    }
    db.session.add(MeetingMessage(
        meeting_id=meeting.id, speaker_type="SYSTEM", message_type="TRANSCRIPT",
        content="FULL TRANSCRIPT MUST NOT BE IN CEO CONTEXT",
    ))
    db.session.commit()
    composed = ceo_context.compose(
        ceo, founder_request="照剛剛那場會議的結果繼續",
        operation=operation, project=operation.project,
    )
    assert "Remember Learning Evidence" in composed.text
    assert "Use the qualified probe." in composed.text
    assert "Run a prepaid probe" in composed.text
    assert "Freeze scope" in composed.text
    assert "Probe decision" in composed.text
    assert "FULL TRANSCRIPT MUST NOT BE IN CEO CONTEXT" not in composed.text
    assert "raw audit secret" not in composed.text
    assert len(composed.text) <= composed.character_budget
    assert composed.composition["working_memory"]["included"] >= 1


def test_automatic_call_spends_authorized_budget_without_manual_task(ctx):
    operation = _approved_operation()
    model = operation.tasks[0].assigned_employee.current_model
    model.input_price_per_million = Decimal("1000")
    model.output_price_per_million = Decimal("1000")
    operation.approved_budget_twd = Decimal("10")
    db.session.commit()
    before = operations.remaining_budget(operation)
    result = operations.next_step(operation, "automatic-task")
    assert result["kind"] == "TASK"
    assert AgentRun.query.filter_by(operation_id=operation.id).count() == 1
    assert CostEvent.query.filter_by(operation_id=operation.id).count() == 1
    assert operations.actual_cost(operation) > 0
    assert operations.remaining_budget(operation) < before
    assert operation.tasks[0].status == "REVIEW"


def test_budget_rejection_calls_no_provider_and_waits_for_founder(
    ctx, monkeypatch
):
    operation = _approved_operation()
    operation.approved_budget_twd = Decimal("0.000001")
    model = operation.tasks[0].assigned_employee.current_model
    model.input_price_per_million = Decimal("1000")
    model.output_price_per_million = Decimal("1000")
    db.session.commit()
    calls = []
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda key: calls.append(key),
    )
    with pytest.raises(ValueError, match="budget"):
        operations.next_step(operation, "cannot-fit")
    assert calls == []
    assert operation.status == "WAITING_FOR_FOUNDER"
    report = operation.founder_report_json
    assert report["approved_twd"]
    assert report["actual_twd"]
    assert report["remaining_twd"]
    assert report["maximum_additional_authorization_twd"]


def test_paid_invalid_output_never_retries_automatically(ctx, monkeypatch):
    operation = _approved_operation()
    model = operation.tasks[0].assigned_employee.current_model
    model.input_price_per_million = Decimal("1000")
    model.output_price_per_million = Decimal("1000")
    operation.approved_budget_twd = Decimal("10")
    db.session.commit()
    calls = []

    class Invalid:
        def complete(self, *args, **kwargs):
            calls.append(1)
            return ProviderResult("not-json", 100, 50, response_id="paid")

    monkeypatch.setattr("eason_one.services.execution.get_provider",
                        lambda key: Invalid())
    with pytest.raises(ValueError):
        operations.next_step(operation, "paid-invalid")
    with pytest.raises(ValueError):
        operations.next_step(operation, "paid-invalid-retry-attempt")
    assert calls == [1]
    assert operation.status == "WAITING_FOR_FOUNDER"
    assert OperationStep.query.filter_by(
        operation_id=operation.id, status="PAID_FAILED"
    ).one()


def test_completion_guard_rejects_block_review_and_founder_wait(ctx):
    operation = _approved_operation()
    task = operation.tasks[0]
    task.status = "BLOCKED"
    db.session.commit()
    allowed, reasons = operations.completion_guard(operation)
    assert not allowed and "blocked" in " ".join(reasons).lower()
    task.status = "REVIEW"
    db.session.commit()
    assert not operations.completion_guard(operation)[0]
    task.status = "DONE"
    operation.status = "WAITING_FOR_FOUNDER"
    db.session.commit()
    assert not operations.completion_guard(operation)[0]


def test_block_routes_to_ceo_decision_and_existing_meeting(ctx, monkeypatch):
    operation = _approved_operation()
    task = operation.tasks[0]
    task.status = "BLOCKED"
    task.result_summary = "Material assumption rejected."
    db.session.commit()
    calls = []

    class DecisionProvider:
        def complete(self, model, system, user, context, max_tokens, schema=None):
            calls.append(system)
            return ProviderResult(json.dumps({
                "action": "MEETING", "reason": "Material conflict needs debate.",
                "goal_status": "BLOCKED", "confidence": 0.9,
                "next_task_id": None,
                "meeting": {
                    "question": "Can the rejected assumption be remediated?",
                    "budget_twd": 1.0,
                },
                "hiring_need": None, "founder_request": None,
            }), 100, 50, response_id="decision")

    monkeypatch.setattr("eason_one.services.execution.get_provider",
                        lambda key: DecisionProvider())
    result = operations.next_step(operation, "decide-block")
    assert result["kind"] == "DECISION"
    assert result["action"] == "MEETING"
    meeting = Meeting.query.filter_by(operation_id=operation.id).one()
    assert meeting.created_by == "CEO"
    assert meeting.status == "RUNNING"
    names = {item.employee.slug for item in meeting.participants}
    assert names == {"ceo", task.assigned_employee.slug, task.reviewer.slug}
    assert len(calls) == 1
    assert operation.status != "COMPLETED"


def test_operation_advances_existing_meeting_and_consumes_result(
    ctx, monkeypatch
):
    operation = _approved_operation()
    task = operation.tasks[0]
    task.status = "BLOCKED"
    meeting = operations.create_meeting_for_conflict(
        operation, task, "Resolve material conflict", Decimal("1")
    )
    meeting.status = "ENDED"
    meeting.minutes_json = {
        "positions": [{"position": "Remediate with a narrower probe",
                       "actions": ["Revise scope"]}],
        "responses": [], "agreement": ["Narrow scope"],
    }
    db.session.commit()
    result = operations.next_step(operation, "consume-meeting")
    assert result["kind"] == "MEETING_RESULT"
    assert "Remediate with a narrower probe" in json.dumps(
        operation.memory_json
    )
    assert AgentRun.query.filter_by(operation_id=operation.id).count() == 0


def test_ambiguous_provider_started_step_never_double_spends(
    ctx, monkeypatch
):
    operation = _approved_operation()
    task = operation.tasks[0]
    db.session.add(OperationStep(
        operation_id=operation.id, idempotency_key="old-request",
        logical_key=f"task:{task.id}:execute", kind="TASK", task_id=task.id,
        status="PROVIDER_CALL_STARTED",
    ))
    db.session.commit()
    calls = []
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda key: calls.append(key),
    )
    with pytest.raises(ValueError, match="recovery"):
        operations.next_step(operation, "new-request")
    assert calls == []
    assert operation.status == "WAITING_FOR_FOUNDER"
    assert OperationStep.query.filter_by(
        operation_id=operation.id, status="AMBIGUOUS"
    ).one()


def test_successful_unmaterialized_run_is_reused_without_new_call(
    ctx, monkeypatch
):
    operation = _approved_operation()
    task = operation.tasks[0]
    task.status = "WORKING"
    employee = task.assigned_employee
    run = AgentRun(
        employee_id=employee.id, project_id=task.project_id, task_id=task.id,
        operation_id=operation.id, model_config_id=employee.current_model.id,
        purpose="TASK_EXECUTION", user_request=task.objective,
        system_prompt_snapshot="snapshot", context_snapshot="snapshot",
        raw_output=json.dumps({"result_summary": "Recovered paid result",
                               "knowledge_proposals": []}),
        parsed_output_json={"result_summary": "Recovered paid result",
                            "knowledge_proposals": []},
        status="SUCCEEDED", provider_key_snapshot="mock",
        model_name_snapshot="deterministic-mock", input_price_snapshot=0,
        output_price_snapshot=0, currency_snapshot="TWD",
    )
    db.session.add(run)
    db.session.flush()
    db.session.add(OperationStep(
        operation_id=operation.id, idempotency_key="interrupted",
        logical_key=f"task:{task.id}:execute", kind="TASK", task_id=task.id,
        agent_run_id=run.id, status="PROVIDER_CALL_STARTED",
    ))
    db.session.commit()
    calls = []
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda key: calls.append(key),
    )
    result = operations.next_step(operation, "resume")
    assert result["kind"] == "TASK"
    assert task.status == "REVIEW"
    assert task.result_summary == "Recovered paid result"
    assert calls == []


def test_hr_director_executes_assessment_and_math_is_deterministic(
    ctx, monkeypatch
):
    operation = _approved_operation()
    ceo = Employee.query.filter_by(slug="ceo").one()
    request = workforce.request_hire(
        requester=ceo, requested_by_type="EMPLOYEE",
        operation=operation, project=operation.project,
        role_needed="Security Reviewer", problem="Missing security review",
        why_now="A material operation is blocked",
        responsibilities=["Review threats"], capabilities=["Threat modeling"],
        urgency="HIGH", use_frequency="RECURRING",
    )
    engineering = Department.query.filter_by(name="Engineering Department").one()
    manager = Employee.query.filter_by(slug="engineering-director").one()
    model = Employee.query.filter_by(slug="engineer").one().current_model
    hr = Employee.query.filter_by(slug="hr-director").one()
    hr.current_model_config_id = model.id
    db.session.commit()

    class HRProvider:
        def complete(self, *args, **kwargs):
            return ProviderResult(json.dumps({
                "recommendation": "HIRE",
                "existing_staff_alternative": "Engineer lacks independent review.",
                "need_duration": "PERSISTENT",
                "duplicate_capability": "No active independent security reviewer.",
                "candidate_template_id": None,
                "target_department_id": engineering.id,
                "target_position": "Security Reviewer",
                "manager_employee_id": manager.id,
                "recommended_model_config_id": model.id,
                "estimated_input_tokens": 1000,
                "estimated_output_tokens": 500,
                "expected_calls_per_mission": 2,
                "missions_per_month": 4,
                "max_mission_budget_twd": 1.2,
                "expected_benefit": "Independent security challenge.",
                "redundancy_risk": "LOW",
                "alternatives": ["Use Engineer", "Temporary review"],
                "success_criteria": ["Three useful reviews"],
                "probation_assignments": 3,
                "instructions": "Challenge security assumptions.",
            }), 100, 50, response_id="hr")

    monkeypatch.setattr("eason_one.services.execution.get_provider",
                        lambda key: HRProvider())
    workforce.assess_request(request)
    assert request.status == "FOUNDER_REVIEW"
    assert request.hr_agent_run_id
    assert AgentRun.query.get(request.hr_agent_run_id).purpose == "HR_ASSESSMENT"
    assert request.target_department_id == engineering.id
    assert request.manager_employee_id == manager.id
    math = request.hr_assessment_json["financial_math"]
    expected = workforce.estimate_mission_cost(model, 1000, 500, 2)
    assert Decimal(math["estimated_mission_cost_twd"]) == expected
    assert Decimal(math["estimated_monthly_cost_twd"]) == expected * 4


def test_approved_hire_uses_approved_department_and_manager(ctx):
    operation = _approved_operation()
    request = workforce.request_hire(
        requested_by_type="FOUNDER", operation=operation,
        project=operation.project, role_needed="Security Reviewer",
        problem="Gap", why_now="Now", responsibilities=["Review"],
        capabilities=["Security"], urgency="HIGH",
        use_frequency="RECURRING",
    )
    engineering = Employee.query.filter_by(
        slug="engineering-director"
    ).one().department
    manager = Employee.query.filter_by(slug="engineering-director").one()
    model = Employee.query.filter_by(slug="engineer").one().current_model
    request.status = "FOUNDER_REVIEW"
    request.hr_assessment_json = {
        "recommendation": "HIRE", "probation_assignments": 3,
        "instructions": "Review security.", "resource_envelope": {
            "max_mission_budget_twd": "1.20"
        },
    }
    request.target_department_id = engineering.id
    request.target_position = "Security Reviewer"
    request.manager_employee_id = manager.id
    request.recommended_model_config_id = model.id
    db.session.commit()
    employee = workforce.approve_hire(request)
    assert employee.department_id == engineering.id
    assert employee.manager_id == manager.id
    assert employee.department.name != "Human Resources"


def test_founder_hr_request_authorizes_one_assessment_not_manual_form(client):
    page = client.get("/team/hr").get_data(as_text=True)
    assert "maximum hr assessment authorization" in page.lower()
    assert "Complete HR review" not in page
    assert "EXISTING-STAFF ALTERNATIVE" not in page


def test_operation_telemetry_is_deterministically_derived(ctx):
    operation = _approved_operation()
    telemetry = operations.telemetry(operation)
    assert set(telemetry) >= {
        "goal_status", "provider_calls", "input_tokens", "output_tokens",
        "actual_cost_twd", "meetings_used", "employees_used", "reviews_used",
        "blocked_count",
    }
    assert "success_score" not in telemetry
