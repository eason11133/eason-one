import json

from eason_one.extensions import db
from eason_one.models import AgentRun, Employee, HiringRequest, Meeting, OperationStep
from eason_one.providers import MockProvider, ProviderResult
from eason_one.services import operations, workforce
from eason_one.services.ceo import founder_request


def test_rework_uses_a_new_logical_execution_attempt(ctx):
    ceo = Employee.query.filter_by(slug="ceo").one()
    _, operation = founder_request(
        ceo, "Prepare a bounded qualified prospect validation"
    )
    operations.approve(operation)
    task = operation.tasks[0]

    operations.next_step(operation, "task-attempt-one")
    task.status = "BLOCKED"
    db.session.commit()

    task.status = "WORKING"
    db.session.commit()
    result = operations.next_step(operation, "task-attempt-two")

    assert result["kind"] == "TASK"
    assert task.status == "REVIEW"
    attempts = OperationStep.query.filter_by(
        operation_id=operation.id, kind="TASK", task_id=task.id
    ).order_by(OperationStep.id).all()
    assert len(attempts) == 2
    assert attempts[0].logical_key != attempts[1].logical_key
    assert AgentRun.query.filter_by(
        operation_id=operation.id, task_id=task.id, purpose="TASK_EXECUTION"
    ).count() == 2


def test_paid_create_task_decision_recovery_materializes_once(
    ctx, monkeypatch
):
    ceo = Employee.query.filter_by(slug="ceo").one()
    _, operation = founder_request(
        ceo, "Prepare a bounded qualified prospect validation"
    )
    operations.approve(operation)
    operation.tasks[0].status = "BLOCKED"
    assignee = Employee.query.filter_by(slug="engineer").one()
    reviewer = Employee.query.filter_by(slug="engineering-director").one()
    decision = {
        "action": "CREATE_TASK", "reason": "Recover remediation.",
        "goal_status": "BLOCKED", "confidence": 0.8,
        "next_task_id": None, "meeting": None, "hiring_need": None,
        "founder_request": None,
        "task_plan": {
            "task_id": None, "title": "Recovered remediation",
            "objective": "Resolve the bounded Operation blocker.",
            "assignee_employee_id": assignee.id,
            "reviewer_employee_id": reviewer.id,
            "acceptance_criteria": ["Blocker is independently resolved"],
            "reason": "Use already-paid CEO intelligence.",
        },
    }
    run = AgentRun(
        employee_id=ceo.id, project_id=operation.project_id,
        operation_id=operation.id, model_config_id=ceo.current_model.id,
        purpose="CEO_OPERATION_DECISION", user_request="Decide",
        system_prompt_snapshot="decision", context_snapshot="operation",
        raw_output=json.dumps(decision), parsed_output_json=decision,
        status="SUCCEEDED", provider_key_snapshot="mock",
        model_name_snapshot="deterministic-mock", input_price_snapshot=0,
        output_price_snapshot=0, currency_snapshot="TWD",
    )
    db.session.add(run)
    db.session.flush()
    db.session.add(OperationStep(
        operation_id=operation.id, idempotency_key="interrupted-decision",
        logical_key="decision:1", kind="DECISION",
        agent_run_id=run.id, status="PROVIDER_CALL_STARTED",
    ))
    db.session.commit()
    calls = []
    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda key: calls.append(key),
    )

    recovered = operations.next_step(operation, "resume-decision")
    duplicate = operations.next_step(operation, "interrupted-decision")

    assert recovered["status"] == "RECOVERED"
    assert duplicate["task_id"] == recovered["task_id"]
    assert len(operation.tasks) == 2
    assert calls == []


def test_complete_mocked_ceo_operation_meeting_hiring_lifecycle(
    ctx, monkeypatch
):
    ceo = Employee.query.filter_by(slug="ceo").one()
    hr = Employee.query.filter_by(slug="hr-director").one()
    hr.current_model_config_id = Employee.query.filter_by(
        slug="engineer"
    ).one().current_model_config_id
    db.session.commit()
    _, operation = founder_request(
        ceo, "Prepare a bounded qualified prospect validation"
    )
    operation.approved_budget_twd = 20
    operations.approve(operation)

    delegate = MockProvider()
    state = {"reviews": 0, "decisions": 0}

    class LifecycleProvider:
        def complete(
            self, model, system, user, context, max_tokens, schema=None
        ):
            if "TASK_REVIEW" in system:
                state["reviews"] += 1
                decision = "BLOCK" if state["reviews"] == 1 else "ACCEPT"
                return ProviderResult(json.dumps({
                    "decision": decision,
                    "summary": "Material capacity gap reviewed.",
                    "issues": (
                        ["Independent specialist capacity is missing."]
                        if decision == "BLOCK" else []
                    ),
                    "required_changes": (
                        ["Resolve the capacity gap and rerun the work."]
                        if decision == "BLOCK" else []
                    ),
                }), 10, 10, response_id=f"review-{state['reviews']}")
            if "CEO_OPERATION_DECISION" in system:
                state["decisions"] += 1
                actions = {
                    1: "MEETING", 2: "HIRING_REQUEST", 3: "REASSIGN_TASK"
                }
                action = actions[state["decisions"]]
                hiring = None
                meeting = None
                task_plan = None
                if action == "MEETING":
                    meeting = {
                        "question": "Can the capacity gap be remediated?",
                        "budget_twd": 1.0,
                    }
                if action == "HIRING_REQUEST":
                    hiring = {
                        "role_needed": "Validation Specialist",
                        "problem": "Independent validation capacity is missing.",
                        "why_now": "The approved operation is blocked.",
                        "responsibilities": ["Run independent validation"],
                        "capabilities": ["Evidence review"],
                        "urgency": "HIGH",
                        "use_frequency": "RECURRING",
                    }
                if action == "REASSIGN_TASK":
                    request = HiringRequest.query.filter_by(
                        operation_id=operation.id, status="HIRED"
                    ).one()
                    hired = request.created_employee
                    task_plan = {
                        "task_id": operation.tasks[0].id,
                        "title": None,
                        "objective": None,
                        "assignee_employee_id": hired.id,
                        "reviewer_employee_id": request.manager_employee_id,
                        "acceptance_criteria": [],
                        "reason": (
                            "Assign the material blocker to the capability "
                            "approved specifically to resolve it."
                        ),
                    }
                return ProviderResult(json.dumps({
                    "action": action,
                    "reason": "Use the next bounded governed intervention.",
                    "goal_status": "BLOCKED",
                    "confidence": 0.8,
                    "next_task_id": operation.tasks[0].id,
                    "meeting": meeting,
                    "hiring_need": hiring,
                    "founder_request": None,
                    "task_plan": task_plan,
                }), 10, 10, response_id=f"decision-{state['decisions']}")
            return delegate.complete(
                model, system, user, context, max_tokens, schema
            )

    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda key: LifecycleProvider(),
    )

    events = []
    for sequence in range(1, 60):
        result = operations.next_step(operation, f"lifecycle-{sequence}")
        events.append(result["kind"])
        if operation.status == "WAITING_FOR_FOUNDER":
            request = HiringRequest.query.filter_by(
                operation_id=operation.id
            ).one()
            assert request.status == "FOUNDER_REVIEW"
            workforce.approve_hire(request)
        if operation.status == "COMPLETED":
            break

    assert operation.status == "COMPLETED"
    assert operation.founder_report_json["headline"] == "Boss, it's complete."
    assert operation.tasks[0].status == "DONE"
    assert Meeting.query.filter_by(operation_id=operation.id).one().status == "ENDED"
    request = HiringRequest.query.filter_by(operation_id=operation.id).one()
    assert request.status == "HIRED"
    assert request.created_employee.department_id == request.target_department_id
    assert request.created_employee.manager_id == request.manager_employee_id
    hired_run = AgentRun.query.filter_by(
        operation_id=operation.id,
        employee_id=request.created_employee_id,
        purpose="TASK_EXECUTION",
    ).one()
    assert "Readiness remains unproven" in hired_run.context_snapshot
    assert "Independent specialist capacity is missing" in hired_run.context_snapshot
    assert "Resolve the capacity gap and rerun the work" in hired_run.context_snapshot
    assert "MEETING_RESULT" in events
    assert "HR_ASSESSMENT" in events
    assert events[-1] == "REPORT"
    assert state == {"reviews": 2, "decisions": 3}
