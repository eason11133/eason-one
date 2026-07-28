import json

import pytest

from eason_one.extensions import db
from eason_one.models import Employee, Operation, OperationStep, Task
from eason_one.providers import ProviderResult
from eason_one.schemas import CEO_SCHEMA
from eason_one.services import operations


def _plan(criteria, task_count=1):
    ceo = Employee.query.filter_by(slug="ceo").one()
    worker = Employee.query.filter_by(slug="researcher").one()
    reviewer = Employee.query.filter_by(slug="research-director").one()
    return ceo, {
        "mode": "OPERATION_PLAN",
        "executive_response": "I prepared bounded Company work.",
        "operation": {
            "title": "Daily operating objective",
            "objective": "Prepare a qualified-prospect validation plan.",
            "project_id": None,
            "budget_twd": 20,
            "tasks": [{
                "title": f"Validation work {number}",
                "objective": "Produce decision-useful validation evidence.",
                "assignee_employee_id": worker.id,
                "reviewer_employee_id": reviewer.id,
                "acceptance_criteria": ["Evidence is reviewable"],
            } for number in range(task_count)],
            "meeting_policy": "REQUIRED_ON_MATERIAL_CONFLICT",
            "completion_criteria": criteria,
        },
    }


def _operation(criteria=None, task_count=1):
    criteria = criteria or ["Evidence is accepted"]
    ceo, plan = _plan(criteria, task_count)
    return operations.approve(operations.propose_operation(ceo, plan))


def _verification(criteria):
    return {
        "overall_status": "SATISFIED",
        "criteria": [{
            "criterion": criterion,
            "status": "SATISFIED",
            "evidence": ["Accepted persisted result"],
            "reason": "The evidence supports this criterion.",
        } for criterion in criteria],
        "summary": "Every approved criterion is supported.",
        "recommended_action": "Produce the final report.",
    }


def test_all_twelve_completion_criteria_reach_goal_verifier(
    ctx, monkeypatch
):
    criteria = [f"Approved criterion {number}" for number in range(1, 13)]
    operation = _operation(criteria)
    operation.tasks[0].status = "DONE"
    operation.tasks[0].result_summary = "Accepted evidence"
    db.session.commit()
    seen = []

    class Capture:
        def complete(self, model, system, user, context, maximum, schema=None):
            seen.append(json.loads(
                context.split("GOAL VERIFICATION EVIDENCE\n", 1)[1]
            ))
            return ProviderResult(
                json.dumps(_verification(criteria)), 20, 10,
                response_id="twelve-criteria",
            )

    monkeypatch.setattr(
        "eason_one.services.execution.get_provider", lambda key: Capture()
    )
    result = operations.next_step(operation, "verify-twelve")
    assert result["overall_status"] == "SATISFIED"
    assert seen[0]["completion_criteria"] == criteria
    assert len(result["verification"]["criteria"]) == 12


def test_thirteen_completion_criteria_are_rejected_never_truncated(ctx):
    operation_schema = CEO_SCHEMA["schema"]["properties"]["operation"][
        "anyOf"
    ][0]
    assert operation_schema["properties"]["completion_criteria"][
        "maxItems"
    ] == 12
    ceo, plan = _plan([f"Criterion {number}" for number in range(13)])
    with pytest.raises(ValueError, match="1 and 12"):
        operations.propose_operation(ceo, plan)
    assert Operation.query.count() == 0


def test_goal_evidence_support_is_compacted_without_losing_criteria(ctx):
    criteria = [f"Criterion {number}" for number in range(12)]
    operation = _operation(criteria)
    operation.tasks[0].status = "DONE"
    operation.tasks[0].result_summary = "evidence " * 10000
    db.session.commit()
    packet, serialized, _ = operations.goal_evidence_packet(operation)
    assert packet["completion_criteria"] == criteria
    assert len(serialized) <= operations.GOAL_EVIDENCE_BUDGET


def test_active_operation_renders_founder_level_ceo_report(client, ctx):
    operation = _operation()
    task = operation.tasks[0]
    task.status = "WORKING"
    task.result_summary = "Qualified prospects prefer the narrower probe."
    operation.memory_json = {
        "goal_verification": {
            "overall_status": "NOT_SATISFIED",
            "criteria": [{
                "criterion": "Independent validation exists",
                "status": "NOT_SATISFIED",
                "evidence": [],
                "reason": "Independent evidence is still missing.",
            }],
            "summary": "More evidence is required.",
            "recommended_action": "Run independent validation.",
        },
    }
    db.session.commit()
    page = client.get("/command").get_data(as_text=True)
    assert operation.objective in page
    assert "Independent evidence is still missing" in page
    assert "Run independent validation" in page
    assert "Spent / remaining" in page
    assert "Founder decision required" not in page


def test_no_active_work_gives_quiet_ceo_office_after_completion(
    client, ctx
):
    operation = _operation()
    operation.status = "COMPLETED"
    operation.tasks[0].status = "DONE"
    operation.project.status = "COMPLETED"
    db.session.commit()
    page = client.get("/command").get_data(as_text=True)
    assert "CEO <b>AVAILABLE</b>" in page
    assert 'class="command-input ceo-composer"' in page
    assert "Good evening" not in page
    assert "what would you like us to do" not in page


def test_founder_attention_report_explains_decision_and_continuation(
    client, ctx
):
    operation = _operation()
    operations.wait_for_founder(
        operation, "Authorize access to Founder-only evidence."
    )
    page = client.get("/command").get_data(as_text=True)
    assert "Founder decision required" in page
    assert "Authorize access to Founder-only evidence" in page


def test_internal_blocker_is_owned_by_ceo_not_escalated(client, ctx):
    operation = _operation()
    task = operation.tasks[0]
    task.status = "BLOCKED"
    task.result_summary = "Evidence criteria need a narrower probe."
    db.session.commit()
    page = client.get("/command").get_data(as_text=True)
    assert "Founder decision required" not in page
    assert "Evidence criteria need a narrower probe" in page


def test_natural_language_work_request_enters_operation_path(
    client, ctx
):
    response = client.post("/command", data={
        "request": "Prepare a bounded qualified-prospect validation"
    })
    assert response.status_code == 302
    operation = Operation.query.one()
    assert operation.status == "PLANNED"
    assert operation.tasks == []
    page = client.get("/command").get_data(as_text=True)
    assert "Approve &amp; Run" in page


def test_ceo_office_running_operation_uses_shared_autonomous_runner(
    client, ctx
):
    operation = _operation()
    before = len(operation.steps)
    page = client.get("/command").get_data(as_text=True)
    assert f'data-operation-id="{operation.id}"' in page
    assert "operation-runner.js" in page
    response = client.post(
        f"/operations/{operation.id}/next-step",
        headers={"Idempotency-Key": "ceo-office-runner-step"},
    )
    assert response.status_code == 200
    assert response.get_json()["kind"] == "TASK"
    assert len(operation.steps) == before + 1
    operation.status = "PAUSED"
    db.session.commit()
    assert "data-operation-runner" not in client.get(
        "/command"
    ).get_data(as_text=True)
    operations.wait_for_founder(operation, "Founder authority required.")
    assert "data-operation-runner" not in client.get(
        "/command"
    ).get_data(as_text=True)


def test_current_operation_follow_up_reuses_running_operation(
    client, ctx
):
    operation = _operation()
    response = client.post("/command", data={
        "request": "continue the current operation"
    })
    assert response.status_code == 302
    assert Operation.query.count() == 1
    db.session.refresh(operation)
    assert operation.status == "RUNNING"
    assert operation.memory_json["founder_followups"][-1][
        "instruction"
    ] == "continue the current operation"
    assert "data-operation-runner" in client.get(
        "/command"
    ).get_data(as_text=True)


def test_genuinely_new_follow_up_may_propose_second_bounded_operation(
    client, ctx
):
    original = _operation()
    response = client.post("/command", data={
        "request": "Prepare a new bounded engineering security audit"
    })
    assert response.status_code == 302
    assert Operation.query.count() == 2
    assert db.session.get(Operation, original.id).status == "RUNNING"
    proposed = Operation.query.filter(Operation.id != original.id).one()
    assert proposed.status == "PLANNED"
    assert proposed.tasks == []


def _persist_verification(operation, status):
    criteria = operation.plan_json["operation"]["completion_criteria"]
    payload = {
        "overall_status": status,
        "criteria": [{
            "criterion": criterion,
            "status": status,
            "evidence": ["Persisted evidence"],
            "reason": "Verification result controls completion.",
        } for criterion in criteria],
        "summary": f"Verification is {status}.",
        "recommended_action": "Continue remediation.",
    }
    _, _, digest = operations.goal_evidence_packet(operation)
    db.session.add(OperationStep(
        operation_id=operation.id,
        idempotency_key=f"verification-{status}",
        logical_key=f"goal_verification:{digest}",
        kind="GOAL_VERIFICATION", status="SUCCEEDED",
        result_json={
            "kind": "GOAL_VERIFICATION", "overall_status": status,
            "verification": payload, "evidence_digest": digest,
        },
    ))
    operation.memory_json = {"goal_verification": {
        **payload, "evidence_digest": digest,
    }}
    db.session.commit()


@pytest.mark.parametrize(
    "status", ["NOT_SATISFIED", "INSUFFICIENT_EVIDENCE"]
)
def test_ceo_complete_is_rejected_until_goal_is_satisfied(
    ctx, monkeypatch, status
):
    operation = _operation()
    operation.tasks[0].status = "DONE"
    operation.tasks[0].result_summary = "Workflow finished."
    db.session.commit()
    _persist_verification(operation, status)

    class Complete:
        def complete(self, *args, **kwargs):
            return ProviderResult(json.dumps({
                "action": "COMPLETE", "reason": "Workflow is finished.",
                "goal_status": "COMPLETE", "confidence": 0.9,
                "next_task_id": None, "meeting": None,
                "hiring_need": None, "founder_request": None,
                "task_plan": None,
            }), 10, 10, response_id="complete-request")

    monkeypatch.setattr(
        "eason_one.services.execution.get_provider", lambda key: Complete()
    )
    result = operations.next_step(operation, f"reject-complete-{status}")
    assert result["action"] == "COMPLETE_REJECTED"
    assert operation.status == "RUNNING"
    assert not operation.ended_at
    assert not any(
        decision.get("action") == "COMPLETE"
        for decision in (operation.memory_json or {}).get("decisions") or []
    )
    step = OperationStep.query.filter_by(
        operation_id=operation.id, kind="DECISION"
    ).one()
    assert step.status == "SUCCEEDED"
    assert step.result_json["action"] == "COMPLETE_REJECTED"


def test_ceo_complete_is_valid_after_satisfied_goal_verification(
    ctx, monkeypatch
):
    operation = _operation()
    operation.tasks[0].status = "DONE"
    operation.tasks[0].result_summary = "Workflow finished."
    db.session.commit()
    _persist_verification(operation, "SATISFIED")

    class CompleteThenReport:
        def complete(self, model, system, user, context, maximum, schema=None):
            if "CEO_OPERATION_DECISION" in system:
                return ProviderResult(json.dumps({
                    "action": "COMPLETE", "reason": "Goal is verified.",
                    "goal_status": "COMPLETE", "confidence": 0.9,
                    "next_task_id": None, "meeting": None,
                    "hiring_need": None, "founder_request": None,
                    "task_plan": None,
                }), 10, 10, response_id="valid-complete")
            return __import__(
                "eason_one.providers", fromlist=["MockProvider"]
            ).MockProvider().complete(
                model, system, user, context, maximum, schema
            )

    monkeypatch.setattr(
        "eason_one.services.execution.get_provider",
        lambda key: CompleteThenReport(),
    )
    decision = operations._decision_step(operation, "valid-complete")
    assert decision["action"] == "COMPLETE"
    report = operations.next_step(operation, "report-valid-complete")
    assert report["kind"] == "REPORT"
    assert operation.status == "COMPLETED"
