import json

import pytest

from eason_one.extensions import db
from eason_one.models import (
    AgentRun, Employee, HiringRequest, MeetingMessage, OperationStep, Task,
)
from eason_one.providers import ProviderResult
from eason_one.services import operations, workforce
from eason_one.services.ceo import founder_request
from eason_one.services.meetings import create as create_meeting


def _operation():
    ceo = Employee.query.filter_by(slug="ceo").one()
    _, operation = founder_request(
        ceo, "Prepare the first qualified-prospect validation plan"
    )
    operation.approved_budget_twd = 20
    return operations.approve(operation)


def _task_result(text="Remediation completed"):
    return ProviderResult(json.dumps({
        "result_summary": text, "knowledge_proposals": [],
    }), 20, 10, response_id="task-result")


def test_meeting_and_latest_review_intelligence_reach_rework_without_transcript(
    ctx, monkeypatch
):
    operation = _operation()
    task = operation.tasks[0]
    task.status = "WORKING"
    operation.memory_json = {
        "meeting_results": [{
            "meeting_id": 17,
            "result": {
                "position": "Use a narrower prepaid probe",
                "qualification_or_disagreement": "Independent validation",
                "risk": "Admissions consulting contamination",
                "actions": ["Recruit qualified prospects"],
                "controls": ["No admissions consulting"],
            },
        }],
        "decisions": [{
            "action": "CONTINUE",
            "reason": "Apply the bounded Meeting remediation.",
        }],
    }
    reviewer = task.reviewer
    review = AgentRun(
        employee_id=reviewer.id, project_id=task.project_id, task_id=task.id,
        operation_id=operation.id, model_config_id=reviewer.current_model.id,
        purpose="TASK_REVIEW", user_request="Review",
        system_prompt_snapshot="review", context_snapshot="review",
        parsed_output_json={
            "decision": "BLOCK",
            "summary": "Independent validation is insufficient.",
            "issues": ["insufficient independent validation"],
            "required_changes": [
                "use an independent reviewer", "narrow the evidence criteria",
            ],
        },
        status="SUCCEEDED", provider_key_snapshot="mock",
        model_name_snapshot="deterministic-mock", input_price_snapshot=0,
        output_price_snapshot=0, currency_snapshot="TWD",
    )
    db.session.add(review)
    audit_meeting = create_meeting(
        "Transcript exclusion audit", "Audit context hygiene",
        "Verify transcript exclusion", reviewer, [reviewer],
        operation.project, max_rounds=1, real_cost_limit_twd=1,
    )
    db.session.add(MeetingMessage(
        meeting_id=audit_meeting.id, speaker_type="SYSTEM",
        message_type="TRANSCRIPT",
        content="FULL MEETING TRANSCRIPT SECRET",
    ))
    db.session.commit()
    seen = []

    class Capture:
        def complete(self, model, system, user, context, maximum, schema=None):
            seen.append(context)
            return _task_result()

    monkeypatch.setattr(
        "eason_one.services.execution.get_provider", lambda key: Capture()
    )
    operations.next_step(operation, "context-rework")
    run = AgentRun.query.filter_by(
        operation_id=operation.id, task_id=task.id, purpose="TASK_EXECUTION"
    ).order_by(AgentRun.id.desc()).first()
    assert "Use a narrower prepaid probe" in run.context_snapshot
    assert "No admissions consulting" in run.context_snapshot
    assert "BLOCK" in run.context_snapshot
    assert "insufficient independent validation" in run.context_snapshot
    assert "use an independent reviewer" in run.context_snapshot
    assert "narrow the evidence criteria" in run.context_snapshot
    assert "FULL MEETING TRANSCRIPT SECRET" not in run.context_snapshot
    operation_section = run.context_composition_json["operation_context"]
    assert operation_section["characters"] <= operation_section["budget"]


def test_ceo_create_task_is_strict_and_duplicate_replay_is_idempotent(
    ctx, monkeypatch
):
    operation = _operation()
    blocked = operation.tasks[0]
    blocked.status = "BLOCKED"
    assignee = Employee.query.filter_by(slug="engineer").one()
    reviewer = Employee.query.filter_by(slug="engineering-director").one()
    db.session.commit()

    class Decision:
        def complete(self, *args, **kwargs):
            return ProviderResult(json.dumps({
                "action": "CREATE_TASK",
                "reason": "Create bounded independent remediation.",
                "goal_status": "BLOCKED",
                "confidence": 0.9,
                "next_task_id": None,
                "meeting": None,
                "hiring_need": None,
                "founder_request": None,
                "task_plan": {
                    "task_id": None,
                    "title": "Independent validation remediation",
                    "objective": "Validate the Operation evidence independently.",
                    "assignee_employee_id": assignee.id,
                    "reviewer_employee_id": reviewer.id,
                    "acceptance_criteria": ["Independent evidence is recorded"],
                    "reason": "Resolve the approved Operation blocker.",
                },
            }), 20, 10, response_id="create-decision")

    monkeypatch.setattr(
        "eason_one.services.execution.get_provider", lambda key: Decision()
    )
    before = len(operation.tasks)
    first = operations.next_step(operation, "same-create")
    duplicate = operations.next_step(operation, "same-create")
    assert first["action"] == duplicate["action"] == "CREATE_TASK"
    assert len(operation.tasks) == before + 1
    created = db.session.get(Task, first["task_id"])
    assert created.operation_id == operation.id
    assert created.project_id == operation.project_id
    assert created.created_by_employee_id == operation.proposed_by_employee_id
    assert OperationStep.query.filter_by(
        operation_id=operation.id, kind="DECISION"
    ).count() == 1


def test_reassignment_is_bounded_audited_and_done_work_is_immutable(ctx):
    operation = _operation()
    task = operation.tasks[0]
    replacement = Employee.query.filter_by(slug="engineer").one()
    reviewer = Employee.query.filter_by(slug="engineering-director").one()
    task.status = "BLOCKED"
    db.session.commit()
    operations.reassign_task(
        operation, task, replacement, reviewer,
        "Newly approved capability resolves the material blocker.",
    )
    assert task.assigned_employee_id == replacement.id
    assert task.reviewer_employee_id == reviewer.id
    task.status = "DONE"
    db.session.commit()
    with pytest.raises(ValueError, match="cannot be reassigned"):
        operations.reassign_task(
            operation, task,
            Employee.query.filter_by(slug="researcher").one(),
            Employee.query.filter_by(slug="research-director").one(),
            "Attempt to alter completed work.",
        )


def test_dynamic_task_ceiling_is_twenty(ctx):
    operation = _operation()
    ceo = Employee.query.filter_by(slug="ceo").one()
    assignee = Employee.query.filter_by(slug="engineer").one()
    reviewer = Employee.query.filter_by(slug="engineering-director").one()
    for number in range(19):
        db.session.add(Task(
            operation_id=operation.id, project_id=operation.project_id,
            title=f"Existing remediation {number}",
            objective=operation.objective, status="CANCELLED",
            created_by_employee_id=ceo.id,
            assigned_employee_id=assignee.id,
            reviewer_employee_id=reviewer.id,
            acceptance_criteria="Bounded acceptance",
        ))
    db.session.commit()
    assert len(operation.tasks) == 20
    with pytest.raises(ValueError, match="20"):
        operations.create_remediation_task(operation, {
            "title": "Overflow",
            "objective": operation.objective,
            "assignee_employee_id": assignee.id,
            "reviewer_employee_id": reviewer.id,
            "acceptance_criteria": ["Bounded acceptance"],
            "reason": "Would exceed the deterministic ceiling.",
        })
    db.session.refresh(operation)
    assert operation.status == "WAITING_FOR_FOUNDER"
    assert len(operation.tasks) == 20


def test_approved_hire_executes_remediation_and_hire_alone_cannot_complete(
    ctx, monkeypatch
):
    operation = _operation()
    original = operation.tasks[0]
    original.status = "BLOCKED"
    ceo = Employee.query.filter_by(slug="ceo").one()
    engineering = Employee.query.filter_by(
        slug="engineering-director"
    ).one().department
    manager = Employee.query.filter_by(slug="engineering-director").one()
    model = Employee.query.filter_by(slug="engineer").one().current_model
    request = workforce.request_hire(
        requester=ceo, requested_by_type="EMPLOYEE", operation=operation,
        project=operation.project, role_needed="Validation Specialist",
        problem="Independent validation capacity is absent.",
        why_now="A material blocker prevents completion.",
        responsibilities=["Perform independent validation"],
        capabilities=["Independent evidence review"],
        urgency="HIGH", use_frequency="RECURRING",
    )
    request.status = "FOUNDER_REVIEW"
    request.hr_assessment_json = {
        "recommendation": "HIRE", "probation_assignments": 3,
        "instructions": "Validate evidence independently.",
        "resource_envelope": {"max_mission_budget_twd": "1.20"},
    }
    request.target_department_id = engineering.id
    request.target_position = "Validation Specialist"
    request.manager_employee_id = manager.id
    request.recommended_model_config_id = model.id
    db.session.commit()
    hired = workforce.approve_hire(request)
    allowed, reasons = operations.completion_guard(operation)
    assert not allowed
    assert any("blocked" in reason.lower() for reason in reasons)
    remediation = operations.create_remediation_task(operation, {
        "title": "Independent remediation",
        "objective": "Apply the narrower prepaid probe independently.",
        "assignee_employee_id": hired.id,
        "reviewer_employee_id": manager.id,
        "acceptance_criteria": ["Independent validation is complete"],
        "reason": "Use the capability approved for this blocker.",
    })
    operation.memory_json = {
        "meeting_results": [{
            "meeting_id": 22,
            "result": {"position": "Use a narrower prepaid probe",
                       "controls": ["No admissions consulting"]},
        }],
    }
    db.session.commit()

    class TaskProvider:
        def complete(self, *args, **kwargs):
            return _task_result("New specialist completed remediation.")

    monkeypatch.setattr(
        "eason_one.services.execution.get_provider", lambda key: TaskProvider()
    )
    result = operations.next_step(operation, "new-hire-work")
    run = db.session.get(AgentRun, result["agent_run_id"])
    assert remediation.assigned_employee_id == hired.id
    assert run.employee_id == hired.id
    assert run.operation_id == operation.id
    assert "Use a narrower prepaid probe" in run.context_snapshot
    assert original.status == "BLOCKED"
    assert not operations.completion_guard(operation)[0]
